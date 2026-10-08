"""Outbox review commands for the dashboard, the CLI and MCP.

Listing, approving, rejecting, editing, rescheduling and regenerating queued
email. Approval and rejection apply to explicit ids; "approve all" exists for
the dashboard button and the CLI flag, and a batch is a list of ids decided
up front, never a filter re-evaluated later. Failures raise ControlError
subclasses with stable codes: not_found, not_pending, not_queued,
not_editable, started_sending, prospect_not_found, conversation_not_found,
provider_failed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from mercury.control.errors import Conflict, Invalid, NotFound, Unavailable

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = ("pending_review", "approved")
SUBJECT_MAX, BODY_MAX, INSTRUCTION_MAX = 200, 4000, 500
BATCH_MAX = 200
BATCH_ACTIONS = ("approve", "reject")


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def with_from_mailbox(state, rows: list[dict], legacy_email: str = "",
                            known: set[str] | None = None) -> list[dict]:
    """Add ``from_mailbox``: the address an email goes (or went) out from,
    resolved the way the sender resolves it. A follow-up inherits its
    opener's mailbox, '' on an old thread means the legacy mailbox, and a
    new thread whose opener has not gone out yet stays '' (it rotates)."""
    need = [r.get("campaign_id") or "" for r in rows
            if not r.get("mailbox") and r.get("kind") == "sequence"
            and int(r.get("step") or 1) > 1]
    threads = await state.get_thread_mailboxes(need)
    for r in rows:
        fm = r.get("mailbox") or ""
        if not fm:
            if r.get("status") == "sent" or r.get("kind") == "reply":
                fm = legacy_email
            elif r.get("kind") == "sequence" and int(r.get("step") or 1) > 1:
                key = (r.get("campaign_id") or "", r.get("prospect_id") or "")
                if key in threads:
                    fm = threads[key] or legacy_email
        r["from_mailbox"] = fm
        # Queued mail pinned to a mailbox no longer configured is held by
        # the sender (never re-routed); say so in the UI.
        r["from_removed"] = bool(fm and known is not None and fm not in known
                                 and r.get("status") != "sent")
    from mercury.personas import PersonaStore
    return await PersonaStore(state).enrich(rows)


class OutboxService:
    def __init__(self, ctx, state, config=None, pool=None, env=None):
        # config None: mercury.yaml could not be read. The demo gate then
        # fails closed (every email with an offer shows held) and follow-ups
        # are not promoted on approval. pool None: no native mailboxes, so
        # from_mailbox falls back to what each row stores.
        self.ctx, self.state, self.config, self.pool, self.env = ctx, state, config, pool, env

    async def ready(self):
        await self.state.init_db()
        return self

    # ── Reads ──

    def _mailboxes(self) -> tuple[str, set[str] | None]:
        if self.pool is None:
            return "", None
        return self.pool.legacy.email, {mb.email for mb in self.pool.mailboxes}

    async def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def _with_policy(self, rows: list[dict]) -> list[dict]:
        """Keep exclusion and company-limit explanations on the review desk."""
        from mercury.policy import ContactPolicy

        policy = ContactPolicy(self.state, self.config)
        for row in rows:
            row["policy"] = (await policy.explain(row)).as_dict()
        return rows

    async def overview(self) -> dict:
        """The review desk: every queue the Outbox tab shows, plus the kill switch."""
        self.ctx.require("read")
        from mercury.bounces import KILL_SWITCH_KEY
        from mercury.demos import annotate_outbox, waiting_for_demo

        state = self.state
        legacy, known = self._mailboxes()
        pending = await state.get_outbox(status="pending_review", limit=100)
        approved = await state.get_outbox(status="approved", limit=50)
        await annotate_outbox(state, self.config, pending + approved)
        return {
            "paused": await state.get_setting(KILL_SWITCH_KEY),
            "pending": await self._with_policy(
                await with_from_mailbox(state, pending, legacy, known)),
            "approved": await self._with_policy(
                await with_from_mailbox(state, approved, legacy, known)),
            "blocked": await self._with_policy(await state.get_outbox(status="blocked", limit=50)),
            "waiting_demo": await waiting_for_demo(state, self.config),
            "sending": await with_from_mailbox(
                state, await state.get_outbox(status="sending", limit=50), legacy, known),
            "sent": await with_from_mailbox(state, await self._rows(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25"), legacy),
            "failed": await self._rows(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25"),
        }

    async def _item(self, item_id: str) -> dict:
        item = await self.state.get_outbox_item(item_id or "")
        if not item:
            raise NotFound("outbox item not found")
        return item

    async def get(self, item_id: str) -> dict:
        """One email as the review desk shows it: sender, persona and demo hold."""
        self.ctx.require("read")
        from mercury.demos import annotate_outbox

        item = await self._item(item_id)
        if item["status"] in EDITABLE_STATUSES:
            await annotate_outbox(self.state, self.config, [item])
        await self._with_policy([item])
        legacy, known = self._mailboxes()
        return (await with_from_mailbox(self.state, [item], legacy, known))[0]

    # ── Review ──

    async def _promote_followups(self, item: dict | None = None) -> int:
        """auto_approve_followups: promote right away on approval, so the
        follow-ups leave the review desk instead of waiting for the next cycle."""
        email = getattr(getattr(self.config, "channels", None), "email", None)
        if not getattr(email, "auto_approve_followups", False):
            return 0
        thread = {}
        if item and item.get("campaign_id"):
            thread = {"campaign_id": item["campaign_id"], "prospect_id": item.get("prospect_id") or ""}
        total = 0
        for _ in range(10):
            n = await self.state.approve_ready_followups(**thread)
            if not n:
                break
            total += n
        return total

    async def approve(self, item_id: str) -> dict:
        """Approve one email awaiting review."""
        self.ctx.require("approve")
        if not await self.state.approve_outbox(item_id or ""):
            item = await self._item(item_id)
            raise Conflict(f"only emails awaiting review can be approved; this one is {item['status']}",
                           code="not_pending", status=item["status"])
        followups = await self._promote_followups(await self.state.get_outbox_item(item_id))
        return {"id": item_id, "approved": 1, "followups_approved": followups}

    async def approve_all(self) -> dict:
        """Approve everything awaiting review right now."""
        self.ctx.require("approve")
        n = await self.state.approve_outbox()
        return {"approved": n, "followups_approved": await self._promote_followups()}

    async def reject(self, item_id: str) -> dict:
        """Reject one queued email, and the later steps of its sequence."""
        self.ctx.require("approve")
        item = await self._item(item_id)
        n = await self.state.reject_outbox_item(item_id)
        if not n:
            raise Conflict(f"only queued emails can be rejected; this one is {item['status']}",
                           code="not_queued", status=item["status"])
        return {"id": item_id, "rejected": n}

    async def batch(self, action: str, ids: list[str]) -> dict:
        """Approve or reject an explicit list of ids. Each id succeeds or
        fails on its own; the list is fixed when the call is made."""
        self.ctx.require("approve")
        if action not in BATCH_ACTIONS:
            raise Invalid(f"action must be one of {', '.join(BATCH_ACTIONS)}")
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise Invalid("ids must be a list of outbox ids")
        ids = list(dict.fromkeys(ids))
        if not ids or len(ids) > BATCH_MAX:
            raise Invalid(f"give between 1 and {BATCH_MAX} ids")
        command = self.approve if action == "approve" else self.reject
        results, done = [], 0
        for item_id in ids:
            try:
                results.append({"id": item_id, "ok": True, **await command(item_id)})
                done += 1
            except (NotFound, Conflict) as error:
                results.append({"id": item_id, "ok": False, "code": error.code, "message": str(error)})
        return {"action": action, "succeeded": done, "failed": len(ids) - done, "results": results}

    # ── Edits ──

    async def _editable(self, item_id: str, verb: str) -> dict:
        item = await self._item(item_id)
        if item.get("status") not in EDITABLE_STATUSES:
            raise Conflict(f"only pending or approved drafts can be {verb}",
                           code="not_editable", status=item.get("status"))
        return item

    async def edit(self, item_id: str, subject: str, body: str) -> dict:
        """The reviewer edits a draft in place. Approved mail stays approved."""
        self.ctx.require("edit")
        subject, body = (subject or "").strip(), (body or "").strip()
        if not subject or not body:
            raise Invalid("subject and body are required")
        if len(subject) > SUBJECT_MAX or len(body) > BODY_MAX:
            raise Invalid(f"subject is limited to {SUBJECT_MAX} characters and body to {BODY_MAX}")
        await self._editable(item_id, "edited")
        if not await self.state.edit_outbox_item(item_id, subject=subject, body=body, manually_edited=1):
            raise Conflict("this draft has started sending", code="started_sending")
        return {"id": item_id}

    async def reschedule(self, item_id: str, send_at: datetime) -> dict:
        """Move a queued email to a new send time (naive UTC)."""
        self.ctx.require("edit")
        item = await self._item(item_id)
        if item.get("status") not in EDITABLE_STATUSES:
            raise Conflict(f"cannot reschedule an email that is {item.get('status')}",
                           code="not_editable", status=item.get("status"))
        if send_at < _utc_naive_now() - timedelta(minutes=1):
            raise Invalid("send_at is in the past")
        normalized = send_at.isoformat(timespec="seconds")
        if not await self.state.edit_outbox_item(item_id, send_at=normalized):
            raise Conflict("this draft has started sending", code="started_sending")
        try:
            await self.state.log_action("outbox_reschedule", self.ctx.actor, {
                "outbox_id": item_id, "from": item.get("send_at"), "to": normalized,
            })
        except Exception as e:
            # The move happened; a missing log line must not undo it.
            logger.debug("reschedule log_action failed: %s", e)
        return {"id": item_id, "send_at": normalized}

    async def regenerate(self, item_id: str, instruction: str = "") -> dict:
        """Ask the writer for a new draft, optionally with an instruction. One
        model call. The new draft goes back to review."""
        self.ctx.require("edit")
        instruction = (instruction or "").strip()
        if len(instruction) > INSTRUCTION_MAX:
            raise Invalid(f"instruction is limited to {INSTRUCTION_MAX} characters")
        state = self.state
        item = await self._editable(item_id, "regenerated")
        prospect = await state.get_prospect(item["prospect_id"])
        if not prospect:
            raise NotFound("prospect not found", code="prospect_not_found")
        from mercury.agents.writer import Writer
        from mercury.brain import Brain
        from mercury.config import load_config, load_env
        from mercury.personas import PersonaStore

        config = self.config or load_config()
        env = self.env or load_env()
        if item.get("kind") == "reply":
            from mercury.agents.handler import Handler
            convo = await state.get_conversation(item.get("conversation_id") or "")
            if not convo:
                raise NotFound("conversation not found", code="conversation_not_found")
            handler = Handler(Brain(state), state, config, env)
            profile = await PersonaStore(state).for_generation(config, item.get("generation_id", ""))
            latest = next((m.content for m in reversed(convo.thread) if not m.is_ours), "")
            response = await handler._generate_response(
                convo.intent, latest, prospect, convo, instruction=instruction, profile=profile,
            )
            draft = {"subject": item["subject"], "body": response,
                     "generation_id": handler._response_generation_id} if response else None
        else:
            writer = Writer(Brain(state), state, config, env)
            draft = await writer.regenerate_email(item, prospect, instruction)
        if not draft:
            raise Unavailable("the writer returned nothing; try again", code="provider_failed")
        # A regenerated draft is unread: back to the review queue.
        try:
            await PersonaStore(state).replace_draft(item_id, draft)
        except ValueError as error:
            raise Conflict(str(error), code="not_editable") from error
        return (await PersonaStore(state).enrich([await state.get_outbox_item(item_id)]))[0]
