"""Outbox review commands for the dashboard, the CLI and MCP.

Listing, approving, rejecting, editing, rescheduling, re-routing and
regenerating queued email.

Revisions. Every outbox row carries a ``revision``. A change to what a
reviewer reads (text, recipient, sending mailbox, a send time an operator
picks, a regenerated draft) is a new revision, and an approved email goes
back to review: its approval was for the earlier content. Every review
command names the revision it was decided on (``expected_revision``) and
fails with ``stale_revision`` when the row has moved on, so one reviewer
cannot approve or overwrite a change they have not seen. Approval records
the revision and a hash of the content it covered, and the sender's claim
checks both (StateManager.claim_outbox_item).

Batches are explicit and frozen: a list of {id, revision} pairs decided up
front. "Approve all" is the same thing, built from what the caller showed;
an email that changed after that list was made is left for review.

Every command is audited and may carry a request key for idempotent replay
(control/audit.py). Failures raise ControlError subclasses with stable
codes: not_found, not_pending, not_queued, not_editable, started_sending,
stale_revision, revision_required, invalid_revision, unknown_mailbox,
prospect_not_found, conversation_not_found, provider_failed, flagged.

Flagged drafts. A draft that broke a deterministic rule (over its step's word
limit, a generic greeting) is queued with ``flags``, its ``word_count`` and
``word_limit`` (mercury/draft_rules.py), and is never approved by policy. A
person approves it only by choosing to: ``approve(..., approve_flagged=True)``
(HTTP: ``"approve_flagged": true``; batches name it per item). Without that
choice the command fails with ``flagged`` and the draft stays in review, so
"approve all" can never carry one through. Editing or regenerating a draft
measures it again and drops any earlier acceptance.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from mercury.control.audit import run_command
from mercury.control.errors import Conflict, Invalid, NotFound, Unavailable
from mercury.draft_rules import count_words, decode_flags, flag_details, word_limit

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = ("pending_review", "approved")
REJECTABLE_STATUSES = (*EDITABLE_STATUSES, "blocked")
SUBJECT_MAX, BODY_MAX, INSTRUCTION_MAX = 200, 4000, 500
BATCH_MAX = 200
BATCH_ACTIONS = ("approve", "reject")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def expected(value) -> int:
    """The revision a command was decided on: a positive whole number."""
    if value is None or value == "":
        raise Invalid("give the revision you reviewed (revision)", code="revision_required")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Invalid("revision must be a positive whole number", code="invalid_revision")
    return value


def frozen_items(items) -> list[tuple[str, int]]:
    """A batch as (id, revision) pairs: [{"id": ..., "revision": ...}, ...]."""
    if not isinstance(items, list) or not all(
            isinstance(i, dict) and isinstance(i.get("id"), str) and i["id"] for i in items):
        raise Invalid('items must be a list of {"id": ..., "revision": ...}')
    pairs: dict[str, int] = {}
    for i in items:
        revision = expected(i.get("revision"))
        if pairs.setdefault(i["id"], revision) != revision:
            raise Invalid(f"{i['id']} is listed with two revisions", code="invalid_revision")
    if not pairs or len(pairs) > BATCH_MAX:
        raise Invalid(f"give between 1 and {BATCH_MAX} items")
    return list(pairs.items())


def accepted_flags(items) -> set[str]:
    """Ids in a batch whose reviewer chose to approve them flagged."""
    return {i["id"] for i in items or [] if isinstance(i, dict) and i.get("approve_flagged") is True
            and isinstance(i.get("id"), str)}


def with_draft_checks(rows: list[dict]) -> list[dict]:
    """Give each row what the review desk shows about its rules: the
    ``word_count`` (measured now when an old row has none), the ``word_limit``
    it is held to (0 = none), ``flags`` (codes), ``flag_details`` ({code,
    label}) and ``needs_flag_approval``: flagged and not yet accepted by a
    person, so approving it takes ``approve_flagged``."""
    for row in rows:
        flags = decode_flags(row.get("flags"))
        row["flags"] = flags
        row["flag_details"] = flag_details(flags)
        if row.get("word_count") is None:
            row["word_count"] = count_words(row.get("body") or "")
        row["word_limit"] = int(row.get("word_limit") or 0)
        row["needs_flag_approval"] = bool(flags) and not row.get("flags_accepted_by")
    return rows


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

    def _with_wire_subject(self, rows: list[dict]) -> list[dict]:
        """Expose the actual reply subject consistently to every client."""
        from mercury.state import wire_subject

        email = getattr(getattr(self.config, "channels", None), "email", None)
        threaded = getattr(email, "thread_followups", True)
        for row in rows:
            row["wire_subject"] = wire_subject(row, threaded)
        return rows

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
        from mercury.demos import annotate_outbox, waiting_for_demo
        from mercury.holds import blocking

        state = self.state
        legacy, known = self._mailboxes()
        pending = await state.get_outbox(status="pending_review", limit=100)
        approved = await state.get_outbox(status="approved", limit=50)
        await annotate_outbox(state, self.config, pending + approved)
        data = {
            "paused": (await blocking(state))[1],
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
            # The rolling day the send caps count (sent_24h on /api/mailboxes).
            "sent_today": await with_from_mailbox(state, await self._rows(
                "SELECT * FROM outbox WHERE status = 'sent' AND replace(sent_at, 'T', ' ') >= "
                "strftime('%Y-%m-%d %H:%M:%S', 'now', '-24 hours') "
                "ORDER BY sent_at DESC LIMIT 200"), legacy),
            "failed": await self._rows(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25"),
        }
        from mercury.offers import annotate_outbox as annotate_offers
        from mercury.pains import annotate_outbox as annotate_pains

        from mercury.outbox_context import annotate_contacts, annotate_review

        buckets = ("pending", "approved", "blocked", "sending", "sent", "sent_today", "failed")
        for bucket in buckets:
            self._with_wire_subject(with_draft_checks(data[bucket]))
            await annotate_offers(state, self.config, data[bucket])
        await annotate_pains(state, [row for bucket in buckets for row in data[bucket]])
        await annotate_review(state, self.config, data["pending"] + data["approved"])
        await annotate_contacts(state, [row for bucket in buckets[2:] for row in data[bucket]])
        return data

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
        from mercury.offers import annotate_outbox as annotate_offers

        legacy, known = self._mailboxes()
        rows = await with_from_mailbox(self.state, [item], legacy, known)
        await annotate_offers(self.state, self.config, rows)
        from mercury.pains import annotate_outbox as annotate_pains

        await annotate_pains(self.state, rows)
        from mercury.outbox_context import annotate_review

        await annotate_review(self.state, self.config, rows)
        return self._with_wire_subject(with_draft_checks(rows))[0]

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

    def _approver(self) -> str:
        return f"{self.ctx.client}:{self.ctx.operator}"

    async def _run(self, action: str, scope: str, params: dict, work,
                   object_id: str = "", revision=None):
        return await run_command(self.state, self.ctx, f"outbox.{action}", scope=scope,
                                 params=params, work=work, object_type="outbox",
                                 object_id=object_id, revision_before=revision)

    @staticmethod
    def _current(item: dict, revision: int) -> None:
        now = int(item.get("revision") or 1)
        if now != revision:
            raise Conflict(f"this email changed since revision {revision} (now {now}); "
                           "reload it and review again", code="stale_revision", revision=now)

    async def _lost_race(self, item_id: str, revision: int, statuses: tuple, code: str, what: str):
        """A guarded UPDATE matched nothing: say why, from the row as it is now."""
        item = await self._item(item_id)
        if item["status"] not in statuses:
            raise Conflict(what.format(status=item["status"]), code=code, status=item["status"])
        self._current(item, revision)
        raise Conflict("this email changed while the command ran; reload it",
                       code="stale_revision", revision=int(item.get("revision") or 1))

    async def _approve_one(self, item_id: str, revision: int, trail, approve_flagged: bool = False) -> dict:
        item = await self._item(item_id)
        if item["status"] != "pending_review":
            raise Conflict(f"only emails awaiting review can be approved; this one is {item['status']}",
                           code="not_pending", status=item["status"])
        self._current(item, revision)
        flags = decode_flags(item.get("flags"))
        if flags and not approve_flagged:
            raise Conflict("this draft is flagged (" + ", ".join(d["label"].lower() for d in flag_details(flags))
                           + "). Edit it, regenerate it, or approve it anyway.",
                           code="flagged", flags=flags, word_count=item.get("word_count"),
                           word_limit=int(item.get("word_limit") or 0))
        if not await self.state.approve_outbox(item_id, revision, approved_by=self._approver(),
                                               accept_flags=approve_flagged):
            await self._lost_race(item_id, revision, ("pending_review",), "not_pending",
                                  "only emails awaiting review can be approved; this one is {status}")
        followups = await self._promote_followups(item)
        trail.record(item_id, revision, revision, followups_approved=followups,
                     **({"flags_accepted": flags} if flags else {}))
        return {"id": item_id, "approved": 1, "followups_approved": followups, "revision": revision,
                **({"flags_accepted": flags} if flags else {})}

    async def _reject_one(self, item_id: str, revision: int, trail) -> dict:
        item = await self._item(item_id)
        if item["status"] not in REJECTABLE_STATUSES:
            raise Conflict(f"only queued emails can be rejected; this one is {item['status']}",
                           code="not_queued", status=item["status"])
        self._current(item, revision)
        n = await self.state.reject_outbox_item(item_id, revision)
        if not n:
            await self._lost_race(item_id, revision, REJECTABLE_STATUSES, "not_queued",
                                  "only queued emails can be rejected; this one is {status}")
        trail.record(item_id, revision, revision, rejected=n)
        return {"id": item_id, "rejected": n, "revision": revision}

    async def approve(self, item_id: str, expected_revision=None, approve_flagged: bool = False) -> dict:
        """Approve one email awaiting review, at the revision the reviewer read.
        A flagged draft needs ``approve_flagged=True`` (see the module notes)."""
        async def work(trail):
            await self._item(item_id)
            return await self._approve_one(item_id, expected(expected_revision), trail,
                                           approve_flagged is True)
        return await self._run("approve", "approve", {"id": item_id, "revision": expected_revision,
                                                      "approve_flagged": approve_flagged is True},
                               work, item_id, expected_revision)

    async def reject(self, item_id: str, expected_revision=None) -> dict:
        """Reject one queued email, and the later steps of its sequence."""
        async def work(trail):
            await self._item(item_id)
            return await self._reject_one(item_id, expected(expected_revision), trail)
        return await self._run("reject", "approve", {"id": item_id, "revision": expected_revision},
                               work, item_id, expected_revision)

    async def _batch(self, action: str, pairs: list[tuple[str, int]], trail,
                     flagged_ok: frozenset | set = frozenset()) -> dict:
        trail.batch_id = uuid.uuid4().hex[:12]
        results, done = [], 0
        for item_id, revision in pairs:
            try:
                if action == "approve":
                    outcome = await self._approve_one(item_id, revision, trail, item_id in flagged_ok)
                else:
                    outcome = await self._reject_one(item_id, revision, trail)
                results.append({"id": item_id, "ok": True, **outcome})
                done += 1
            except (NotFound, Conflict) as error:
                trail.record(item_id, revision, None, outcome=error.code, message=str(error))
                results.append({"id": item_id, "ok": False, "code": error.code, "message": str(error),
                                **error.details})
        return {"action": action, "batch_id": trail.batch_id, "succeeded": done,
                "failed": len(pairs) - done, "results": results}

    async def batch(self, action: str, items) -> dict:
        """Approve or reject an explicit, frozen list of {id, revision}. Each
        item succeeds or fails on its own; one that changed since the list
        was made fails with stale_revision and is left as it is. An item may
        add ``"approve_flagged": true`` to approve a flagged draft; one that
        does not fails with ``flagged``."""
        async def work(trail):
            if action not in BATCH_ACTIONS:
                raise Invalid(f"action must be one of {', '.join(BATCH_ACTIONS)}")
            return await self._batch(action, frozen_items(items), trail, accepted_flags(items))
        return await self._run(f"batch_{action}" if action in BATCH_ACTIONS else "batch",
                               "approve", {"action": action, "items": items}, work)

    async def approve_all(self, items) -> dict:
        """Approve every email in ``items``: the {id, revision} list of what
        the reviewer was shown (pending_snapshot builds one). Never "whatever
        is pending now": anything queued or changed since stays in review."""
        async def work(trail):
            result = await self._batch("approve", frozen_items(items), trail, accepted_flags(items))
            followups = sum(r.get("followups_approved", 0) for r in result["results"] if r["ok"])
            return {**result, "approved": result["succeeded"], "followups_approved": followups}
        return await self._run("approve_all", "approve", {"items": items}, work)

    async def pending_snapshot(self, limit: int = BATCH_MAX) -> list[dict]:
        """What is awaiting review right now, as a frozen batch to approve."""
        self.ctx.require("read")
        rows = await self.state.get_outbox(status="pending_review", limit=min(limit, BATCH_MAX))
        return [{"id": r["id"], "revision": int(r.get("revision") or 1)} for r in rows]

    # ── Edits ──
    #
    # Each one is a new revision. An approved email goes back to review.

    async def _editable(self, item_id: str, verb: str, revision: int) -> dict:
        item = await self._item(item_id)
        if item.get("status") not in EDITABLE_STATUSES:
            raise Conflict(f"only pending or approved drafts can be {verb}",
                           code="not_editable", status=item.get("status"))
        self._current(item, revision)
        return item

    async def _revise(self, item: dict, revision: int, trail, **changes) -> dict:
        new = await self.state.revise_outbox_item(item["id"], revision, **changes)
        if new is None:
            await self._lost_race(item["id"], revision, EDITABLE_STATUSES, "started_sending",
                                  "this draft has started sending")
        was_approved = item.get("status") == "approved"
        trail.record(item["id"], revision, new, approval_cleared=was_approved,
                     fields=sorted(k for k in changes if k not in ("manually_edited", "word_limit")))
        current = with_draft_checks([await self._item(item["id"])])[0]
        return {"id": item["id"], "revision": new, "status": "pending_review",
                "approval_cleared": was_approved,
                **{k: current[k] for k in ("word_count", "word_limit", "flags", "flag_details",
                                           "needs_flag_approval")}}

    async def edit(self, item_id: str, subject: str, body: str, expected_revision=None) -> dict:
        """The reviewer edits a draft in place. An approved draft goes back to review."""
        async def work(trail):
            await self._item(item_id)
            revision = expected(expected_revision)
            text, message = (subject or "").strip(), (body or "").strip()
            if not text or not message:
                raise Invalid("subject and body are required")
            if len(text) > SUBJECT_MAX or len(message) > BODY_MAX:
                raise Invalid(f"subject is limited to {SUBJECT_MAX} characters and body to {BODY_MAX}")
            item = await self._editable(item_id, "edited", revision)
            # Measured again against the limit this config sets for the step.
            limit = int(item.get("word_limit") or 0)
            if self.config is not None and item.get("kind") != "reply":
                limit = word_limit(self.config, item.get("step") or 1)
            return await self._revise(item, revision, trail, subject=text, body=message,
                                      manually_edited=1, word_limit=limit)
        return await self._run("edit", "edit", {"id": item_id, "subject": subject, "body": body,
                                                "revision": expected_revision},
                               work, item_id, expected_revision)

    async def reschedule(self, item_id: str, send_at: datetime, expected_revision=None) -> dict:
        """Move a queued email to a new send time (naive UTC). An approved
        email goes back to review: it was approved to go out at another time."""
        async def work(trail):
            item = await self._item(item_id)
            revision = expected(expected_revision)
            if item.get("status") not in EDITABLE_STATUSES:
                raise Conflict(f"cannot reschedule an email that is {item.get('status')}",
                               code="not_editable", status=item.get("status"))
            self._current(item, revision)
            if send_at < _utc_naive_now() - timedelta(minutes=1):
                raise Invalid("send_at is in the past")
            normalized = send_at.isoformat(timespec="seconds")
            result = await self._revise(item, revision, trail, send_at=normalized)
            try:
                await self.state.log_action("outbox_reschedule", self.ctx.actor, {
                    "outbox_id": item_id, "from": item.get("send_at"), "to": normalized,
                })
            except Exception as e:
                # The move happened; a missing log line must not undo it.
                logger.debug("reschedule log_action failed: %s", e)
            return {**result, "send_at": normalized}
        return await self._run("reschedule", "edit", {"id": item_id, "send_at": send_at,
                                                      "revision": expected_revision},
                               work, item_id, expected_revision)

    async def reroute(self, item_id: str, expected_revision=None, to_email: str | None = None,
                      mailbox: str | None = None) -> dict:
        """Change who an email goes to, or the mailbox it goes out from
        ('' lets the sender pick). Either way it goes back to review."""
        async def work(trail):
            await self._item(item_id)
            revision = expected(expected_revision)
            changes = {}
            if to_email is not None:
                address = str(to_email).strip().lower()
                if not EMAIL_RE.match(address) or len(address) > 254:
                    raise Invalid("to_email must be an email address", code="invalid_value",
                                  field="to_email")
                changes["to_email"] = address
            if mailbox is not None:
                sender = str(mailbox).strip().lower()
                if sender and not EMAIL_RE.match(sender):
                    raise Invalid("mailbox must be an email address, or empty to rotate",
                                  code="invalid_value", field="mailbox")
                known = self._mailboxes()[1]
                if sender and known is not None and sender not in {m.lower() for m in known}:
                    raise Invalid(f"{sender} is not a configured mailbox", code="unknown_mailbox")
                changes["mailbox"] = sender
            if not changes:
                raise Invalid("give to_email, mailbox or both")
            item = await self._editable(item_id, "re-routed", revision)
            return {**await self._revise(item, revision, trail, **changes), **changes}
        return await self._run("reroute", "edit", {"id": item_id, "to_email": to_email,
                                                   "mailbox": mailbox, "revision": expected_revision},
                               work, item_id, expected_revision)

    async def regenerate(self, item_id: str, instruction: str = "", expected_revision=None) -> dict:
        """Ask the writer for a new draft, optionally with an instruction. One
        model call. The new draft is a new revision and goes back to review."""
        async def work(trail):
            await self._item(item_id)
            return await self._regenerate(item_id, instruction, expected(expected_revision), trail)
        return await self._run("regenerate", "edit", {"id": item_id, "instruction": instruction,
                                                      "revision": expected_revision},
                               work, item_id, expected_revision)

    async def _regenerate(self, item_id: str, instruction: str, revision: int, trail) -> dict:
        instruction = (instruction or "").strip()
        if len(instruction) > INSTRUCTION_MAX:
            raise Invalid(f"instruction is limited to {INSTRUCTION_MAX} characters")
        state = self.state
        # Checked before the model call: a stale request should not spend one.
        item = await self._editable(item_id, "regenerated", revision)
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
            await PersonaStore(state).replace_draft(item_id, draft, expected_revision=revision)
        except ValueError:
            await self._lost_race(item_id, revision, EDITABLE_STATUSES, "not_editable",
                                  "only pending or approved drafts can be regenerated")
        updated = await state.get_outbox_item(item_id)
        trail.record(item_id, revision, updated.get("revision"),
                     approval_cleared=item.get("status") == "approved",
                     generation_id=updated.get("generation_id") or "")
        from mercury.pains import annotate_outbox as annotate_pains

        rows = await annotate_pains(state, await PersonaStore(state).enrich([updated]))
        return self._with_wire_subject(with_draft_checks(rows))[0]
