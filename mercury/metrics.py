"""Outreach metrics over time: sends, replies, positive replies, bounces.

These numbers back the dashboard trends chart (``/api/trends``) and the
warm-up health gate, so both read the same definitions:

* **sent** — outbox rows with ``status = 'sent'`` and ``kind = 'sequence'``,
  bucketed by ``date(sent_at)``. Replies Mercury sends are not outreach and
  are excluded. (The warm-up *volume* counts are different on purpose — see
  ``sent_by_day``.)
* **replies** — inbound human replies, one per prospect per UTC day. The
  source is the ``reply_received`` event the handler logs to ``actions``
  (timestamped, carries prospect_id + intent). Prospects with NO such event
  — conversations recorded before the event existed — fall back to the
  conversation's ``created_at`` (the handler creates a conversation on the
  first reply). Out-of-office auto-replies (intent ``ooo``) are not replies.
* **positive** — the same events where intent is ``interested``.
* **bounces** — the ``bounce`` event the handler logs, one per prospect per
  day (unmatched bounces count individually).

Stored timestamps mix ``YYYY-MM-DDTHH:MM:SS`` (Python isoformat) and
``YYYY-MM-DD HH:MM:SS`` (SQLite CURRENT_TIMESTAMP); every comparison goes
through ``date()`` / ``datetime()`` so both sort and bucket identically.
All days are UTC.
"""

from __future__ import annotations

from datetime import date, timedelta

import aiosqlite

METRICS = ("sent", "replies", "positive", "bounces")


def _j(path: str, col: str = "details_json") -> str:
    """json_extract that tolerates a malformed details blob."""
    return f"(CASE WHEN json_valid({col}) THEN json_extract({col}, '$.{path}') END)"


# (key, ts, intent) for every inbound reply event — see module docstring.
_REPLY_EVENTS = f"""
    SELECT {_j('prospect_id')} AS k, created_at AS ts, {_j('intent')} AS intent
    FROM actions
    WHERE action_type = 'reply_received' AND COALESCE({_j('prospect_id')}, '') != ''
    UNION ALL
    SELECT c.prospect_id AS k, c.created_at AS ts, c.intent AS intent
    FROM conversations c
    WHERE COALESCE(c.prospect_id, '') != ''
      AND NOT EXISTS (
          SELECT 1 FROM actions a
          WHERE a.action_type = 'reply_received'
            AND {_j('prospect_id', 'a.details_json')} = c.prospect_id
      )
"""

_BOUNCE_EVENTS = f"""
    SELECT CASE
             WHEN COALESCE({_j('prospect_id')}, '') != '' THEN {_j('prospect_id')}
             WHEN COALESCE({_j('prospect')}, '') NOT IN ('', 'unknown') THEN {_j('prospect')}
             ELSE id
           END AS k,
           created_at AS ts, '' AS intent
    FROM actions
    WHERE action_type = 'bounce'
"""

_SENT_EVENTS = """
    SELECT id AS k, sent_at AS ts, '' AS intent
    FROM outbox
    WHERE status = 'sent' AND kind = 'sequence' AND sent_at IS NOT NULL
"""

_EVENT_SOURCES = {
    "sent": (_SENT_EVENTS, ""),
    "replies": (_REPLY_EVENTS, "AND COALESCE(intent, '') != 'ooo'"),
    "positive": (_REPLY_EVENTS, "AND intent = 'interested'"),
    "bounces": (_BOUNCE_EVENTS, ""),
}


async def daily_counts(db_path: str, start: date, end: date) -> dict[str, dict[str, int]]:
    """``{YYYY-MM-DD: {sent, replies, positive, bounces}}`` for start..end
    inclusive. Only days with at least one event appear."""
    out: dict[str, dict[str, int]] = {}
    params = (start.isoformat(), end.isoformat())
    async with aiosqlite.connect(db_path) as db:
        for metric, (source, extra) in _EVENT_SOURCES.items():
            sql = (
                f"SELECT date(ts) AS d, COUNT(DISTINCT k || '|' || date(ts)) "
                f"FROM ({source}) "
                f"WHERE date(ts) BETWEEN ? AND ? {extra} "
                f"GROUP BY d"
            )
            async with db.execute(sql, params) as cursor:
                for d, n in await cursor.fetchall():
                    if d:
                        out.setdefault(d, dict.fromkeys(METRICS, 0))[metric] = n
    return out


async def window_counts(db_path: str, since: str) -> dict[str, int]:
    """Event totals since a timestamp (rolling window, not day buckets).

    Events are still de-duplicated per prospect per day."""
    totals = dict.fromkeys(METRICS, 0)
    async with aiosqlite.connect(db_path) as db:
        for metric, (source, extra) in _EVENT_SOURCES.items():
            sql = (
                f"SELECT COUNT(DISTINCT k || '|' || date(ts)) FROM ({source}) "
                f"WHERE datetime(ts) >= datetime(?) {extra}"
            )
            async with db.execute(sql, (since,)) as cursor:
                totals[metric] = (await cursor.fetchone())[0] or 0
    return totals


async def sent_by_day(db_path: str, start: date, end: date) -> dict[str, int]:
    """Every native send (sequence AND replies) per UTC day.

    This is *volume* — what a warm-up cap limits and what the sender's daily
    cap already counts — so Mercury's own replies are included here even
    though they are excluded from the outreach ``sent`` metric."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT date(sent_at) AS d, COUNT(*) FROM outbox "
            "WHERE status = 'sent' AND sent_at IS NOT NULL "
            "AND date(sent_at) BETWEEN ? AND ? GROUP BY d",
            (start.isoformat(), end.isoformat()),
        ) as cursor:
            return {d: n for d, n in await cursor.fetchall() if d}


async def sent_by_day_by_mailbox(db_path: str, start: date, end: date) -> dict[str, dict[str, int]]:
    """``{mailbox: {YYYY-MM-DD: n}}`` — ``sent_by_day`` split by the mailbox
    each email went out from ('' = sent before mailbox tracking)."""
    out: dict[str, dict[str, int]] = {}
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COALESCE(mailbox, ''), date(sent_at) AS d, COUNT(*) FROM outbox "
            "WHERE status = 'sent' AND sent_at IS NOT NULL "
            "AND date(sent_at) BETWEEN ? AND ? GROUP BY 1, 2",
            (start.isoformat(), end.isoformat()),
        ) as cursor:
            for mailbox, d, n in await cursor.fetchall():
                if d:
                    out.setdefault(mailbox, {})[d] = n
    return out


# The mailbox an event belongs to: the one recorded on the event, else the
# mailbox of the latest email sent to that prospect ('' = untracked/legacy).
# An event no send can be traced to. Not an address, so MailboxPool.resolve()
# returns None and no mailbox is charged for it.
UNATTRIBUTED = "?"


def _event_mailbox_sql() -> str:
    """The mailbox an action event belongs to: its own ``mailbox`` field, else
    the last sent email to its ``prospect_id``, else (bounces logged before
    either existed) the last sent email to the bounced address in
    ``prospect``. A matched pre-rotation send yields '' (the legacy mailbox,
    which did send it); no match at all yields UNATTRIBUTED, so old bounces
    are never blamed on whichever mailbox happens to own '' rows."""
    return f"""COALESCE(NULLIF(lower({_j('mailbox')}), ''), (
        SELECT COALESCE(o.mailbox, '') FROM outbox o
        WHERE o.prospect_id = {_j('prospect_id')} AND o.status = 'sent'
        ORDER BY o.sent_at DESC LIMIT 1
    ), (
        SELECT COALESCE(o.mailbox, '') FROM outbox o
        WHERE lower(o.to_email) = lower({_j('prospect')}) AND o.status = 'sent'
          AND COALESCE({_j('prospect')}, '') != ''
        ORDER BY o.sent_at DESC LIMIT 1
    ), '{UNATTRIBUTED}')"""


async def window_counts_by_mailbox(db_path: str, since: str) -> dict[str, dict[str, int]]:
    """``{mailbox: {sent, bounces, replies}}`` since a timestamp — the
    warm-up health gate's inputs, per sending mailbox.

    Same definitions as ``window_counts``: sent = outreach (sequence) sends;
    bounces and replies are de-duplicated per prospect per day, and an
    out-of-office reply is not a reply. Events logged before they carried a
    mailbox are attributed through the prospect's last sent email."""
    out: dict[str, dict[str, int]] = {}

    def bump(mailbox: str, metric: str, n: int) -> None:
        out.setdefault(mailbox or "", {"sent": 0, "bounces": 0, "replies": 0})[metric] += n

    mb = _event_mailbox_sql()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COALESCE(mailbox, ''), COUNT(*) FROM outbox "
            "WHERE status = 'sent' AND kind = 'sequence' AND sent_at IS NOT NULL "
            "AND datetime(sent_at) >= datetime(?) GROUP BY 1",
            (since,),
        ) as cursor:
            for mailbox, n in await cursor.fetchall():
                bump(mailbox, "sent", n)
        for metric, action, extra in (
            ("bounces", "bounce", ""),
            ("replies", "reply_received", f"AND COALESCE({_j('intent')}, '') != 'ooo'"),
        ):
            key = (f"CASE WHEN COALESCE({_j('prospect_id')}, '') != '' "
                   f"THEN {_j('prospect_id')} ELSE id END")
            sql = (
                f"SELECT m, COUNT(DISTINCT k || '|' || d) FROM ("
                f"  SELECT {mb} AS m, {key} AS k, date(created_at) AS d FROM actions "
                f"  WHERE action_type = ? AND datetime(created_at) >= datetime(?) {extra}"
                f") GROUP BY m"
            )
            async with db.execute(sql, (action, since)) as cursor:
                for mailbox, n in await cursor.fetchall():
                    bump(mailbox, metric, n)
    return out


def _rate(n: int, sent: int) -> float | None:
    return round(n / sent, 4) if sent else None


def summarize(rows: list[dict]) -> dict:
    """Totals + rates (fractions of sent; None when nothing was sent)."""
    totals = {m: sum(r[m] for r in rows) for m in METRICS}
    sent = totals["sent"]
    totals["reply_rate"] = _rate(totals["replies"], sent)
    totals["positive_rate"] = _rate(totals["positive"], sent)
    totals["bounce_rate"] = _rate(totals["bounces"], sent)
    return totals


async def trends(db_path: str, days: int, today: date) -> dict:
    """The ``/api/trends`` payload: a zero-filled daily series ending today,
    its totals, and the same totals for the preceding window of equal length."""
    start = today - timedelta(days=days - 1)
    prior_start = start - timedelta(days=days)
    counts = await daily_counts(db_path, prior_start, today)

    def series(first: date) -> list[dict]:
        rows = []
        for i in range(days):
            d = (first + timedelta(days=i)).isoformat()
            rows.append({"date": d, **counts.get(d, dict.fromkeys(METRICS, 0))})
        return rows

    current = series(start)
    prior = series(prior_start)
    return {
        "days": days,
        "series": current,
        "totals": summarize(current),
        "prior": summarize(prior),
    }


async def heatmap(db_path: str, weeks: int, today: date) -> dict:
    """The ``/api/heatmap`` payload: outreach sends per UTC day for a
    GitHub-style grid of ``weeks`` full weeks (Monday-first, ending with the
    current week). Days after today are omitted, not zero-filled, so the
    client can leave them blank."""
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    counts = await daily_counts(db_path, start, today)
    days = []
    d = start
    while d <= today:
        c = counts.get(d.isoformat(), {})
        days.append({"date": d.isoformat(), "sent": c.get("sent", 0),
                     "replies": c.get("replies", 0)})
        d += timedelta(days=1)

    sent = [x["sent"] for x in days]
    longest = run = 0
    for n in sent:
        run = run + 1 if n else 0
        longest = max(longest, run)
    current = 0
    # Today with nothing sent yet doesn't break a streak that ran to yesterday.
    for n in reversed(sent[:-1] if sent and not sent[-1] else sent):
        if not n:
            break
        current += 1
    best = max(days, key=lambda x: x["sent"], default=None)
    return {
        "weeks": weeks,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "days": days,
        "total_sent": sum(sent),
        "total_replies": sum(x["replies"] for x in days),
        "active_days": sum(1 for n in sent if n),
        "max": max(sent, default=0),
        "streak_current": current,
        "streak_longest": longest,
        "best_day": best if best and best["sent"] else None,
    }
