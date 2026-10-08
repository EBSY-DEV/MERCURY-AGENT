"""Read-only queries the dashboard serves, for every interface.

Plain reads never create the database and never raise on a fresh install: a
missing file, table or column reads as empty. ``signals``, ``runs`` and
``cohort`` need the schema (and the signal catalog), so call ``ready()``
before them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SIGNAL_CATEGORIES = {
    "discovery": {
        "label": "Discovery — who exists",
        "blurb": "How Mercury finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile — what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People — who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability — can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
SIGNAL_CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]
COHORT_LIMIT, COHORT_ROWS = 1000, 200


def _decode(rows: list[dict], column: str, key: str, empty) -> list[dict]:
    for row in rows:
        try:
            row[key] = json.loads(row.get(column, json.dumps(empty)))
        except (json.JSONDecodeError, TypeError):
            row[key] = type(empty)()
    return rows


class QueryService:
    def __init__(self, ctx, state):
        self.ctx, self.state = ctx, state

    async def ready(self):
        await self.state.init_db()
        return self

    async def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Rows as dicts. Never raises: a missing DB file, missing table, or
        malformed schema returns []."""
        if not Path(self.state.db_path).exists():
            return []
        try:
            async with aiosqlite.connect(str(self.state.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(sql, params) as cursor:
                    return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.warning("query failed (%s): %s", sql.split(None, 4)[:4], e)
            return []

    async def stats(self) -> dict:
        """Pipeline overview counts."""
        self.ctx.require("read")
        prospects = await self._rows("SELECT status, COUNT(*) as count FROM prospects GROUP BY status")
        campaigns = {r["status"]: r["count"] for r in await self._rows(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status")}
        convos = {r["status"]: r["count"] for r in await self._rows(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status")}
        actions = await self._rows("SELECT COUNT(*) as count FROM actions")
        usage = await self._rows("SELECT claude_calls FROM usage_log WHERE date = date('now')")
        return {
            "prospects": {"total": sum(r["count"] for r in prospects),
                          "by_status": {r["status"]: r["count"] for r in prospects}},
            "campaigns": {"total": sum(campaigns.values()), "by_status": campaigns},
            "conversations": {"total": sum(convos.values()), "by_status": convos},
            "actions_total": actions[0]["count"] if actions else 0,
            "claude_calls_today": usage[0]["claude_calls"] if usage else 0,
        }

    async def companies(self, limit: int = 200) -> list[dict]:
        """Newest companies first, with their contact counts."""
        self.ctx.require("read")
        return await self._rows("""
            SELECT c.*,
                (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
            FROM companies c ORDER BY c.created_at DESC LIMIT ?
        """, (int(limit),))

    async def company_contacts(self, company_id: str) -> list[dict]:
        self.ctx.require("read")
        return await self._rows("SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                                (company_id,))

    async def prospects(self, limit: int = 200) -> list[dict]:
        self.ctx.require("read")
        return await self._rows("SELECT * FROM prospects ORDER BY created_at DESC LIMIT ?", (int(limit),))

    async def campaigns(self, limit: int = 100) -> list[dict]:
        self.ctx.require("read")
        rows = await self._rows("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT ?", (int(limit),))
        return _decode(_decode(rows, "sequence_json", "sequence", []), "prospect_ids_json", "prospect_ids", [])

    async def conversations(self, limit: int = 100) -> list[dict]:
        self.ctx.require("read")
        rows = await self._rows("""
            SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company
            FROM conversations c
            LEFT JOIN prospects p ON c.prospect_id = p.id
            ORDER BY c.updated_at DESC LIMIT ?
        """, (int(limit),))
        return _decode(rows, "thread_json", "thread", [])

    async def activity(self, limit: int = 100) -> list[dict]:
        self.ctx.require("read")
        rows = await self._rows("SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (int(limit),))
        return _decode(rows, "details_json", "details", {})

    async def audit(self, object_type: str = "", object_id: str = "", limit: int = 100) -> list[dict]:
        """Operator commands, newest first: who, through which client, on
        what, revisions before and after, and how each ended."""
        self.ctx.require("read")
        if not Path(self.state.db_path).exists():
            return []
        await self.state.init_db()
        return await self.state.get_audit(object_type, object_id, limit)

    async def runs(self, limit: int = 25) -> list[dict]:
        """The collector run log: what ran, when, what it produced and cost."""
        self.ctx.require("read")
        await self.state.sweep_stale_runs()
        return await self.state.get_runs(limit=limit)

    async def signals(self) -> dict:
        """The signal vocabulary, grouped for review, with live cohort sizes."""
        self.ctx.require("read")
        from mercury.signals import seed_signal_catalog

        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(self.state)
        codes = await self.state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await self.state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in SIGNAL_CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = SIGNAL_CATEGORIES.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            status = sig.get("status", "proposed")
            summary[status] = summary.get(status, 0) + 1
        return {"summary": summary, "groups": groups, "total": len(codes)}

    async def cohort(self, require: list[str], exclude: list[str] | None = None) -> dict:
        """How many companies carry ALL the required signals and none of the
        excluded ones. Set intersection happens in SQL; intersecting a capped
        fetch elsewhere silently returns the wrong answer."""
        self.ctx.require("read")
        require = [c for c in (require or []) if c]
        exclude = [c for c in (exclude or []) if c]
        if not require:
            return {"size": 0, "companies": []}
        ids = await self.state.cohort(require, exclude, limit=COHORT_LIMIT)
        if not ids:
            return {"size": 0, "companies": []}
        placeholders = ",".join("?" for _ in ids[:COHORT_ROWS])
        rows = await self._rows(
            f"SELECT id, name, domain, industry, location FROM companies WHERE id IN ({placeholders})",
            tuple(ids[:COHORT_ROWS]),
        )
        return {"size": len(ids), "companies": rows}
