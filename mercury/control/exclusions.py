"""Exclusions and company holds for the dashboard and the CLI.

An exclusion (a row in ``suppressions``) stops every Mercury email to an
address or a domain until someone lifts it. A company hold pauses only cold
mail to one company; replies to people who wrote back still go.

Every change records who made it and why. Lifting something never approves
mail: an email an exclusion blocked goes back to review, never straight out.
Failures raise ExclusionError with a stable code.
"""

from __future__ import annotations

import csv
import io

from mercury.csv_import import ImportFileError, read_csv
from mercury.policy import (
    PROTECTED_SOURCES, SOURCE_LABELS, ContactPolicy, capability, describe_rule,
    normalize_rule_value,
)

KINDS = ("email", "domain")
EXPORT_COLUMNS = ("kind", "value", "include_subdomains", "source", "reason", "created_by",
                  "created_at", "removed_at", "removed_by", "removed_note")
MAX_REASON = 300
TRUE_WORDS = {"1", "true", "yes", "y", "x"}


class ExclusionError(ValueError):
    """Codes: invalid, not_found, protected, not_blocked, still_excluded, bad_file."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _public(rule: dict) -> dict:
    return {**rule, "label": SOURCE_LABELS.get(rule["source"], rule["source"]),
            "description": describe_rule(rule),
            "protected": rule["source"] in PROTECTED_SOURCES}


class ExclusionService:
    def __init__(self, state, config=None):
        self.state, self.config = state, config
        self.policy = ContactPolicy(state, config)

    async def ready(self):
        await self.state.init_db()
        return self

    # ── Exclusions ──

    async def list(self, query: str = "", source: str = "", removed: bool = False,
                   limit: int = 500) -> list[dict]:
        rows = await self.state.list_suppressions(query, source, removed, limit)
        return [_public(r) for r in rows]

    async def get(self, rule_id: str) -> dict:
        rule = await self.state.get_suppression(rule_id)
        if not rule:
            raise ExclusionError("not_found", f"No exclusion {rule_id!r}.")
        return {**_public(rule), "events": await self.state.suppression_events(rule_id)}

    async def check(self, email: str) -> dict:
        """Whether this address is excluded, and by what."""
        rules = await self.state.find_suppressions(email)
        return {"email": email.strip().lower(), "excluded": bool(rules),
                "rules": [_public(r) for r in rules]}

    async def add(self, kind: str, value: str, *, reason: str = "",
                  include_subdomains: bool = False, actor: str = "",
                  source: str = "manual") -> dict:
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise ExclusionError("invalid", "Choose email or domain.")
        normalized = normalize_rule_value(kind, value)
        if not normalized:
            what = "an email address" if kind == "email" else "a domain like acme.com"
            raise ExclusionError("invalid", f"{value.strip()[:80]!r} is not {what}.")
        if include_subdomains and kind != "domain":
            raise ExclusionError("invalid", "Only a domain rule can include subdomains.")
        rule, created = await self.state.add_suppression(
            kind, normalized, source=source, reason=(reason or "").strip()[:MAX_REASON],
            include_subdomains=include_subdomains, actor=actor)
        return {**_public(rule), "created": created}

    async def remove(self, rule_id: str, *, note: str = "", actor: str = "",
                     confirm_opt_out: bool = False) -> dict:
        """Lift one rule. An opt-out is the recipient's own request, so lifting
        it takes an explicit confirmation and a note saying why."""
        rule = await self.state.get_suppression(rule_id)
        if not rule or rule["removed_at"]:
            raise ExclusionError("not_found", f"No active exclusion {rule_id!r}.")
        note = (note or "").strip()[:MAX_REASON]
        if rule["source"] in PROTECTED_SOURCES and not (confirm_opt_out and note):
            raise ExclusionError(
                "protected",
                "This person opted out. Lift it only if they asked to hear from you again, "
                "confirm that explicitly, and write down why.")
        removed = await self.state.remove_suppression(rule_id, actor=actor, note=note)
        if removed is None:
            raise ExclusionError("not_found", f"No active exclusion {rule_id!r}.")
        others = await self.state.find_suppressions(
            rule["value"] if rule["kind"] == "email" else f"x@{rule['value']}")
        return {**_public(removed),
                "still_excluded_by": [_public(r) for r in others]}

    # ── CSV ──

    async def export_csv(self, removed: bool = False) -> str:
        rows = await self.state.list_suppressions(removed=False, limit=1_000_000)
        if removed:
            rows += await self.state.list_suppressions(removed=True, limit=1_000_000)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(EXPORT_COLUMNS)
        for r in rows:
            writer.writerow([r.get(c) if r.get(c) is not None else "" for c in EXPORT_COLUMNS])
        return out.getvalue()

    async def import_csv(self, data: bytes, *, actor: str = "", reason: str = "") -> dict:
        """Add a rule per row. Columns: one of value / email / domain, plus
        optional kind, include_subdomains and reason. An import only ever
        adds: it never lifts or narrows a rule that already exists."""
        try:
            parsed = read_csv(data)
        except ImportFileError as error:
            raise ExclusionError("bad_file", str(error)) from error
        headers = {h.strip().lower(): h for h in parsed.headers}
        value_col = next((headers[k] for k in ("value", "email", "domain", "address")
                          if k in headers), None)
        if value_col is None:
            raise ExclusionError("bad_file",
                                 "The file needs a column named value, email or domain.")
        kind_col = headers.get("kind") or headers.get("type")
        subs_col = headers.get("include_subdomains") or headers.get("subdomains")
        reason_col = headers.get("reason") or headers.get("note")
        default_kind = ("domain" if headers.get("domain") == value_col else
                        "email" if headers.get("email") == value_col else "")
        added = existing = 0
        invalid: list[dict] = []
        for row_number, raw in parsed.rows:
            value = (raw.get(value_col) or "").strip()
            if not value:
                continue
            kind = ((raw.get(kind_col) or "").strip().lower() if kind_col else "") \
                or default_kind or ("email" if "@" in value.strip("@") else "domain")
            subdomains = kind == "domain" and subs_col is not None and \
                (raw.get(subs_col) or "").strip().lower() in TRUE_WORDS
            row_reason = ((raw.get(reason_col) or "").strip() if reason_col else "") or reason
            try:
                result = await self.add(kind, value, reason=row_reason or "imported",
                                        include_subdomains=subdomains, actor=actor,
                                        source="import")
            except ExclusionError as error:
                invalid.append({"row": row_number, "value": value[:120], "error": str(error)})
                continue
            if result["created"]:
                added += 1
            else:
                existing += 1
        return {"added": added, "already_excluded": existing, "invalid": invalid[:200],
                "invalid_count": len(invalid)}

    # ── Blocked mail ──

    async def requeue(self, item_id: str) -> dict:
        """A blocked email goes back to review (never straight to approved)."""
        result = await self.state.requeue_blocked_outbox(item_id)
        if result == "not_blocked":
            raise ExclusionError("not_blocked", "That email is not blocked.")
        if result == "excluded":
            item = await self.state.get_outbox_item(item_id)
            verdict = await self.policy.explain(item)
            raise ExclusionError("still_excluded",
                                 verdict.reason + " Lift that rule first.")
        await self.state.log_action("outbox_requeue", "dashboard", {"outbox_id": item_id})
        return {"id": item_id, "status": "pending_review"}

    # ── Company holds ──

    async def holds(self, released: bool = False) -> list[dict]:
        rows = await self.state.list_company_holds(released=released)
        for r in rows:
            r["reason_text"] = _hold_text(r)
        return rows

    async def hold(self, company_id: str, *, note: str = "", actor: str = "") -> dict:
        if not company_id or not await self.state.get_company(company_id):
            raise ExclusionError("not_found", f"No company {company_id!r}.")
        hold, created = await self.state.hold_company(
            company_id, reason="manual", note=(note or "").strip()[:MAX_REASON], actor=actor)
        if created:
            await self.state.log_action("company_hold", actor or "dashboard",
                                        {"company_id": company_id, "hold_id": hold["id"],
                                         "note": hold["note"]})
        return {**hold, "created": created}

    async def release(self, hold_id: str, *, note: str = "", actor: str = "") -> dict:
        """Resume cold mail to a company. Held emails keep their status: what
        was approved goes out, what was waiting for review still waits."""
        released = await self.state.release_company_hold(
            hold_id, actor=actor, note=(note or "").strip()[:MAX_REASON])
        if released is None:
            raise ExclusionError("not_found", f"No active hold {hold_id!r}.")
        await self.state.log_action("company_resume", actor or "dashboard",
                                    {"company_id": released["company_id"], "hold_id": hold_id,
                                     "note": released["released_note"]})
        return released

    # ── Policy summary ──

    def settings(self) -> dict:
        limits = self.policy.limits
        return {
            "max_new_contacts_per_company_per_day": limits.max_new_per_day,
            "max_active_contacts_per_company": limits.max_active,
            "pause_company_on_reply": limits.pause_on_reply,
            "daily_window": "rolling 24 hours, the same window as max_daily_sends",
            "capability": capability(self.config) if self.config is not None else {},
        }


def _hold_text(hold: dict) -> str:
    if hold.get("reason") == "reply":
        who = hold.get("prospect_name") or hold.get("prospect_email") or "Someone"
        return f"{who} replied."
    return "Paused by you." + (f" {hold['note']}" if hold.get("note") else "")
