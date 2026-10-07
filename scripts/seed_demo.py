"""Seed a throwaway demo database for dashboard screenshots.

Creates ~12 Denver/Boulder roofing + HVAC companies, 30 prospects spread
across all seven pipeline columns (new, queued, contacted, replied, meeting,
won, lost), conversations at several stages, and outbox rows: sequence steps
1-3 with send times spread over the past two weeks and the next three weeks
(sent / approved / pending_review / cancelled) plus two replies.

On top of that: ~60 days of outreach history (sends, replies, positive
replies, bounces) so the Trends chart has 30/90-day shape, and an inbox
warm-up — alex@tryebsy.com warming since 9 days ago with part of the
checklist done, plus a second, not-started inbox. The demo config is the
untrained template, so /api/warmup treats the first warming inbox as the
sender; set MERCURY_SENDER_EMAIL to force a different one.

The target database is DELETED and rebuilt on every run. It refuses to touch
the real data/mercury.db.

Usage (from the repo root):

    env -u APPIMAGE .venv/bin/python scripts/seed_demo.py              # -> data/demo.db
    env -u APPIMAGE .venv/bin/python scripts/seed_demo.py /tmp/demo.db

Then point the dashboard at it:

    MERCURY_DB_PATH=data/demo.db env -u APPIMAGE .venv/bin/mercury dashboard
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mercury.models.campaign import Campaign, EmailStep  # noqa: E402
from mercury.models.company import Company  # noqa: E402
from mercury.models.conversation import Conversation, Message  # noqa: E402
from mercury.models.prospect import Prospect  # noqa: E402
from mercury.state import StateManager  # noqa: E402

DEFAULT_DB = ROOT / "data" / "demo.db"
REAL_DB = ROOT / "data" / "mercury.db"

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

COMPANIES = [
    ("Summit Peak Roofing", "summitpeakroofing.com", "Roofing", "Denver, CO"),
    ("Front Range HVAC", "frontrangehvac.com", "HVAC", "Denver, CO"),
    ("Flatirons Roof Co", "flatironsroof.com", "Roofing", "Boulder, CO"),
    ("Mile High Heating & Air", "milehighheatingair.com", "HVAC", "Denver, CO"),
    ("Pearl Street Roofing", "pearlstreetroofing.com", "Roofing", "Boulder, CO"),
    ("Cherry Creek Comfort", "cherrycreekcomfort.com", "HVAC", "Denver, CO"),
    ("Rocky Ridge Exteriors", "rockyridgeexteriors.com", "Roofing", "Lakewood, CO"),
    ("Boulder Valley Mechanical", "bouldervalleymech.com", "HVAC", "Boulder, CO"),
    ("Highlands Roofing Group", "highlandsroofing.com", "Roofing", "Denver, CO"),
    ("Arapahoe Air Systems", "arapahoeair.com", "HVAC", "Centennial, CO"),
    ("Chautauqua Roof & Gutter", "chautauquaroof.com", "Roofing", "Boulder, CO"),
    ("Platte River Heating", "platteriverheating.com", "HVAC", "Denver, CO"),
]

FIRST = ["Mike", "Sarah", "Dave", "Jen", "Carlos", "Amy", "Tom", "Rachel", "Luis",
         "Kate", "Brian", "Megan", "Jose", "Lisa", "Kevin", "Nicole", "Ryan", "Erin",
         "Matt", "Dana", "Greg", "Tina", "Sam", "Holly", "Derek", "Maria", "Chris",
         "Beth", "Nate", "Alexis"]
LAST = ["Hoffman", "Nguyen", "Kowalski", "Ramirez", "Bauer", "Okafor", "Lindqvist",
        "Patel", "Sorensen", "Brooks", "Delgado", "Fischer", "Murphy", "Tran",
        "Whitaker", "Castillo", "Reyes", "Holm", "Gallagher", "Yoon", "Becker",
        "Santos", "Mercer", "Novak", "Ortiz", "Klein", "Foster", "Abbott", "Diaz",
        "Hughes"]
TITLES = ["Owner", "Founder & Owner", "General Manager", "Operations Manager",
          "President", "Co-Owner", "Office Manager", "Sales Manager"]

# Column -> number of prospects (sums to 30).
PLAN = [("new", 5), ("queued", 4), ("contacted", 7), ("replied", 5),
        ("meeting", 3), ("won", 2), ("lost", 4)]

SEQUENCE = [
    EmailStep(step=1, delay_days=0, subject="{{company}} on page two",
              body="Hi {{first_name}}, {{company}} shows up on page two for "
                   "'roof repair Denver'. Worth a quick look at why?"),
    EmailStep(step=2, delay_days=4, subject="Re: {{company}} on page two",
              body="{{first_name}}, the three shops above you all have online "
                   "booking. Want the short list of what they do differently?"),
    EmailStep(step=3, delay_days=5, subject="Closing the loop",
              body="Last note, {{first_name}}. Happy to send the audit if timing's better later."),
]


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def render(text: str, p: Prospect, company: str) -> str:
    return text.replace("{{first_name}}", p.first_name).replace("{{company}}", company)


async def seed(db_path: Path) -> dict:
    rnd = random.Random(7)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    sm = StateManager(str(db_path))
    await sm.init_db()

    companies = []
    for name, domain, industry, location in COMPANIES:
        cid = await sm.add_company(Company(
            name=name, domain=domain, website=f"https://{domain}", industry=industry,
            location=location, source="dataforseo_listings",
            phone=f"(303) 555-{rnd.randint(1000, 9999)}",
            created_at=NOW - timedelta(days=20), updated_at=NOW - timedelta(days=20),
        ))
        companies.append((cid, name, domain))

    campaign = Campaign(id="", name="Denver trades — page two", sequence=SEQUENCE,
                        status="active", created_at=NOW - timedelta(days=14))
    campaign.id = await sm.add_campaign(campaign)

    counts = {"prospects": 0, "conversations": 0, "outbox": 0}
    idx = 0

    async def outbox(p, pid, company, step, status, send_at, sent_at=None, error=""):
        s = SEQUENCE[step - 1]
        item = await sm.add_outbox_item(
            prospect_id=pid, to_email=p.email, subject=render(s.subject, p, company),
            body=render(s.body, p, company), send_at=iso(send_at), status=status,
            campaign_id=campaign.id, step=step, provider="gmail",
        )
        fields = {}
        if sent_at:
            fields["sent_at"] = iso(sent_at)
        if error:
            fields["error"] = error
        if fields:
            await sm.update_outbox_item(item, **fields)
        counts["outbox"] += 1

    async def convo(pid, stage, status, msgs, age_days, intent="interested"):
        when = NOW - timedelta(days=age_days, hours=rnd.randint(1, 8))
        # The timestamped event the handler logs on every inbound reply —
        # what /api/trends counts as a reply (and a positive one).
        await sm.log_action("reply_received", "handler",
                            {"prospect_id": pid, "intent": intent}, created_at=iso(when))
        cid = await sm.add_conversation(Conversation(
            id="", prospect_id=pid, campaign_id=campaign.id, stage=stage, status=status,
            intent=intent,
            thread=[Message(sender=s, content=c, timestamp=when) for s, c in msgs],
            created_at=when, updated_at=when,
        ))
        counts["conversations"] += 1
        return cid

    for column, n in PLAN:
        for k in range(n):
            cid, cname, domain = companies[idx % len(companies)]
            first, last = FIRST[idx], LAST[idx]
            status = {"won": "closed"}.get(column, column)
            if column == "lost" and k == 0:
                status = "opted_out"
            if column == "replied" and k == 4:
                status = "contacted"  # has a conversation -> still lands in Replied
            age = rnd.randint(0, 13)
            p = Prospect(
                company_id=cid, first_name=first, last_name=last,
                title=rnd.choice(TITLES), email=f"{first.lower()}@{domain}",
                email_status=rnd.choice(["verified", "verified", "verified", "risky"]),
                email_verified=True, status=status, score=rnd.randint(42, 96),
                company=cname, industry="Construction",
                source="website", created_at=NOW - timedelta(days=age + 3),
                updated_at=NOW - timedelta(days=age, hours=rnd.randint(0, 20)),
            )
            pid = await sm.add_prospect(p)
            counts["prospects"] += 1
            idx += 1

            # Day the first email went (or goes) out, relative to now.
            start = NOW - timedelta(days=rnd.randint(1, 13), hours=rnd.randint(0, 6))
            start = start.replace(hour=rnd.choice([8, 9, 10, 14, 15]), minute=rnd.choice([5, 20, 40]))

            if column == "new":
                continue
            if column == "queued":
                first_at = NOW + timedelta(days=rnd.randint(0, 6), hours=rnd.randint(2, 9))
                for step in (1, 2, 3):
                    at = first_at + timedelta(days=(step - 1) * 4 + (step > 2))
                    await outbox(p, pid, cname, step,
                                 "pending_review" if k % 2 else "approved", at)
                continue

            # Everyone else got step 1.
            await outbox(p, pid, cname, 1, "sent", start, start + timedelta(minutes=3))
            step2 = start + timedelta(days=4)
            step3 = start + timedelta(days=9)

            if column == "contacted":
                for step, at in ((2, step2), (3, step3)):
                    if at < NOW:
                        await outbox(p, pid, cname, step, "sent", at, at + timedelta(minutes=2))
                    else:
                        await outbox(p, pid, cname, step,
                                     "approved" if step == 2 else "pending_review",
                                     at + timedelta(days=rnd.randint(0, 8)))
                continue

            reason = {"replied": "stop_on_reply", "meeting": "moved_to_meeting",
                      "won": "moved_to_won", "lost": "moved_to_lost"}[column]
            if status == "opted_out":
                reason = "stop_on_reply"
            await outbox(p, pid, cname, 2, "cancelled", max(step2, NOW + timedelta(days=1)),
                         error=reason)
            await outbox(p, pid, cname, 3, "cancelled", max(step3, NOW + timedelta(days=6)),
                         error=reason)

            reply = ("prospect", "Yeah we've noticed the drop. What would this look like for us?")
            if column == "replied":
                stage = ["engaged", "qualifying", "presenting", "engaged", "negotiating"][k]
                intent = ["question", "interested", "question", "objection", "interested"][k]
                await convo(pid, stage, "open", [reply], rnd.randint(0, 3), intent)
            elif column == "meeting":
                await convo(pid, "closing", "open",
                            [reply, ("mercury", "Thursday at 10 work for a 15-minute call?"),
                             ("prospect", "Thursday works.")], rnd.randint(0, 2))
            elif column == "won":
                await convo(pid, "closed_won", "closed",
                            [reply, ("prospect", "Let's do it. Send the agreement.")], rnd.randint(1, 5))
            elif column == "lost" and k == 1:
                await convo(pid, "closed_lost", "closed",
                            [("prospect", "We're locked in with our agency through next year.")],
                            rnd.randint(2, 6), "not_interested")

    # Two replies in the outbox: one already sent, one awaiting approval.
    replied = await sm.get_prospects_by_status("replied")
    for i, p in enumerate(replied[:2]):
        item_id = await sm.add_outbox_item(
            prospect_id=p.id, to_email=p.email, kind="reply", step=1, provider="gmail",
            subject=f"Re: {p.company} on page two",
            body=f"Hi {p.first_name}, happy to walk through it. Does Tuesday or Wednesday morning work?",
            send_at=iso(NOW - timedelta(days=1) if i == 0 else NOW + timedelta(hours=3)),
            status="sent" if i == 0 else "pending_review",
        )
        if i == 0:
            await sm.update_outbox_item(item_id, sent_at=iso(NOW - timedelta(days=1) + timedelta(minutes=4)))
        counts["outbox"] += 1

    counts.update(await seed_history(sm))
    counts.update(await seed_warmup(sm))
    return counts


# ── Trends history + warm-up ─────────────────────────────────────────

# The demo's sending identity. mercury.yaml in a fresh checkout is the
# untrained template (a placeholder persona email), so /api/warmup falls back
# to the first warming inbox — this one. MERCURY_SENDER_EMAIL forces it.
DEMO_SENDER = "alex@tryebsy.com"
DEMO_SECOND_INBOX = "sam@tryebsy.com"
WARMUP_START_DAYS_AGO = 9          # today is plan day 10 (week 2)
DEMO_TARGET = 40
HISTORY_DAYS = 60

HIST_PREFIX = ["summit", "peak", "ridge", "canyon", "aspen", "granite", "mesa", "pine",
               "foothill", "timberline", "redrock", "bluebird", "highplains", "clearcreek"]
HIST_TRADE = ["roofing", "hvac", "exteriors", "heating", "mechanical", "roofco", "air"]
HIST_FIRST = ["owner", "info", "mike", "sarah", "dave", "jen", "tom", "kate", "luis", "amy"]


async def seed_history(sm: StateManager) -> dict:
    """~60 days of outreach so /api/trends 30 and 90 have something to show.

    Earlier history comes from a finished campaign; the last ten days are
    topped up on the warming inbox but kept under its ramp caps (warm-up is
    enforced in the real sender, so the demo shouldn't show it broken).
    History rows reference archived prospect ids that aren't on the board,
    so the pipeline stays exactly as seeded above.
    """
    from mercury import warmup

    rnd = random.Random(11)
    archived = Campaign(id="", name="Front Range trades — spring list", sequence=SEQUENCE,
                        status="completed", created_at=NOW - timedelta(days=HISTORY_DAYS + 2))
    archived.id = await sm.add_campaign(archived)

    today = NOW.date()
    start = today - timedelta(days=WARMUP_START_DAYS_AGO)
    caps = warmup.ramp_plan(DEMO_TARGET)

    async with sm._connect() as db:
        async with db.execute(
            "SELECT date(sent_at), COUNT(*) FROM outbox WHERE status = 'sent' GROUP BY 1"
        ) as cur:
            existing = {d: n for d, n in await cur.fetchall()}

    outbox_rows, events = [], []
    n = 0
    for days_ago in range(HISTORY_DAYS - 1, -1, -1):
        day = today - timedelta(days=days_ago)
        weekend = day.weekday() >= 5
        if day >= start:
            cap = caps[(day - start).days]
            share = 0.4 if day == today else rnd.uniform(0.75, 0.95)
            target = int(cap * share)
        else:
            progress = 1 - days_ago / HISTORY_DAYS
            target = rnd.randint(0, 3) if weekend else int(rnd.uniform(8, 13) + 8 * progress)
        volume = max(0, target - existing.get(day.isoformat(), 0))
        # Reply rate improves over the period (copy got better), bounces fall.
        progress = 1 - days_ago / HISTORY_DAYS
        p_reply = 0.05 + 0.035 * progress
        p_bounce = 0.028 - 0.016 * progress

        for i in range(volume):
            n += 1
            pid = f"hist{n:05d}"
            email = (f"{rnd.choice(HIST_FIRST)}@{rnd.choice(HIST_PREFIX)}"
                     f"{rnd.choice(HIST_TRADE)}{n % 97}.com")
            sent_at = datetime.combine(day, datetime.min.time()) + timedelta(
                hours=rnd.randint(14, 22), minutes=rnd.randint(0, 59))
            if sent_at > NOW:
                # Today's sends happened between midnight (UTC) and now.
                midnight = datetime.combine(day, datetime.min.time())
                elapsed = max(0, int((NOW - midnight).total_seconds()))
                sent_at = midnight + timedelta(seconds=rnd.randint(0, elapsed))
            step = rnd.choice([1, 1, 1, 2, 2, 3])
            s = SEQUENCE[step - 1]
            outbox_rows.append((
                pid + "-o", archived.id, pid, step, email,
                s.subject.replace("{{company}}", "your shop"),
                s.body.replace("{{first_name}}", "there").replace("{{company}}", "your shop"),
                iso(sent_at), iso(sent_at),
            ))

            # Bounces in the last week are kept rare on purpose: the warm-up
            # health gate reads them, and the demo inbox should be healthy.
            bounce = rnd.random() < p_bounce if days_ago >= 7 else (days_ago == 5 and i == 0)
            if bounce:
                at = min(sent_at + timedelta(minutes=rnd.randint(1, 40)), NOW)
                events.append(("bounce", {"prospect": email, "prospect_id": pid}, iso(at)))
                continue
            if rnd.random() < p_reply:
                at = min(sent_at + timedelta(hours=rnd.randint(1, 60)), NOW)
                roll = rnd.random()
                intent = ("interested" if roll < 0.38 else "ooo" if roll < 0.48 else
                          "question" if roll < 0.68 else "objection" if roll < 0.82
                          else "not_interested")
                events.append(("reply_received",
                               {"prospect_id": pid, "prospect_email": email, "intent": intent},
                               iso(at)))

    async with sm._connect() as db:
        await db.executemany(
            """INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email,
                                   subject, body, status, send_at, sent_at, provider)
               VALUES (?, ?, ?, ?, 'sequence', ?, ?, ?, 'sent', ?, ?, 'gmail')""",
            outbox_rows,
        )
        await db.commit()
    for action_type, details, at in events:
        await sm.log_action(action_type, "handler", details, created_at=at)
    return {"history_sends": len(outbox_rows), "history_events": len(events)}


async def seed_warmup(sm: StateManager) -> dict:
    start = (NOW.date() - timedelta(days=WARMUP_START_DAYS_AGO)).isoformat()
    await sm.add_warmup_inbox(DEMO_SENDER, status="warming", start_date=start, target_daily=DEMO_TARGET,
                              notes="Google Workspace on the secondary domain, bought for outreach.")
    done = ["secondary_domain", "domain_redirect", "profile", "personal_emails",
            "newsletters", "verified_only", "reply_same_day", "plain_text"]
    await sm.update_warmup_inbox(DEMO_SENDER, tasks_json=json.dumps({k: True for k in done}))
    await sm.add_warmup_inbox(DEMO_SECOND_INBOX, notes="Second inbox for when alex@ hits target.")
    return {"warmup_inboxes": 2}


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_DB
    target = target if target.is_absolute() else (Path.cwd() / target)
    if target.resolve() == REAL_DB.resolve():
        print(f"Refusing to overwrite the real database at {REAL_DB}.", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    counts = asyncio.run(seed(target))
    print(f"Seeded {target}: {counts['prospects']} prospects, "
          f"{len(COMPANIES)} companies, {counts['conversations']} conversations, "
          f"{counts['outbox']} outbox rows, {counts['history_sends']} historical sends, "
          f"{counts['history_events']} reply/bounce events, "
          f"{counts['warmup_inboxes']} warm-up inboxes.")
    print(f"Run: MERCURY_DB_PATH={target} env -u APPIMAGE .venv/bin/mercury dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
