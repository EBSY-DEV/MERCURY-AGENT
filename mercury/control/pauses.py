"""Out-of-office pauses for the dashboard and the CLI.

A vacation reply pauses one contact's cold sequence until the day they are
back (``mercury/ooo.py`` reads the date, the sender resumes it). A person can
correct that day, set one the reply did not give, or resume the contact now.
Neither ever approves an email: a draft waiting for review still waits.

Every change lands in the activity log with who made it. Failures raise
PauseError with a stable code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from mercury import ooo
from mercury.policy import ContactPolicy, capability

MAX_NOTE = 300


class PauseError(ValueError):
    """Codes: invalid, not_found, past, over."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PauseService:
    def __init__(self, state, config=None, clock=_utcnow):
        self.state, self.config, self.clock = state, config, clock
        self.policy = ContactPolicy(state, config)

    async def ready(self):
        await self.state.init_db()
        return self

    def timezone(self) -> str:
        usage = getattr(self.config, "usage", None)
        quiet = getattr(usage, "quiet_hours", None)
        return getattr(quiet, "timezone", "") or "UTC"

    def capability(self) -> dict:
        native = capability(self.config)["send_time_exclusions"] if self.config else True
        return {"enforced": native, "note": "" if native else (
            "Instantly sends the sequence itself, so Mercury cannot pause it for someone "
            "who is away. Pause the lead in Instantly, or switch channels.email.provider "
            "to gmail or smtp.")}

    def _public(self, row: dict) -> dict:
        tz = row.get("timezone") or self.timezone()
        resume = ooo.parse_stored(row.get("resume_at"))
        return {
            **row,
            "back_on": row.get("return_date") or (ooo.local_day(resume, tz).isoformat() if resume else None),
            "resume_at": row.get("resume_at") or "",
            "resume_local": ooo.local_day(resume, tz).isoformat() if resume else "",
            "manual_override": bool(row.get("manual_override")),
            "display_timezone": tz,
            "review_text": ooo.REVIEW_REASONS.get(row.get("review_reason") or "", ""),
        }

    async def list(self, ended: bool = False, limit: int = 200) -> list[dict]:
        return [self._public(r) for r in await self.state.list_pauses(ended, limit)]

    async def _active(self, pause_id: str) -> dict:
        pause = await self.state.get_pause(pause_id)
        if not pause or pause["ended_at"]:
            raise PauseError("not_found", f"No active pause {pause_id!r}.")
        return pause

    async def get(self, pause_id: str) -> dict:
        rows = {r["id"]: r for r in await self.state.list_pauses(False)}
        row = rows.get(pause_id) or await self.state.get_pause(pause_id)
        if not row:
            raise PauseError("not_found", f"No pause {pause_id!r}.")
        return {**self._public(row),
                "messages": await self.state.auto_replies_for(row["prospect_id"])}

    async def set_return_date(self, pause_id: str, day: str, *, note: str = "",
                              actor: str = "") -> dict:
        """Set their return day and schedule the first send using quiet hours,
        weekends and the configured business-day buffer."""
        try:
            back = date.fromisoformat((day or "").strip())
        except ValueError:
            raise PauseError("invalid", "Use a date like 2026-10-20.") from None
        pause = await self._active(pause_id)
        pause_id = pause["id"]
        tz = self.timezone()
        now = self.clock()
        today = ooo.local_day(now, tz)
        if back < today:
            raise PauseError("past", "That day has passed. Pick today or later, or resume "
                                     "them now.")
        if (back - today).days > ooo.MAX_AWAY_DAYS:
            raise PauseError("invalid", "Pick a day within the next year.")
        _tz, quiet_end = ooo.operator_clock(self.config)
        buffer = getattr(getattr(getattr(self.config, "channels", None), "email", None),
                         "ooo_resume_buffer_days", 0)
        resume_at = ooo.resume_time(back, tz, quiet_end, buffer).isoformat()
        changed = await self.state.set_pause_resume_at(pause_id, resume_at, actor=actor,
                                                       now=now.isoformat(), return_date=back.isoformat())
        if changed is None:
            raise PauseError("not_found", f"No active pause {pause_id!r}.")
        before, after = changed
        prospect = await self.state.get_prospect(pause["prospect_id"])
        await self.state.log_action("ooo_return_date_set", actor or "dashboard", {
            "prospect_id": pause["prospect_id"],
            "prospect_email": prospect.email if prospect else "",
            "pause_id": pause_id, "before": before["resume_at"], "after": resume_at,
            "back_on": back.isoformat(), "timezone": tz,
            "note": (note or "").strip()[:MAX_NOTE],
        })
        await self.state.log_action("pause_date_changed", actor or "dashboard", {
            "prospect_id": pause["prospect_id"], "by": "operator",
            "from": before.get("resume_at") or "", "was": before["state"],
            "to": after.get("resume_at") or "", "now": after["state"],
        })
        return {**self._public(after), "success": True}

    async def resume(self, pause_id: str, *, note: str = "", actor: str = "") -> dict:
        """Resume now: the next unsent step is due and later steps keep their
        gaps. A contact who has left the sequence since is not resumed."""
        pause = await self._active(pause_id)
        pause_id = pause["id"]
        prospect = await self.state.get_prospect(pause["prospect_id"])
        over = ""
        if prospect is None:
            over = "the contact no longer exists"
        elif prospect.status in ooo.SEQUENCE_OVER:
            over = f"their status is '{prospect.status}'"
        elif (prospect.email_status or "") == "invalid":
            over = "their address is invalid"
        elif await self.policy.exclusion_for(prospect.email):
            over = "their address is excluded"
        now = self.clock().isoformat()
        note = (note or "").strip()[:MAX_NOTE]
        email = prospect.email if prospect else ""
        if over:
            await self.state.end_pause(pause["prospect_id"], reason=over,
                                       actor=actor or "dashboard", now=now)
            await self.state.log_action("ooo_pause_ended", actor or "dashboard", {
                "prospect_id": pause["prospect_id"], "prospect_email": email,
                "pause_id": pause_id, "reason": over})
            raise PauseError("over", f"Nothing to resume: {over}, so their sequence is over.")
        ended, moved = await self.state.resume_pause(
            pause_id, start_at=now, actor=actor or "dashboard",
            reason=note or "resumed by hand", now=now)
        if ended is None:
            raise PauseError("not_found", f"No active pause {pause_id!r}.")
        await self.state.log_action("ooo_resumed", actor or "dashboard", {
            "prospect_id": pause["prospect_id"], "prospect_email": email,
            "pause_id": pause_id, "rescheduled": moved, "by": "hand", "note": note})
        await self.state.log_action("sequence_resumed", actor or "dashboard", {
            "prospect_id": pause["prospect_id"], "by": "operator",
            "was": pause["state"], "resume_at": pause.get("resume_at") or "",
            "rescheduled": moved,
        })
        return {**self._public(ended), "rescheduled": moved, "success": True}
