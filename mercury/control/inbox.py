"""The unified inbox: triage prospect conversations from every mailbox.

Reads. ``list`` pages through conversations in the database (filters,
search and facet counts are SQL, never "load everything and filter"), and
``thread`` assembles one contact's known history: emails that were really
sent (outbox rows with status 'sent') and messages that were received
(inbound_messages), each once, in order. Queued and failed drafts come back
separately and are never mixed into sent history. Conversations recorded
before inbound mail was stored keep their ``thread_json`` text, shown as
partial history: their messages carry no mailbox or ids, and a message of
ours found only there is "recorded", never presented as sent.

Local state. Read/unread, snoozes, contact notes and reminders live only in
Mercury's database. Nothing here changes the read flags in Gmail or the
IMAP server, and nothing here sends or queues mail: a due reminder only
flags its conversation in the inbox and on Today.

Compose. A reply is an outbox row like any other, so it goes through the
same review, revisions, approval snapshot and Sender gates (exclusions,
opt-outs, pauses, holds, the pre-send gate). It is bound to the contact,
the mailbox the conversation runs through, the message it answers and that
message's thread headers. Creating or opening a draft never sends: a draft
starts in review whatever ``require_approval`` says, and goes out only once
approved (or scheduled, which approves it for that time). Editing or
regenerating it is a new revision that needs approval again, and a save
naming an old revision fails with ``stale_revision``. Escalated and
opted-out conversations stay visible, but composing to them is refused.

Failures raise ControlError subclasses with stable codes: not_found,
invalid, compose_refused, draft_exists, stale_revision, not_editable,
confirmation_required, provider_failed (plus the outbox codes).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from mercury.control.audit import run_command
from mercury.control.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from mercury.control.outbox import (
    BODY_MAX, INSTRUCTION_MAX, SUBJECT_MAX, OutboxService, with_from_mailbox,
)

PAGE_DEFAULT, PAGE_MAX = 50, 200
BULK_MAX = 200
NOTE_MAX, REMINDER_NOTE_MAX, QUERY_MAX = 2000, 300, 200
SNIPPET = 160
BULK_ACTIONS = ("read", "unread", "snooze", "unsnooze", "exclude")
# Filters that pick one of the inbox's views (Needs you, Unread, Snoozed,
# All). Segment counts ignore them so every view shows its own count.
SEGMENT_FILTERS = ("read", "attention", "needs_you", "response", "snoozed", "reminder")
CLOSED_STAGES = ("closed_won", "closed_lost")
LOCAL_NOTE = ("Read, snooze, notes and reminders are kept in Mercury only. They do not "
              "change read flags or anything else in your mailbox.")

# Inbound rows that are not a message of their own in the thread.
_HIDDEN_INBOUND = ("duplicate",)
_DRAFTS = ("pending_review", "approved", "blocked", "sending")
_UNSENT = ("failed", "rejected", "cancelled")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def parse_time(value, field: str = "time") -> datetime:
    """An ISO time from a client as naive UTC. A zone-less value is UTC."""
    if isinstance(value, datetime):
        when = value
    else:
        text = str(value or "").strip()
        if not text:
            raise Invalid(f"{field} is required", code="invalid", field=field)
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise Invalid(f"{field} must be an ISO date and time", code="invalid",
                          field=field) from error
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def _sort_time(value) -> datetime:
    if not value:
        return datetime.min
    try:
        when = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return datetime.min
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def _norm_text(text: str) -> str:
    return " ".join((text or "").split())


def _snippet(text: str) -> str:
    from mercury.agents.handler import strip_quoted

    own = strip_quoted(text or "") or (text or "")
    flat = _norm_text(own)
    return flat if len(flat) <= SNIPPET else flat[:SNIPPET - 1].rstrip() + "…"


def _t(column: str) -> str:
    """SQL: a stored timestamp in one comparable form ('T', no zone)."""
    return f"REPLACE(COALESCE({column}, ''), ' ', 'T')"


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _values(raw) -> list[str]:
    """A filter value as a list: repeated params, or comma separated."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _flag(raw) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise Invalid("expected true or false", code="invalid")


# One row per conversation with everything the list filters and sorts on.
_ROWS_SQL = f"""
WITH conv AS (
  SELECT c.id, c.prospect_id, c.campaign_id, c.intent, c.stage, c.status,
         c.created_at, c.updated_at, c.thread_json,
         COALESCE(p.email, '') AS email, COALESCE(p.first_name, '') AS first_name,
         COALESCE(p.last_name, '') AS last_name, COALESCE(p.title, '') AS title,
         COALESCE(p.status, '') AS prospect_status, COALESCE(p.company_id, '') AS company_id,
         COALESCE(NULLIF(co.name, ''), p.company, '') AS company,
         COALESCE(co.domain, '') AS company_domain,
         s.read_at, s.snoozed_until, s.snoozed_at,
         (SELECT MAX(i.created_at) FROM inbound_messages i
           WHERE i.conversation_id = c.id AND i.status != 'duplicate') AS last_inbound_at,
         (SELECT i.mailbox FROM inbound_messages i
           WHERE i.conversation_id = c.id AND i.status != 'duplicate'
           ORDER BY i.created_at DESC, i.rowid DESC LIMIT 1) AS inbound_mailbox,
         (SELECT COALESCE(o.mailbox, '') FROM outbox o
           WHERE o.prospect_id = c.prospect_id AND c.prospect_id != '' AND o.status = 'sent'
           ORDER BY o.sent_at DESC LIMIT 1) AS sent_mailbox,
         (SELECT MAX(o.sent_at) FROM outbox o
           WHERE o.prospect_id = c.prospect_id AND c.prospect_id != ''
             AND o.status = 'sent') AS last_sent_at,
         EXISTS (SELECT 1 FROM outbox o WHERE o.conversation_id = c.id AND o.kind = 'reply'
                   AND o.status IN ({", ".join(f"'{s}'" for s in _DRAFTS)})) AS has_draft,
         EXISTS (SELECT 1 FROM outbox o WHERE o.conversation_id = c.id AND o.kind = 'reply'
                   AND o.status = 'pending_review') AS review_draft,
         (SELECT MIN(r.due_at) FROM inbox_reminders r
           WHERE r.conversation_id = c.id AND r.done_at IS NULL) AS next_reminder_at,
         CASE WHEN json_valid(c.thread_json)
              THEN json_extract(c.thread_json, '$[#-1].sender') END AS last_legacy_sender,
         CASE WHEN json_valid(c.thread_json)
              THEN json_array_length(c.thread_json) ELSE 0 END AS legacy_count
  FROM conversations c
  LEFT JOIN prospects p ON p.id = c.prospect_id
  LEFT JOIN companies co ON co.id = p.company_id AND p.company_id != ''
  LEFT JOIN inbox_state s ON s.conversation_id = c.id
),
flags AS (
  SELECT conv.*,
    COALESCE(NULLIF(inbound_mailbox, ''), NULLIF(sent_mailbox, ''), '') AS mailbox,
    substr(MAX({_t("created_at")}, {_t("last_inbound_at")}, {_t("last_sent_at")}), 1, 19)
      AS last_activity_at,
    CASE WHEN read_at IS NULL THEN 1
         WHEN {_t("COALESCE(last_inbound_at, created_at)")} > {_t("read_at")} THEN 1
         ELSE 0 END AS unread,
    CASE WHEN snoozed_until IS NOT NULL AND {_t("snoozed_until")} > :now
              AND NOT (last_inbound_at IS NOT NULL
                       AND {_t("last_inbound_at")} > {_t("snoozed_at")})
         THEN 1 ELSE 0 END AS snoozed,
    CASE WHEN status = 'needs_human' OR intent = 'escalate' THEN 1 ELSE 0 END AS needs_human,
    CASE WHEN prospect_status = 'opted_out' OR intent = 'unsubscribe' THEN 1 ELSE 0 END
      AS opted_out,
    CASE WHEN has_draft THEN 'draft'
         WHEN status != 'closed' AND (
              (last_inbound_at IS NOT NULL
               AND (last_sent_at IS NULL OR {_t("last_inbound_at")} > {_t("last_sent_at")}))
              OR (last_inbound_at IS NULL AND last_legacy_sender = 'prospect'))
         THEN 'awaiting' ELSE 'none' END AS response,
    CASE WHEN next_reminder_at IS NULL THEN 'none'
         WHEN {_t("next_reminder_at")} <= :now THEN 'due' ELSE 'scheduled' END AS reminder
  FROM conv
),
rows AS (
  -- "Needs you": a person must act. Escalated, a draft waiting for review,
  -- or a reminder whose time has come.
  SELECT flags.*,
    CASE WHEN needs_human = 1 OR review_draft = 1 OR reminder = 'due' THEN 1 ELSE 0 END
      AS needs_you
  FROM flags
)
"""

# Facet name -> the SQL value it groups by.
FACETS = {
    "mailbox": "mailbox",
    "intent": "intent",
    "stage": "stage",
    "prospect_status": "prospect_status",
    "status": "status",
    "read": "CASE WHEN unread THEN 'unread' ELSE 'read' END",
    "attention": "CASE WHEN needs_human THEN 'needs_human' ELSE 'none' END",
    "needs_you": "CASE WHEN needs_you THEN 'needs_you' ELSE 'none' END",
    "response": "response",
    "snoozed": "CASE WHEN snoozed THEN 'snoozed' ELSE 'active' END",
    "reminder": "reminder",
}


class InboxService:
    def __init__(self, ctx, state, config=None, pool=None, env=None):
        self.ctx, self.state, self.config, self.pool, self.env = ctx, state, config, pool, env
        self.clock = _utc_now

    async def ready(self):
        await self.state.init_db()
        return self

    def _outbox(self) -> OutboxService:
        return OutboxService(self.ctx, self.state, self.config, self.pool, self.env)

    def _require_approval(self) -> bool:
        email = getattr(getattr(self.config, "channels", None), "email", None)
        return bool(getattr(email, "require_approval", True))

    async def _rows(self, sql: str, params=()) -> list[dict]:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── The list ──

    def _filters(self, raw: dict) -> tuple[dict[str, tuple[str, dict]], dict]:
        """Validated filters as {name: (sql, params)}, plus their echo."""
        raw = raw or {}
        out: dict[str, tuple[str, dict]] = {}
        echo: dict = {}

        def one_of(name: str, column: str):
            values = _values(raw.get(name))
            if not values:
                return
            marks = {f"{name}{i}": v.lower() if name == "mailbox" else v
                     for i, v in enumerate(values)}
            out[name] = (f"{column} IN ({', '.join(':' + k for k in marks)})", marks)
            echo[name] = list(marks.values())

        one_of("mailbox", "mailbox")
        one_of("intent", "intent")
        one_of("stage", "stage")
        one_of("prospect_status", "prospect_status")
        one_of("status", "status")

        read = (raw.get("read") or "").strip().lower()
        if read:
            if read not in ("read", "unread"):
                raise Invalid("read must be read or unread", field="read")
            out["read"] = ("unread = 1" if read == "unread" else "unread = 0", {})
            echo["read"] = read
        attention = _flag(raw.get("attention"))
        if attention is not None:
            out["attention"] = (f"needs_human = {1 if attention else 0}", {})
            echo["attention"] = attention
        needs_you = _flag(raw.get("needs_you"))
        if needs_you is not None:
            out["needs_you"] = (f"needs_you = {1 if needs_you else 0}", {})
            echo["needs_you"] = needs_you
        response = (raw.get("response") or "").strip().lower()
        if response:
            if response not in ("draft", "awaiting", "none"):
                raise Invalid("response must be draft, awaiting or none", field="response")
            out["response"] = ("response = :response", {"response": response})
            echo["response"] = response
        snoozed = (raw.get("snoozed") or "exclude").strip().lower()
        if snoozed not in ("exclude", "only", "include"):
            raise Invalid("snoozed must be exclude, only or include", field="snoozed")
        if snoozed != "include":
            out["snoozed"] = ("snoozed = 0" if snoozed == "exclude" else "snoozed = 1", {})
        echo["snoozed"] = snoozed
        reminder = (raw.get("reminder") or "").strip().lower()
        if reminder:
            if reminder not in ("due", "scheduled", "any", "none"):
                raise Invalid("reminder must be due, scheduled, any or none", field="reminder")
            sql = "reminder != 'none'" if reminder == "any" else "reminder = :reminder"
            out["reminder"] = (sql, {"reminder": reminder})
            echo["reminder"] = reminder
        q = (raw.get("q") or "").strip()
        if q:
            if len(q) > QUERY_MAX:
                raise Invalid(f"search is limited to {QUERY_MAX} characters", field="q")
            out["q"] = (
                "(email LIKE :q ESCAPE '\\' OR (first_name || ' ' || last_name) LIKE :q ESCAPE '\\'"
                " OR company LIKE :q ESCAPE '\\' OR company_domain LIKE :q ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(thread_json)"
                "   THEN thread_json ELSE '[]' END) j"
                "   WHERE json_extract(j.value, '$.content') LIKE :q ESCAPE '\\')"
                " OR EXISTS (SELECT 1 FROM inbound_messages i WHERE (i.conversation_id = rows.id"
                "     OR (rows.prospect_id != '' AND i.prospect_id = rows.prospect_id))"
                "   AND (i.subject LIKE :q ESCAPE '\\' OR i.body LIKE :q ESCAPE '\\'))"
                " OR EXISTS (SELECT 1 FROM outbox o WHERE rows.prospect_id != ''"
                "   AND o.prospect_id = rows.prospect_id AND o.status = 'sent'"
                "   AND (o.subject LIKE :q ESCAPE '\\' OR o.body LIKE :q ESCAPE '\\'"
                "        OR o.thread_subject LIKE :q ESCAPE '\\')))",
                {"q": _like(q)})
            echo["q"] = q
        return out, echo

    @staticmethod
    def _where(filters: dict[str, tuple[str, dict]], skip: str = "") -> tuple[str, dict]:
        parts, params = [], {}
        for name, (sql, values) in filters.items():
            if name == skip:
                continue
            parts.append(sql)
            params.update(values)
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    async def list(self, raw_filters: dict | None = None, limit=PAGE_DEFAULT, offset=0) -> dict:
        """One page of conversations, newest activity first, with the total
        and facet counts. Each facet counts its values under every other
        filter (so picking one mailbox still shows how many the others have)."""
        self.ctx.require("read")
        try:
            limit, offset = int(limit), int(offset)
        except (TypeError, ValueError) as error:
            raise Invalid("limit and offset must be whole numbers") from error
        if not 1 <= limit <= PAGE_MAX or offset < 0:
            raise Invalid(f"limit must be 1 to {PAGE_MAX} and offset 0 or more")
        filters, echo = self._filters(raw_filters or {})
        now = {"now": _ts(self.clock())}
        where, params = self._where(filters)
        params = {**params, **now}
        total = (await self._rows(f"{_ROWS_SQL} SELECT COUNT(*) AS n FROM rows{where}",
                                  params))[0]["n"]
        page = await self._rows(
            f"{_ROWS_SQL} SELECT * FROM rows{where} "
            "ORDER BY last_activity_at DESC, id DESC LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset})
        facets = {}
        for name, expr in FACETS.items():
            f_where, f_params = self._where(filters, skip=name)
            counted = await self._rows(
                f"{_ROWS_SQL} SELECT {expr} AS value, COUNT(*) AS count FROM rows{f_where} "
                "GROUP BY 1 ORDER BY 2 DESC, 1", {**f_params, **now})
            facets[name] = [{"value": r["value"] or "", "count": r["count"]} for r in counted]
        segments = await self._segments(
            {k: v for k, v in filters.items() if k not in SEGMENT_FILTERS})
        items = await self._list_items(page)
        nxt = offset + len(items)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "next_offset": nxt if nxt < total else None, "filters": echo,
                "facets": facets, "segments": segments, "local_state_note": LOCAL_NOTE}

    async def _segments(self, filters: dict[str, tuple[str, dict]]) -> dict:
        """How many conversations each view holds under these filters.
        Snoozed ones count only under Snoozed and All."""
        where, params = self._where(filters)
        (seg,) = await self._rows(
            f"{_ROWS_SQL} SELECT COALESCE(SUM(needs_you = 1 AND snoozed = 0), 0) AS needs_you, "
            "COALESCE(SUM(unread = 1 AND snoozed = 0), 0) AS unread, "
            "COALESCE(SUM(snoozed = 1), 0) AS snoozed, COUNT(*) AS everything "
            f"FROM rows{where}", {**params, "now": _ts(self.clock())})
        return {"needs_you": seg["needs_you"], "unread": seg["unread"],
                "snoozed": seg["snoozed"], "all": seg["everything"]}

    async def _list_items(self, page: list[dict]) -> list[dict]:
        if not page:
            return []
        ids = [r["id"] for r in page]
        marks = ", ".join("?" for _ in ids)
        latest: dict[str, dict] = {}
        for row in await self._rows(
                "SELECT conversation_id, subject, body, created_at FROM inbound_messages "
                f"WHERE conversation_id IN ({marks}) AND status != 'duplicate' "
                "ORDER BY created_at, rowid", ids):
            latest[row["conversation_id"]] = row
        drafts: dict[str, dict] = {}
        for row in await self._rows(
                "SELECT id, conversation_id, status, revision, send_at FROM outbox "
                f"WHERE conversation_id IN ({marks}) AND kind = 'reply' "
                f"AND status IN ({', '.join('?' for _ in _DRAFTS)}) ORDER BY created_at",
                (*ids, *_DRAFTS)):
            drafts.setdefault(row["conversation_id"], {
                "id": row["id"], "status": row["status"],
                "revision": int(row["revision"] or 1), "send_at": row["send_at"]})
        prospects = sorted({r["prospect_id"] for r in page if r["prospect_id"]})
        sent_subject: dict[str, str] = {}
        if prospects:
            from mercury.state import wire_subject
            pmarks = ", ".join("?" for _ in prospects)
            for row in await self._rows(
                    f"SELECT * FROM outbox WHERE prospect_id IN ({pmarks}) AND status = 'sent' "
                    "ORDER BY sent_at", prospects):
                sent_subject[row["prospect_id"]] = wire_subject(row)
        items = []
        for r in page:
            inbound = latest.get(r["id"])
            legacy = _legacy_thread(r.get("thread_json"))
            if inbound:
                subject, snippet = inbound["subject"], _snippet(inbound["body"])
            else:
                subject = sent_subject.get(r["prospect_id"], "")
                snippet = _snippet(legacy[-1]["content"]) if legacy else ""
            items.append({
                "id": r["id"],
                "prospect": {"id": r["prospect_id"], "email": r["email"],
                             "name": f"{r['first_name']} {r['last_name']}".strip(),
                             "title": r["title"], "status": r["prospect_status"]},
                "company": {"id": r["company_id"], "name": r["company"],
                            "domain": r["company_domain"]},
                "intent": r["intent"], "stage": r["stage"], "status": r["status"],
                "mailbox": r["mailbox"], "subject": subject, "snippet": snippet,
                "last_activity_at": r["last_activity_at"],
                "last_inbound_at": r["last_inbound_at"],
                "unread": bool(r["unread"]), "read_at": r["read_at"],
                "snoozed": bool(r["snoozed"]),
                "snoozed_until": r["snoozed_until"] if r["snoozed"] else None,
                "needs_human": bool(r["needs_human"]), "opted_out": bool(r["opted_out"]),
                "needs_you": bool(r["needs_you"]),
                "response": r["response"], "draft": drafts.get(r["id"]),
                "reminder": r["reminder"], "next_reminder_at": r["next_reminder_at"],
                "partial_history": not r["last_inbound_at"] and bool(r["legacy_count"]),
            })
        return items

    # ── One thread ──

    async def _conversation(self, conversation_id: str):
        convo = await self.state.get_conversation(conversation_id or "")
        if convo is None:
            raise NotFound("conversation not found", code="not_found")
        return convo

    async def thread(self, conversation_id: str) -> dict:
        """The contact's known history, drafts apart, plus what the composer
        needs: who it would go to, from which mailbox, answering what, and
        whether composing is allowed at all."""
        self.ctx.require("read")
        convo = await self._conversation(conversation_id)
        prospect = await self.state.get_prospect(convo.prospect_id) if convo.prospect_id else None
        company = None
        if prospect and getattr(prospect, "company_id", ""):
            company = await self.state.get_company(prospect.company_id)
        messages, partial = await self._messages(convo, prospect)
        drafts, unsent = await self._drafts(convo, prospect)
        local = await self.state.get_inbox_state(convo.id)
        target = await self._reply_target(convo, prospect)
        block = await self._compose_block(convo, prospect)
        restrictions = await self._restrictions(prospect)
        # Unread and snoozed exactly as the list computes them.
        (flags,) = await self._rows(f"{_ROWS_SQL} SELECT unread, snoozed FROM rows "
                                    "WHERE id = :id", {"id": convo.id,
                                                       "now": _ts(self.clock())})
        unread, snoozed = bool(flags["unread"]), bool(flags["snoozed"])
        events, stage_since = await self._events(convo, prospect, messages, restrictions)
        return {
            "conversation": {
                "id": convo.id, "prospect_id": convo.prospect_id,
                "campaign_id": convo.campaign_id, "intent": convo.intent, "stage": convo.stage,
                "status": convo.status, "needs_human": convo.status == "needs_human"
                or convo.intent == "escalate",
                "created_at": convo.created_at.isoformat(),
                "updated_at": convo.updated_at.isoformat(),
                "stage_since": stage_since,
            },
            "prospect": _prospect_view(prospect),
            "company": {**({"id": company.id, "name": company.name, "domain": company.domain,
                            "website": company.website, "industry": company.industry,
                            "location": company.location} if company else
                           {"id": "", "name": getattr(prospect, "company", "") or "",
                            "domain": "", "website": "", "industry": "", "location": ""}),
                        **await self._company_facts(company, convo)},
            "mailbox": target["mailbox"],
            "messages": messages,
            "partial_history": partial,
            "drafts": drafts,
            "unsent": unsent,
            "local": {"read_at": local["read_at"], "unread": unread,
                      "snoozed": snoozed,
                      "snoozed_until": local["snoozed_until"] if snoozed else None,
                      "note": LOCAL_NOTE},
            "notes": await self.state.list_contact_notes(convo.prospect_id)
            if convo.prospect_id else [],
            "reminders": await self.state.list_reminders(conversation_id=convo.id,
                                                         include_done=True, limit=50),
            "compose": {
                "allowed": block is None, "code": block[0] if block else "",
                "reason": block[1] if block else "",
                "to_email": getattr(prospect, "email", "") or "",
                "mailbox": target["mailbox"], "subject": target["subject"],
                "reply_to": target["reply_to"],
                "require_approval": self._require_approval(),
            },
            "restrictions": restrictions,
            "events": events,
        }

    async def _company_facts(self, company, convo) -> dict:
        """The offer this contact is pitched (their campaign's, or the newest
        email's) and the company's latest value for each signal, briefly."""
        offer = ""
        if convo.campaign_id:
            rows = await self._rows("SELECT offer_key FROM campaigns WHERE id = ?",
                                    (convo.campaign_id,))
            offer = rows[0]["offer_key"] if rows else ""
        if not offer and convo.prospect_id:
            rows = await self._rows(
                "SELECT offer_key FROM outbox WHERE prospect_id = ? AND offer_key != '' "
                "ORDER BY created_at DESC LIMIT 1", (convo.prospect_id,))
            offer = rows[0]["offer_key"] if rows else ""
        signals = []
        if company is not None:
            for row in await self._rows(
                    "SELECT signal_code, value_num, value_text FROM observations o "
                    "WHERE company_id = ? AND observed_at = (SELECT MAX(observed_at) "
                    "FROM observations x WHERE x.company_id = o.company_id "
                    "AND x.signal_code = o.signal_code) GROUP BY signal_code "
                    "ORDER BY signal_code LIMIT 8", (company.id,)):
                value = row["value_num"]
                if value is not None and float(value).is_integer():
                    value = int(value)
                text = (row["value_text"] or "").strip()
                signals.append({"code": row["signal_code"],
                                "value": value if value is not None else
                                (text if len(text) <= 24 else "")})
        return {"offer_key": offer or "", "signals": signals}

    async def _events(self, convo, prospect, messages, restrictions) -> tuple[list[dict], dict]:
        """What happened to the thread that is not a message (an exclusion,
        a paused sequence, a stage someone set), oldest first, and since when
        the conversation has been at its stage: the last stage change made
        here, or else the first reply (or the start, before any reply)."""
        events: list[dict] = []
        exclusion = restrictions.get("exclusion")
        if exclusion and exclusion.get("created_at"):
            events.append({"kind": "exclusion", "at": exclusion["created_at"],
                           "source": exclusion["source"], "value": exclusion.get("value", ""),
                           "description": exclusion["description"],
                           "by": exclusion.get("created_by", "")})
        pause = restrictions.get("pause")
        if pause and pause.get("created_at"):
            events.append({"kind": "pause", "at": pause["created_at"], "state": pause["state"],
                           "resume_at": pause.get("resume_at")})
        changes = [row for row in await self.state.get_audit("conversation", convo.id, limit=200)
                   if row["action"] == "inbox.stage" and row["outcome"] == "ok"]
        for row in reversed(changes):
            events.append({"kind": "stage", "at": row["at"], "from": row["detail"].get("from", ""),
                           "to": row["detail"].get("to", ""), "by": row["operator"] or "you"})
        events.sort(key=lambda e: _sort_time(e["at"]))
        if changes:
            since = {"at": changes[0]["at"], "reason": "set"}
        elif convo.stage in CLOSED_STAGES:
            reason = "opted_out" if (getattr(prospect, "status", "") == "opted_out"
                                     or convo.intent == "unsubscribe") else "closed"
            since = {"at": convo.updated_at.isoformat(), "reason": reason}
        else:
            first = next((m for m in messages if m["direction"] == "inbound"), None)
            since = ({"at": first["at"], "reason": "replied"} if first else
                     {"at": convo.created_at.isoformat(), "reason": "started"})
        return events, since

    async def _messages(self, convo, prospect) -> tuple[list[dict], bool]:
        pid = convo.prospect_id or ""
        email = (getattr(prospect, "email", "") or "").lower()
        inbound = await self._rows(
            "SELECT * FROM inbound_messages WHERE conversation_id = ? "
            "OR (? != '' AND prospect_id = ?) "
            "OR (? != '' AND prospect_id = '' AND from_email = ?) ORDER BY created_at, rowid",
            (convo.id, pid, pid, email, email))
        sent = await self._rows(
            "SELECT * FROM outbox WHERE status = 'sent' AND "
            "((? != '' AND prospect_id = ?) OR conversation_id = ?) ORDER BY sent_at",
            (pid, pid, convo.id))
        legacy_email = self.pool.legacy.email if self.pool is not None else ""
        sent = await with_from_mailbox(self.state, sent, legacy_email)
        from mercury.state import wire_subject

        ours = {r["message_id"] for r in sent if r.get("message_id")}
        items: list[dict] = []
        by_id: dict[str, dict] = {}
        for row in inbound:
            if row["rfc_message_id"] and row["rfc_message_id"] in ours:
                continue   # our own email, read back from a mailbox
            if row["status"] in _HIDDEN_INBOUND:
                original = by_id.get(row["duplicate_of"])
                if original is not None and row["mailbox"] not in original["also_received_in"]:
                    original["also_received_in"].append(row["mailbox"])
                continue
            item = {
                "id": row["id"], "source": "inbound", "direction": "inbound",
                "kind": row["kind"], "auto_kind": row["auto_kind"],
                "mailbox": row["mailbox"], "provider": row["provider"],
                "from_email": row["from_email"], "to_email": row["mailbox"],
                "subject": row["subject"], "body": row["body"],
                "at": row["received_at"] or row["created_at"],
                "time_source": "date_header" if row["received_at"] else "stored",
                "received_at": row["received_at"], "stored_at": row["created_at"],
                "rfc_message_id": row["rfc_message_id"], "in_reply_to": row["in_reply_to"],
                "answers_outbox_id": row["outbox_id"],
                "conversation_id": row["conversation_id"],
                "intent": row["intent"], "escalated": row["intent"] == "escalate",
                "delivery": "received",
                "ingestion": {"status": row["status"], "attempts": row["attempts"],
                              "error": row["last_error"]},
                "also_received_in": [],
            }
            by_id[row["id"]] = item
            items.append(item)
        for row in sent:
            items.append({
                "id": row["id"], "source": "outbox", "direction": "outbound",
                "kind": row["kind"], "step": row["step"], "auto_kind": "",
                "mailbox": row.get("from_mailbox") or row.get("mailbox") or "",
                "provider": row["provider"],
                "from_email": row.get("from_mailbox") or row.get("mailbox") or "",
                "to_email": row["to_email"], "subject": wire_subject(row), "body": row["body"],
                "at": row["sent_at"], "time_source": "sent", "sent_at": row["sent_at"],
                "rfc_message_id": row["message_id"], "in_reply_to": row["in_reply_to"],
                "answers_inbound_id": row.get("answers_inbound_id") or "",
                "conversation_id": row["conversation_id"], "campaign_id": row["campaign_id"],
                "intent": "", "escalated": False, "delivery": "sent",
                "generation_id": row.get("generation_id") or "",
                "persona": row.get("persona"),
            })
        # Conversations from before inbound storage: thread_json text that no
        # stored message accounts for. Matched one to one on the text itself.
        unmatched: dict[tuple[str, str], list[dict]] = {}
        for item in items:
            if item["kind"] == "sequence":
                continue
            unmatched.setdefault((item["direction"], _norm_text(item["body"])), []).append(item)
        partial = False
        convos = await self._rows(
            "SELECT id, thread_json FROM conversations WHERE id = ? OR (? != '' AND prospect_id = ?) "
            "ORDER BY created_at, rowid", (convo.id, pid, pid))
        for c in convos:
            for n, message in enumerate(_legacy_thread(c["thread_json"])):
                ours_msg = (message.get("sender") or "").strip().lower() in ("mercury", "harvey")
                direction = "outbound" if ours_msg else "inbound"
                bucket = unmatched.get((direction, _norm_text(message.get("content", ""))))
                if bucket:
                    bucket.pop(0)
                    continue
                partial = True
                items.append({
                    "id": f"legacy:{c['id']}:{n}", "source": "legacy", "direction": direction,
                    "kind": "message", "auto_kind": "", "mailbox": "", "provider": "",
                    "from_email": "" if ours_msg else email, "to_email": email if ours_msg else "",
                    "subject": "", "body": message.get("content", ""),
                    "at": message.get("timestamp"), "time_source": "recorded",
                    "rfc_message_id": "", "in_reply_to": "", "conversation_id": c["id"],
                    "intent": "", "escalated": False,
                    # Never claimed as sent: only an outbox row says that.
                    "delivery": "recorded",
                })
        rank = {"inbound": 0, "outbound": 1}
        items.sort(key=lambda m: (_sort_time(m["at"]), rank[m["direction"]], m["id"]))
        return items, partial

    async def _drafts(self, convo, prospect) -> tuple[list[dict], list[dict]]:
        pid = convo.prospect_id or ""
        rows = await self._rows(
            "SELECT * FROM outbox WHERE kind = 'reply' AND (conversation_id = ? "
            "OR (? != '' AND prospect_id = ?)) ORDER BY created_at, rowid",
            (convo.id, pid, pid))
        queued = [r for r in rows if r["status"] in _DRAFTS]
        unsent = [r for r in rows if r["status"] in _UNSENT][-20:]
        outbox = self._outbox()
        legacy, known = outbox._mailboxes()
        queued = await outbox._with_policy(
            await with_from_mailbox(self.state, queued, legacy, known))
        outbox._with_wire_subject(queued)
        outbox._with_wire_subject(unsent)
        for row in queued + unsent:
            row["revision"] = int(row.get("revision") or 1)
            row["approved"] = row["status"] == "approved" and \
                row.get("approved_revision") == row["revision"]
        return queued, unsent

    async def _reply_target(self, convo, prospect) -> dict:
        """The message a new reply answers (the newest human message in this
        conversation), its mailbox, subject and thread headers. A
        conversation with no stored message falls back to our last sent
        email to them."""
        from mercury.state import reply_subject, thread_headers, wire_subject

        rows = await self._rows(
            "SELECT * FROM inbound_messages WHERE conversation_id = ? AND kind = 'message' "
            "AND status != 'duplicate' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (convo.id,))
        if rows:
            return self._target_from_inbound(rows[0])
        sent = []
        if convo.prospect_id:
            sent = await self._rows(
                "SELECT * FROM outbox WHERE prospect_id = ? AND status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 1", (convo.prospect_id,))
        if sent:
            row = sent[0]
            headers = thread_headers(row)
            return {"mailbox": row.get("mailbox") or "",
                    "subject": reply_subject(wire_subject(row)) if wire_subject(row) else "",
                    "reply_to": None, "in_reply_to": headers.get("in_reply_to", ""),
                    "thread_ref": headers.get("thread_ref", ""),
                    "thread_references": headers.get("thread_references", "")}
        return {"mailbox": "", "subject": "", "reply_to": None, "in_reply_to": "",
                "thread_ref": "", "thread_references": ""}

    @staticmethod
    def _target_from_inbound(row: dict) -> dict:
        from mercury.state import reply_subject

        chain = (row.get("thread_references") or "").split()
        if row.get("rfc_message_id") and row["rfc_message_id"] not in chain:
            chain.append(row["rfc_message_id"])
        return {
            "mailbox": row.get("mailbox") or "",
            "subject": reply_subject(row["subject"]) if row.get("subject") else "",
            "reply_to": {"id": row["id"], "subject": row.get("subject") or "",
                         "from_email": row.get("from_email") or "",
                         "rfc_message_id": row.get("rfc_message_id") or "",
                         "received_at": row.get("received_at") or row.get("created_at")},
            "in_reply_to": row.get("rfc_message_id") or "",
            "thread_ref": row.get("thread_ref") or "",
            "thread_references": " ".join(chain),
        }

    async def _compose_block(self, convo, prospect) -> tuple[str, str] | None:
        """(code, reason) when Mercury must not compose to this conversation."""
        from mercury.policy import ContactPolicy, describe_rule

        if prospect is None or not getattr(prospect, "email", ""):
            return "no_contact", "This conversation has no contact address to write to."
        if convo.status == "needs_human" or convo.intent == "escalate":
            return ("escalated", "This conversation was escalated for a person to handle "
                                 "directly. Mercury does not write replies to it.")
        if prospect.status == "opted_out" or convo.intent == "unsubscribe":
            return "opted_out", "They asked not to be contacted. No reply can be sent."
        rule = await ContactPolicy(self.state, self.config).exclusion_for(prospect.email)
        if rule:
            code = "opted_out" if rule["source"] == "opt_out" else "excluded"
            return code, f"Excluded: {describe_rule(rule)}. No reply can be sent."
        if (prospect.email_status or "") == "invalid":
            return "invalid_address", "Their address bounced. No reply can be sent."
        return None

    async def _restrictions(self, prospect) -> dict:
        """Everything that holds mail to this contact back, read only. The
        Sender re-checks all of it when it claims an email."""
        from mercury.holds import blocking
        from mercury.policy import ContactPolicy, describe_rule

        out = {"exclusion": None, "pause": None, "company_hold": None,
               "sending": {"paused": False, "kind": "", "reason": ""}}
        kind, reason = await blocking(self.state)
        out["sending"] = {"paused": bool(kind), "kind": kind, "reason": reason}
        if prospect is None:
            return out
        policy = ContactPolicy(self.state, self.config)
        rule = await policy.exclusion_for(prospect.email or "")
        if rule:
            out["exclusion"] = {"id": rule["id"], "source": rule["source"],
                                "description": describe_rule(rule),
                                "value": rule.get("value", ""),
                                "created_at": rule.get("created_at"),
                                "created_by": rule.get("created_by", "")}
        pause = await self.state.get_active_pause(prospect.id)
        if pause:
            out["pause"] = {"id": pause["id"], "state": pause.get("state"),
                            "resume_at": pause.get("resume_at"),
                            "created_at": pause.get("created_at"),
                            "note": "Holds their cold sequence only; replies still go."}
        company_id = await policy.company_for(prospect)
        if company_id:
            hold = await self.state.get_company_hold(company_id)
            if hold:
                out["company_hold"] = {"id": hold["id"], "reason": hold.get("reason"),
                                       "note": "Holds cold mail to the company; replies "
                                               "still go."}
        return out

    # ── Local state ──

    async def _run(self, action: str, scope: str, params: dict, work, object_id: str = "",
                   object_type: str = "conversation"):
        return await run_command(self.state, self.ctx, f"inbox.{action}", scope=scope,
                                 params=params, work=work, object_type=object_type,
                                 object_id=object_id)

    async def mark(self, conversation_id: str, read: bool) -> dict:
        async def work(trail):
            await self._conversation(conversation_id)
            await self.state.set_conversations_read([conversation_id], read,
                                                    now=_ts(self.clock()))
            trail.record(conversation_id, read=read)
            return {"id": conversation_id, "unread": not read}
        return await self._run("read" if read else "unread", "edit",
                               {"id": conversation_id}, work, conversation_id)

    def _snooze_until(self, until) -> str:
        when = parse_time(until, "until")
        if when <= self.clock():
            raise Invalid("until must be in the future", field="until")
        return _ts(when)

    async def snooze(self, conversation_id: str, until) -> dict:
        async def work(trail):
            await self._conversation(conversation_id)
            when = self._snooze_until(until)
            await self.state.snooze_conversation(conversation_id, when, now=_ts(self.clock()))
            trail.record(conversation_id, snoozed_until=when)
            return {"id": conversation_id, "snoozed": True, "snoozed_until": when}
        return await self._run("snooze", "edit", {"id": conversation_id, "until": until},
                               work, conversation_id)

    async def unsnooze(self, conversation_id: str) -> dict:
        async def work(trail):
            await self._conversation(conversation_id)
            await self.state.snooze_conversation(conversation_id, None, now=_ts(self.clock()))
            trail.record(conversation_id)
            return {"id": conversation_id, "snoozed": False, "snoozed_until": None}
        return await self._run("unsnooze", "edit", {"id": conversation_id}, work,
                               conversation_id)

    async def set_stage(self, conversation_id: str, stage) -> dict:
        """Move a conversation to a sales stage by hand. A closed stage closes
        it; an open stage reopens a closed one (an escalated conversation
        stays with a person). Mail is not touched: queued emails stay as
        they are, and the Pipeline board follows the stage."""
        from mercury.models.conversation import STAGES

        async def work(trail):
            convo = await self._conversation(conversation_id)
            to = str(stage or "").strip()
            if to not in STAGES:
                raise Invalid(f"stage must be one of {', '.join(STAGES)}", field="stage")
            status = convo.status
            if to in CLOSED_STAGES:
                status = "closed"
            elif status == "closed":
                status = "open"
            if to != convo.stage or status != convo.status:
                await self.state.update_conversation(convo.id, stage=to, status=status)
            trail.record(convo.id, **{"from": convo.stage, "to": to, "status": status})
            return {"id": convo.id, "stage": to, "status": status,
                    "changed": to != convo.stage}
        return await self._run("stage", "edit", {"id": conversation_id, "stage": stage}, work,
                               conversation_id)

    @staticmethod
    def _note_text(body) -> str:
        text = str(body or "").strip()
        if not text:
            raise Invalid("a note needs some text", field="body")
        if len(text) > NOTE_MAX:
            raise Invalid(f"a note is limited to {NOTE_MAX} characters", field="body")
        return text

    async def notes(self, prospect_id: str) -> list[dict]:
        self.ctx.require("read")
        if not await self.state.get_prospect(prospect_id or ""):
            raise NotFound("contact not found", code="not_found")
        return await self.state.list_contact_notes(prospect_id)

    async def add_note(self, prospect_id: str, body) -> dict:
        async def work(trail):
            if not await self.state.get_prospect(prospect_id or ""):
                raise NotFound("contact not found", code="not_found")
            note = await self.state.add_contact_note(prospect_id, self._note_text(body),
                                                     created_by=self.ctx.actor)
            trail.record(note["id"], prospect_id=prospect_id)
            return note
        return await self._run("note_add", "edit", {"prospect_id": prospect_id, "body": body},
                               work, prospect_id, "contact_note")

    async def edit_note(self, note_id: str, body) -> dict:
        async def work(trail):
            text = self._note_text(body)
            note = await self.state.update_contact_note(note_id, text)
            if note is None:
                raise NotFound("note not found", code="not_found")
            trail.record(note_id)
            return note
        return await self._run("note_edit", "edit", {"id": note_id, "body": body}, work,
                               note_id, "contact_note")

    async def delete_note(self, note_id: str) -> dict:
        async def work(trail):
            if not await self.state.delete_contact_note(note_id):
                raise NotFound("note not found", code="not_found")
            trail.record(note_id)
            return {"id": note_id, "deleted": True}
        return await self._run("note_delete", "edit", {"id": note_id}, work, note_id,
                               "contact_note")

    async def reminders(self, due: bool = False, include_done: bool = False,
                        limit: int = 200) -> list[dict]:
        """Open reminders soonest first; ``due`` keeps those whose time has come."""
        self.ctx.require("read")
        rows = await self.state.list_reminders(
            due_before=_ts(self.clock()) if due else None, include_done=include_done,
            limit=min(max(int(limit), 1), 500))
        now = self.clock()
        for row in rows:
            row["due"] = row["done_at"] is None and _sort_time(row["due_at"]) <= now
        return rows

    async def add_reminder(self, conversation_id: str, due_at, note="") -> dict:
        async def work(trail):
            convo = await self._conversation(conversation_id)
            when = _ts(parse_time(due_at, "due_at"))
            text = str(note or "").strip()
            if len(text) > REMINDER_NOTE_MAX:
                raise Invalid(f"a reminder note is limited to {REMINDER_NOTE_MAX} characters",
                              field="note")
            reminder = await self.state.add_reminder(
                conversation_id, when, prospect_id=convo.prospect_id, note=text,
                created_by=self.ctx.actor)
            trail.record(reminder["id"], conversation_id=conversation_id, due_at=when)
            return reminder
        return await self._run("reminder_add", "edit",
                               {"id": conversation_id, "due_at": due_at, "note": note},
                               work, conversation_id, "reminder")

    async def complete_reminder(self, reminder_id: str) -> dict:
        async def work(trail):
            if not await self.state.get_reminder(reminder_id):
                raise NotFound("reminder not found", code="not_found")
            done = await self.state.complete_reminder(reminder_id, done_by=self.ctx.actor)
            trail.record(reminder_id, changed=done)
            return await self.state.get_reminder(reminder_id)
        return await self._run("reminder_done", "edit", {"id": reminder_id}, work,
                               reminder_id, "reminder")

    async def delete_reminder(self, reminder_id: str) -> dict:
        async def work(trail):
            if not await self.state.delete_reminder(reminder_id):
                raise NotFound("reminder not found", code="not_found")
            trail.record(reminder_id)
            return {"id": reminder_id, "deleted": True}
        return await self._run("reminder_delete", "edit", {"id": reminder_id}, work,
                               reminder_id, "reminder")

    # ── Bulk ──

    async def bulk(self, action: str, conversation_ids, *, until=None, kind: str = "email",
                   reason: str = "", confirm: bool = False) -> dict:
        """One action over an explicit list of conversations. Each succeeds
        or fails on its own; the response names the scope and every outcome.
        ``exclude`` is a contact-policy change (it blocks queued mail and every
        future email to the address or domain) and needs ``confirm``."""
        async def work(trail):
            if action not in BULK_ACTIONS:
                raise Invalid(f"action must be one of {', '.join(BULK_ACTIONS)}", field="action")
            if not isinstance(conversation_ids, list) or not all(
                    isinstance(c, str) and c for c in conversation_ids):
                raise Invalid("conversation_ids must be a list of conversation ids",
                              field="conversation_ids")
            ids = list(dict.fromkeys(conversation_ids))
            if not 1 <= len(ids) <= BULK_MAX:
                raise Invalid(f"give between 1 and {BULK_MAX} conversations",
                              field="conversation_ids")
            when = self._snooze_until(until) if action == "snooze" else None
            exclusions = None
            if action == "exclude":
                if confirm is not True:
                    raise Invalid("excluding stops every email to these contacts; send "
                                  "confirm: true to do it", code="confirmation_required")
                if kind not in ("email", "domain"):
                    raise Invalid("kind must be email or domain", field="kind")
                from mercury.control.exclusions import ExclusionService
                exclusions = ExclusionService(self.state, self.config)
            trail.batch_id = uuid.uuid4().hex[:12]
            results = []
            for cid in ids:
                try:
                    outcome = await self._bulk_one(action, cid, when, kind, reason, exclusions)
                    results.append({"id": cid, "ok": True, **outcome})
                    trail.record(cid, **outcome)
                except (NotFound, Conflict, Invalid, Forbidden) as error:
                    results.append({"id": cid, "ok": False, "code": error.code,
                                    "message": str(error)})
                    trail.record(cid, outcome=error.code, message=str(error))
            done = sum(1 for r in results if r["ok"])
            return {"action": action, "batch_id": trail.batch_id,
                    "scope": {"conversation_ids": ids, "count": len(ids),
                              **({"until": when} if when else {}),
                              **({"kind": kind} if action == "exclude" else {})},
                    "succeeded": done, "failed": len(ids) - done, "results": results}
        scope = "approve" if action == "exclude" else "edit"
        return await self._run(f"bulk_{action}" if action in BULK_ACTIONS else "bulk", scope,
                               {"action": action, "conversation_ids": conversation_ids,
                                "until": until, "kind": kind, "reason": reason,
                                "confirm": confirm}, work)

    async def _bulk_one(self, action, cid, when, kind, reason, exclusions) -> dict:
        """One conversation's outcome. ``changed`` is false when it was
        already in the state asked for (read, or snoozed until that time)."""
        convo = await self._conversation(cid)
        now = _ts(self.clock())
        if action in ("read", "unread", "snooze", "unsnooze"):
            (before,) = await self._rows(f"{_ROWS_SQL} SELECT unread, snoozed, snoozed_until "
                                         "FROM rows WHERE id = :id", {"id": cid, "now": now})
        if action in ("read", "unread"):
            changed = bool(before["unread"]) != (action == "unread")
            if changed:
                await self.state.set_conversations_read([cid], action == "read", now=now)
            return {"unread": action == "unread", "changed": changed}
        if action == "snooze":
            changed = not (before["snoozed"] and _sort_time(before["snoozed_until"])
                           == _sort_time(when))
            if changed:
                await self.state.snooze_conversation(cid, when, now=now)
            return {"snoozed_until": when, "changed": changed}
        if action == "unsnooze":
            changed = bool(before["snoozed"])
            if changed:
                await self.state.snooze_conversation(cid, None, now=now)
            return {"snoozed_until": None, "changed": changed}
        from mercury.control.exclusions import ExclusionError
        from mercury.policy import email_domain, is_shared_provider

        prospect = await self.state.get_prospect(convo.prospect_id) if convo.prospect_id else None
        if prospect is None or not prospect.email:
            raise Conflict("this conversation has no contact address", code="no_contact")
        value = prospect.email if kind == "email" else email_domain(prospect.email)
        if kind == "domain" and is_shared_provider(value):
            raise Conflict(f"{value} is a shared mail provider; exclude the address instead",
                           code="shared_domain")
        try:
            rule = await exclusions.add(kind, value, reason=reason or "excluded from the inbox",
                                        actor=self.ctx.actor)
        except ExclusionError as error:
            raise Invalid(str(error), code=error.code) from error
        return {"rule_id": rule["id"], "kind": kind, "value": rule["value"],
                "created": rule["created"], "blocked_queued": rule.get("blocked", 0)}

    # ── Compose ──

    async def _composable(self, conversation_id: str):
        convo = await self._conversation(conversation_id)
        prospect = await self.state.get_prospect(convo.prospect_id) if convo.prospect_id else None
        block = await self._compose_block(convo, prospect)
        if block:
            raise Forbidden(block[1], code="compose_refused", reason=block[0])
        return convo, prospect

    async def _draft_of(self, convo, item_id: str) -> dict:
        item = await self.state.get_outbox_item(item_id or "")
        if not item or item.get("kind") != "reply" or item.get("conversation_id") != convo.id:
            raise NotFound("draft not found in this conversation", code="not_found")
        return item

    async def _draft_view(self, item_id: str) -> dict:
        view = await self._outbox().get(item_id)
        view["revision"] = int(view.get("revision") or 1)
        view["approved"] = view["status"] == "approved" and \
            view.get("approved_revision") == view["revision"]
        return view

    async def create_draft(self, conversation_id: str, *, body: str = "", subject: str = "",
                           reply_to: str = "", generate: bool = False,
                           instruction: str = "") -> dict:
        """A new reply draft in review, written here (``body``) or by Mercury
        (``generate``, with an optional instruction: one model call). It
        answers ``reply_to`` (an inbound message id) or the newest message in
        the conversation, from the mailbox it arrived in. Never sends."""
        async def work(trail):
            convo, prospect = await self._composable(conversation_id)
            existing = await self._rows(
                "SELECT id, status, revision FROM outbox WHERE conversation_id = ? "
                f"AND kind = 'reply' AND status IN ({', '.join('?' for _ in _DRAFTS)}) "
                "ORDER BY created_at LIMIT 1", (convo.id, *_DRAFTS))
            if existing:
                raise Conflict("this conversation already has a reply waiting; edit that one",
                               code="draft_exists", draft_id=existing[0]["id"],
                               revision=int(existing[0]["revision"] or 1),
                               status=existing[0]["status"])
            target = await self._target(convo, prospect, reply_to)
            text = (subject or "").strip() or target["subject"] or "Re: your note"
            if len(text) > SUBJECT_MAX:
                raise Invalid(f"subject is limited to {SUBJECT_MAX} characters", field="subject")
            generation_id = ""
            if generate:
                message, generation_id = await self._generate(convo, prospect, target,
                                                              instruction)
            else:
                message = (body or "").strip()
                if not message:
                    raise Invalid("write the reply, or ask Mercury to generate one",
                                  field="body")
            if len(message) > BODY_MAX:
                raise Invalid(f"body is limited to {BODY_MAX} characters", field="body")
            provider = getattr(getattr(getattr(self.config, "channels", None), "email", None),
                               "provider", "") or ""
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id, conversation_id=convo.id, kind="reply",
                to_email=prospect.email, subject=text, body=message,
                send_at=self.clock().isoformat(), status="pending_review",
                provider=provider, thread_ref=target["thread_ref"],
                in_reply_to=target["in_reply_to"],
                thread_references=target["thread_references"], mailbox=target["mailbox"],
                generation_id=generation_id,
                answers_inbound_id=(target["reply_to"] or {}).get("id", ""),
            )
            if not item_id:
                raise Conflict("the draft could not be created; reload and try again")
            if not generate:
                await self.state.update_outbox_item(item_id, manually_edited=1)
            trail.record(item_id, None, 1, object_type="outbox", conversation_id=convo.id,
                         generated=bool(generate))
            return await self._draft_view(item_id)
        return await self._run("draft_create", "edit",
                               {"id": conversation_id, "body": body, "subject": subject,
                                "reply_to": reply_to, "generate": generate,
                                "instruction": instruction}, work, conversation_id)

    async def _target(self, convo, prospect, reply_to: str) -> dict:
        if not reply_to:
            return await self._reply_target(convo, prospect)
        row = await self.state.get_inbound(reply_to)
        if not row or row.get("status") == "duplicate" or not (
                row.get("conversation_id") == convo.id
                or (convo.prospect_id and row.get("prospect_id") == convo.prospect_id)):
            raise NotFound("that message is not part of this conversation", code="not_found",
                           field="reply_to")
        return self._target_from_inbound(row)

    async def _generate(self, convo, prospect, target, instruction: str) -> tuple[str, str]:
        instruction = (instruction or "").strip()
        if len(instruction) > INSTRUCTION_MAX:
            raise Invalid(f"instruction is limited to {INSTRUCTION_MAX} characters",
                          field="instruction")
        from mercury.agents.handler import Handler
        from mercury.brain import Brain
        from mercury.config import load_config, load_env

        config = self.config or load_config()
        env = self.env or load_env()
        handler = Handler(Brain(self.state), self.state, config, env)
        latest = ""
        if target["reply_to"]:
            answered = await self.state.get_inbound(target["reply_to"]["id"])
            latest = (answered or {}).get("body", "").strip()
        if not latest:
            latest = next((m.content for m in reversed(convo.thread) if not m.is_ours), "")
        response = await handler._generate_response(
            convo.intent or "question", latest, prospect, convo, instruction=instruction)
        if not response:
            raise Unavailable("the writer returned nothing; try again", code="provider_failed")
        return response, handler._response_generation_id

    async def edit_draft(self, conversation_id: str, item_id: str, subject: str, body: str,
                         revision=None) -> dict:
        convo, _ = await self._composable(conversation_id)
        await self._draft_of(convo, item_id)
        result = await self._outbox().edit(item_id, subject, body, revision)
        return {**await self._draft_view(item_id),
                "approval_cleared": result["approval_cleared"]}

    async def regenerate_draft(self, conversation_id: str, item_id: str, instruction: str = "",
                               revision=None) -> dict:
        convo, _ = await self._composable(conversation_id)
        item = await self._draft_of(convo, item_id)
        was_approved = item["status"] == "approved"
        await self._outbox().regenerate(item_id, instruction, revision)
        return {**await self._draft_view(item_id), "approval_cleared": was_approved}

    async def approve_draft(self, conversation_id: str, item_id: str, revision=None) -> dict:
        """Approve the draft at the revision on screen. It leaves at its send
        time (now, for a draft not scheduled) once the Sender's gates pass."""
        convo, _ = await self._composable(conversation_id)
        await self._draft_of(convo, item_id)
        await self._outbox().approve(item_id, revision)
        return await self._draft_view(item_id)

    async def schedule_draft(self, conversation_id: str, item_id: str, send_at,
                             revision=None) -> dict:
        """Approve the draft to go out at ``send_at``. Moving the time is a
        new revision; it is approved at that revision in the same request."""
        convo, _ = await self._composable(conversation_id)
        await self._draft_of(convo, item_id)
        when = parse_time(send_at, "send_at")
        moved = await self._outbox().reschedule(item_id, when, revision)
        # Two commands: a request key covers each under its own name.
        ctx = self.ctx
        if ctx.request_id:
            ctx = dataclasses.replace(ctx, request_id=f"{ctx.request_id}:approve")
        await OutboxService(ctx, self.state, self.config, self.pool, self.env).approve(
            item_id, moved["revision"])
        return await self._draft_view(item_id)

    async def discard_draft(self, conversation_id: str, item_id: str, revision=None) -> dict:
        convo = await self._conversation(conversation_id)
        await self._draft_of(convo, item_id)
        result = await self._outbox().reject(item_id, revision)
        return {"id": item_id, "rejected": result["rejected"], "status": "rejected"}

    # ── Today ──

    async def today(self) -> dict:
        """Due reminders and messages that could not be handled: what the
        Today page flags. Read only; nothing is sent or queued."""
        self.ctx.require("read")
        now = _ts(self.clock())
        due = await self.state.list_reminders(due_before=now, limit=500)
        failed = await self._rows(
            "SELECT id, from_email, mailbox, subject, last_error, attempts, created_at, "
            "conversation_id FROM inbound_messages WHERE status = 'failed' "
            "ORDER BY created_at DESC LIMIT 50")
        return {"reminders_due": due, "failed_messages": failed,
                "segments": await self._segments({})}


def _legacy_thread(raw) -> list[dict]:
    """A conversation's stored thread_json as plain dicts ([] when unreadable)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def _prospect_view(prospect) -> dict:
    if prospect is None:
        return {"id": "", "name": "", "email": "", "title": "", "status": "",
                "email_status": "", "linkedin_url": "", "phone": ""}
    return {"id": prospect.id, "name": prospect.full_name(), "email": prospect.email,
            "title": prospect.title, "status": prospect.status,
            "email_status": prospect.email_status or "",
            "linkedin_url": prospect.linkedin_url or "", "phone": prospect.phone or ""}


__all__ = ["InboxService", "parse_time", "BULK_ACTIONS", "FACETS", "LOCAL_NOTE",
           "SEGMENT_FILTERS"]
