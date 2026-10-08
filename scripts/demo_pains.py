"""Demo pains for scripts/seed_demo.py: a small library across all three
statuses, plus outbox rows that carry a pain code so the results show.

Everything here is invented. Codes are PAIN_A..PAIN_E, markets are segment_a
and segment_b, trades are trade_a and trade_b, offers are offer_a and offer_b,
and URLs are example.com. Nothing is tied to a real company's offers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mercury.signals import seed_signal_catalog

HUMAN = "dashboard:demo"
# Signals the demo pains name; a person has confirmed them, so the editor offers them.
CONFIRMED_SIGNALS = ["NO_ONLINE_BOOKING", "TECH_STACK", "SITE_PAGE_COUNT", "HIRING_ROLE", "BLOG_STALE"]

# (code, status, days ago it was decided or proposed, note, fields)
PAINS = [
    ("PAIN_A", "proposed", 1, "", dict(
        label="Quotes go unanswered", owner_words="We lose track of the quotes we send.",
        scene="Quotes go out by email and nobody checks back for a week.",
        cost="Jobs go to whoever follows up first.",
        market="segment_a", sector="trade_a", offer_key="offer_a",
        signal_codes=["NO_ONLINE_BOOKING"], source="trainer",
        evidence=["example.com/services, quote form with no follow-up step",
                  "Mentioned twice in calls with businesses in segment_a"])),
    ("PAIN_B", "confirmed", 3, "", dict(
        label="Paperwork eats the week", owner_words="Half my week goes to paperwork.",
        scene="The owner does estimates and invoices at night after jobs.",
        cost="Fewer jobs booked in busy season.",
        market="segment_a", sector="trade_a", offer_key="offer_a",
        signal_codes=["TECH_STACK", "SITE_PAGE_COUNT"], source="trainer",
        evidence=["example.com/about, owner writes every quote by hand",
                  "Repeated across five discovery notes",
                  "example.com/blog, post about late nights"])),
    ("PAIN_C", "confirmed", 3, "", dict(
        label="New hires ramp slowly", owner_words="New hires take months to get up to speed.",
        scene="Every new tech shadows the owner for weeks before going alone.",
        cost="Growth stalls at the owner's calendar.",
        market="segment_b", sector="trade_b", offer_key="offer_b",
        signal_codes=["HIRING_ROLE"], source="trainer",
        evidence=["example.com/careers, three open roles"])),
    ("PAIN_D", "proposed", 1, "", dict(
        label="Job profit is a guess", owner_words="We never know which jobs made money.",
        scene="Costs are tracked in a notebook, prices are set by feel.",
        cost="Underpriced jobs keep the crew busy and the margin thin.",
        market="segment_b", sector="trade_b", offer_key="offer_b",
        signal_codes=["TECH_STACK"], source="trainer",
        evidence=["Came up in two customer calls"])),
    ("PAIN_E", "rejected", 4, "too generic", dict(
        label="Nobody can find them online", owner_words="Nobody can find us online.",
        scene="Too broad to be about anyone in particular.", cost="",
        market="segment_a", sector="trade_a", offer_key="offer_a",
        signal_codes=["BLOG_STALE"], source="trainer", evidence=[])),
]

# Sent sequence emails to attach to each confirmed pain: (code, without a reply, with a reply).
STATS = [("PAIN_B", 16, 2), ("PAIN_C", 8, 1)]


def extend_config(cfg: dict) -> None:
    """Give the demo config the markets and offers the pains refer to."""
    markets = cfg.setdefault("icp", {}).setdefault("markets", [])
    have = {m.get("name") for m in markets}
    for market in ({"name": "segment_a", "places": ["Denver, CO"], "terms": ["trade_a"]},
                   {"name": "segment_b", "places": ["Boulder, CO"], "terms": ["trade_b"]}):
        if market["name"] not in have:
            markets.append(market)
    offers = cfg.setdefault("offers", [])
    keys = {o.get("key") for o in offers}
    # Other demo modules may already define these offers in full; only add
    # the ones still missing, since a key may appear once.
    offers.extend({"key": key} for key in ("offer_a", "offer_b") if key not in keys)


def _stamp(days: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
            - timedelta(days=days)).isoformat()


async def seed_pains(sm) -> dict:
    await seed_signal_catalog(sm)
    for code in CONFIRMED_SIGNALS:
        await sm.set_signal_status(code, "confirmed")

    for code, status, days, note, fields in PAINS:
        await sm.add_pain(code, status=status, status_note=note,
                          status_by=HUMAN if status != "proposed" else "",
                          origin_text=fields["label"], **fields)
        when = _stamp(days)
        async with sm._connect() as db:
            await db.execute("UPDATE pains SET created_at = ?, updated_at = ?, "
                             "status_at = CASE WHEN status = 'proposed' THEN NULL ELSE ? END "
                             "WHERE code = ?", (when, when, when, code))
            await db.commit()

    # Attach pains to sent sequence emails, preferring people who replied so
    # the per-pain results show replies as well as sends.
    async with sm._connect() as db:
        async with db.execute(
            "SELECT o.id, o.prospect_id, EXISTS(SELECT 1 FROM conversations c "
            "WHERE c.prospect_id = o.prospect_id) AS replied FROM outbox o "
            "WHERE o.status = 'sent' AND o.kind = 'sequence' AND o.pain_code = '' "
            "ORDER BY o.sent_at, o.id") as cursor:
            rows = await cursor.fetchall()
        plain = [r[0] for r in rows if not r[2]]
        replied = [r[0] for r in rows if r[2]]
        tagged = 0
        for code, quiet, answered in STATS:
            ids = [plain.pop(0) for _ in range(min(quiet, len(plain)))]
            ids += [replied.pop(0) for _ in range(min(answered, len(replied)))]
            for item_id in ids:
                await db.execute("UPDATE outbox SET pain_code = ? WHERE id = ?", (code, item_id))
            tagged += len(ids)
        await db.commit()
    return {"pains": len(PAINS), "pain_emails": tagged}
