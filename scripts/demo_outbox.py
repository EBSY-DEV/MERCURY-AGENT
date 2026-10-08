"""Outbox review data for the demo database (called by scripts/seed_demo.py).

The base seed queues sequence emails from a template. This turns the review
queue into what a reviewer meets after a real Writer run, all synthetic:

* two offers in the demo config (``offer_a`` for roofing listings with no
  website, ``offer_b`` the default), with approved content, a call to action
  per step and claim restrictions;
* two confirmed pains, named on some drafts and not others;
* drafts rewritten per contact, each with its word count and step limit,
  plus one new contact whose first email ran over its limit (flagged);
* a recorded generation per thread whose prompt carries the offer brief, so
  "Why this email" shows the facts, the ask and what was kept out;
* approved emails spread over today, tomorrow and later days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from mercury.config import load_config
from mercury.draft_rules import FLAG_OVER_LIMIT, count_words, encode_flags, word_limit
from mercury.offers import OfferBrief, offer_by_key
from mercury.personas import PersonaStore
from mercury.state import StateManager

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
SIGNER = "Jordan"
REVIEWER = "Jordan Hale"


def demo_offers() -> list[dict]:
    """The two offers added to the demo config, next to the voice offer."""
    restrictions = [
        "Never quote a price or a discount.",
        "Never promise a ranking, a result or a guarantee.",
        "Never mention review counts or star ratings.",
        "Never mention another offer.",
    ]
    return [
        {
            "key": "offer_a", "segments": ["roofing"], "signals": {"require": ["NO_WEBSITE"]},
            "facts": ["NO_WEBSITE", "SERP_RANK"],
            "content": {
                "name": "Listing page",
                "summary": "A one-page site for a business listing that links nowhere.",
                "claims": ["Built from what the listing already shows."],
                "sentence": "We build a one-page site for listings that link nowhere.",
            },
            "restrictions": restrictions,
            "steps": {
                1: {"cta": "One question they can answer in a line",
                    "angle": "the listing has nothing to click through to"},
                2: {"cta": "Offer the two changes to make first"},
                3: {"cta": "Leave the door open, no question"},
            },
        },
        {
            "key": "offer_b", "default": True, "facts": ["SERP_RANK"],
            "content": {
                "name": "Listing review",
                "summary": "A short review of how a listing compares with the ones above it.",
                "sentence": "We send a short review of how your listing compares.",
            },
            "restrictions": restrictions,
            "steps": {
                1: {"cta": "Ask if a short comparison would help"},
                2: {"cta": "Offer the two changes to make first"},
                3: {"cta": "Leave the door open, no question"},
            },
        },
    ]


PAINS = [
    {"code": "PAIN_A", "label": "Quotes go out and nobody follows up",
     "owner_words": "We lose track of the quotes we send.",
     "scene": "A quote goes out on Monday and nobody calls back.", "offer_key": "offer_a"},
    {"code": "PAIN_C", "label": "New hires take months to get up to speed",
     "owner_words": "New hires take months to get up to speed.",
     "scene": "Every new tech shadows the owner for weeks before going alone.", "offer_key": "offer_b"},
]


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


TRADE_WORDS = {"roofing", "group", "hvac", "heating", "&", "air", "co", "exteriors", "mechanical",
               "comfort", "roof", "gutter", "systems"}


def short_name(company: str) -> str:
    """How an email names the business: "Front Range HVAC" -> "Front Range"."""
    words = [w for w in (company or "").split() if w.lower() not in TRADE_WORDS]
    return " ".join(words) or company or "your shop"


def drafts(offer_key: str, first: str, company: str, city: str, rank: int, sign: str,
           keyword: str) -> dict[int, tuple]:
    short = short_name(company)
    if offer_key == "offer_a":
        one = (f"{short.lower()} on page two",
               f"Hi {first},\n\n{short} shows up {ordinal(rank)} for roof repair in {city}, and there is "
               "no website behind the listing, so anyone comparing roofers has nothing to click "
               f"through to.\n\nWould a short note on what usually moves a listing like that onto "
               f"page one be useful?\n\n{sign}")
    elif offer_key == "voice":
        one = (f"{short.lower()}, quick one",
               f"Hi {first},\n\nI put together a short example of what this could look like "
               f"for {short}.\n\nWant me to send it over?\n\n{sign}")
    else:
        one = (f"{short.lower()} listing",
               f"Hi {first},\n\n{short} ranks {ordinal(rank)} for {keyword} in {city}. The "
               "listing has no way to book online, so people pick the next listing that "
               f"does.\n\nWould a short comparison with the listings above yours help?\n\n{sign}")
    return {
        1: one,
        2: (f"Re: {one[0]}",
            f"Hi {first},\n\nOne more thought on {short}. The three listings above yours all link "
            "to a page with photos of finished jobs. Yours links nowhere yet.\n\n"
            f"Want the two changes I would make first?\n\n{sign}"),
        3: ("Closing the loop",
            f"Hi {first},\n\nLast note from me. If this is not a priority this season, no problem. "
            f"Happy to send the short list whenever it is.\n\n{sign}"),
    }


def long_first(first: str, company: str, city: str, sign: str, keyword: str) -> tuple[str, str]:
    """A step 1 that runs over its limit, the way a flagged draft does."""
    short = short_name(company)
    return (f"{short.lower()}, quick one",
            f"Hi {first},\n\n{short} shows up on page two for {keyword} in {city}, below three "
            "companies with fewer projects in their gallery than you have. Most people pick from "
            "the first five results and never scroll, which means a lot of the work you are "
            "clearly good at is going to whoever ranks above you. There are usually one or two "
            "fixes on the listing itself that move a business up, and they rarely take more than "
            "an afternoon to put in place.\n\nWould it be useful if I sent you the two I would "
            f"start with?\n\n{sign}")


def fact_lines(rank: int, keyword: str, has_site: bool) -> list[str]:
    observed = (NOW - timedelta(days=6)).date().isoformat()
    ranked = (NOW - timedelta(days=4)).date().isoformat()
    lines = [] if has_site else [f"- No website: yes (observed {observed})"]
    return lines + [f"- Search rank for {keyword}: {rank} (observed {ranked})"]


async def _rows(sm, sql: str, params=()) -> list[dict]:
    import aiosqlite

    async with sm._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def _exec(sm, sql: str, params=()) -> None:
    async with sm._connect() as db:
        await db.execute(sql, params)
        await db.commit()


async def seed_outbox_review(db_path: Path, config_path: Path) -> dict:
    sm = StateManager(str(db_path))
    config = load_config(str(config_path))
    store = PersonaStore(sm)
    profile = await store.resolve(config)

    for pain in PAINS:
        await sm.add_pain(pain["code"], label=pain["label"], owner_words=pain["owner_words"],
                          scene=pain["scene"], offer_key=pain["offer_key"], status="confirmed",
                          status_by=REVIEWER)

    main = (await _rows(sm, "SELECT id FROM campaigns WHERE offer_key = '' ORDER BY created_at LIMIT 1"))[0]["id"]

    # One more contact, first email over its limit: the draft that needs a look.
    fresh = (await _rows(sm, "SELECT p.id, p.email FROM prospects p WHERE p.status = 'new' "
                             "AND p.company LIKE 'Rocky Ridge%' LIMIT 1")
             or await _rows(sm, "SELECT p.id, p.email FROM prospects p WHERE p.status = 'new' LIMIT 1"))[0]
    first_at = NOW + timedelta(minutes=50)
    for step in (1, 2, 3):
        await sm.add_outbox_item(
            prospect_id=fresh["id"], to_email=fresh["email"], subject="-", body="-",
            send_at=(first_at + timedelta(days=(step - 1) * 4)).isoformat(),
            status="pending_review", campaign_id=main, step=step, provider="smtp")
    await _exec(sm, "UPDATE prospects SET status = 'queued' WHERE id = ?", (fresh["id"],))

    queued = await _rows(
        sm,
        "SELECT o.id, o.campaign_id, o.prospect_id, o.step, o.status, o.mailbox, o.send_at, "
        "c.offer_key AS campaign_offer, p.first_name, co.name AS company, co.industry, co.location "
        "FROM outbox o JOIN campaigns c ON c.id = o.campaign_id "
        "JOIN prospects p ON p.id = o.prospect_id LEFT JOIN companies co ON co.id = p.company_id "
        "WHERE o.kind = 'sequence' AND o.status IN ('pending_review', 'approved') "
        "ORDER BY o.campaign_id, o.prospect_id, o.step")
    threads: dict[tuple, list[dict]] = {}
    for row in queued:
        threads.setdefault((row["campaign_id"], row["prospect_id"]), []).append(row)

    flagged = 0
    for n, ((campaign_id, prospect_id), rows) in enumerate(sorted(threads.items())):
        head = rows[0]
        roofing = (head["industry"] or "").lower() == "roofing"
        offer_key = head["campaign_offer"] or ("offer_a" if roofing else "offer_b")
        city = (head["location"] or "Denver, CO").split(",")[0]
        rank = 11 + (sum(map(ord, head["company"] or "")) % 18)
        mailbox = next((r["mailbox"] for r in rows if r["mailbox"]), "")
        sign = mailbox.split("@")[0].capitalize() if mailbox else SIGNER
        keyword = "roof repair" if roofing else "furnace repair"
        texts = drafts(offer_key, head["first_name"], head["company"], city, rank, sign, keyword)
        is_fresh = prospect_id == fresh["id"]
        if is_fresh:
            offer_key = "offer_b"
            texts[1] = long_first(head["first_name"], head["company"], city, sign, keyword)
        pain = "" if is_fresh else ("PAIN_A" if offer_key == "offer_a" else
                                    "PAIN_C" if offer_key == "offer_b" and n % 2 else "")
        if offer_key == "offer_a":
            reason = "offer_a rule matched: segment roofing, has NO_WEBSITE"
        elif offer_key == "offer_b":
            reason = "no offer rule matched; offer_b is the default"
        else:
            reason = f"kept from the campaign ({offer_key})"
        await sm.record_offer_routes(campaign_id, [{
            "prospect_id": prospect_id, "offer_key": offer_key, "reason": reason,
            "is_default": offer_key == "offer_b"}])

        offer = offer_by_key(config, offer_key)
        brief = OfferBrief(offer=offer, steps=[r["step"] for r in rows], reason=reason,
                           facts=fact_lines(rank, keyword, has_site=not roofing or is_fresh))
        prompt = ("You write short, plain cold emails for one contact.\n\n"
                  f"CONTACT\n{head['first_name']} at {head['company']}, {head['location']}"
                  + brief.render())
        output = {"steps": [{"step": r["step"], "subject": texts[r["step"]][0],
                             "body": texts[r["step"]][1]} for r in rows]}
        generation_id = await store.record(profile, config, prompt, output, "write_sequence")

        for r in rows:
            subject, body = texts[r["step"]]
            limit = word_limit(config, r["step"])
            words = count_words(body)
            flags = [FLAG_OVER_LIMIT] if words > limit else []
            flagged += bool(flags)
            await _exec(
                sm,
                "UPDATE outbox SET subject = ?, body = ?, offer_key = ?, pain_code = ?, "
                "word_count = ?, word_limit = ?, flags = ?, generation_id = ? WHERE id = ?",
                (subject, body, offer_key if not head["campaign_offer"] else "", pain,
                 words, limit, encode_flags(flags), generation_id, r["id"]))

    # Approved mail on several days: the first ones go out today and tomorrow.
    approved = await _rows(sm, "SELECT id FROM outbox WHERE status = 'approved' ORDER BY send_at")
    slots = [NOW + timedelta(minutes=95), NOW + timedelta(hours=3, minutes=20),
             NOW + timedelta(days=1, hours=1, minutes=10)]
    for row, at in zip(approved, slots):
        await _exec(sm, "UPDATE outbox SET send_at = ? WHERE id = ?", (at.isoformat(), row["id"]))

    return {"review_threads": len(threads), "flagged_drafts": flagged}
