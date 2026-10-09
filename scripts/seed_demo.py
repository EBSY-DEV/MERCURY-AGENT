"""Seed a throwaway demo database for dashboard screenshots.

Creates ~12 Denver/Boulder roofing + HVAC companies, 30 prospects spread
across all seven pipeline columns (new, queued, contacted, replied, meeting,
won, lost), conversations at several stages, and outbox rows: sequence steps
1-3 with send times spread over the past two weeks and the next three weeks
(sent / approved / pending_review / cancelled) plus two replies. Three of
the queued contacts are on a ``voice`` offer that requires a demo: one demo
requested, one not registered, one ready, so the Outbox shows the demo gate.

On top of that: ~60 days of outreach history (sends, replies, positive
replies, bounces) so the Trends chart has 30/90-day shape, spread over three
sending mailboxes through ``outbox.mailbox``:

* jordan@northwind-outreach.com — already warm (30/day), carries the older history and
  owns pre-rotation rows; a 4% bounce rate this week puts it on *hold*.
* alex@getnorthwind.com  — warming since 16 days ago (week 3 of a 5 → 30 ramp),
  with part of the checklist ticked and notes.
* sam@trynorthwind.com   — scheduled: its ramp starts in three days.

The Inbox gets stored mail for every conversation (scripts/demo_inbox.py):
replies across all three mailboxes, drafts waiting for review, a reply
scheduled for later, an escalated thread, an opted-out contact, a snooze, a
due reminder, notes, and one reply Mercury could not process.
Two writing personas ("Plain local", the default, and "Numbers first"), with a
saved generation behind every board email so the Outbox shows which voice
wrote each draft. Twelve signals confirmed and observed across the companies
(listing data for all twelve, homepage profiling for ten), so Companies,
Signals and the cohort builder have real counts.

Plus one inbox placement test from yesterday (rows in ``placement_tests``,
nothing sent): the warm inbox and the control land in the inbox, the
warming one mostly in spam.

Which inboxes exist, their caps and start dates come from the mail config,
not the database (see mercury/warmup.py). So next to the database the seed
writes a demo config, ``<db>.mercury.yaml`` (data/demo.mercury.yaml by
default — data/ is gitignored), with those three SMTP mailboxes and dates
relative to today. It holds no secrets: every mailbox reads its password from
MAILBOX_DEMO_PASSWORD, which you set to any dummy value so the dashboard
treats the mailboxes as configured. Nothing is ever sent (the dashboard does
not send, and smtp.invalid never resolves). Your own mercury.local.yaml is
never touched; MERCURY_CONFIG points the dashboard at the demo config instead.

The target database is DELETED and rebuilt on every run. It refuses to touch
the real data/mercury.db.

Usage (from the repo root):

    env -u APPIMAGE .venv/bin/python scripts/seed_demo.py              # -> data/demo.db
    env -u APPIMAGE .venv/bin/python scripts/seed_demo.py /tmp/demo.db

Then point the dashboard at it:

    MERCURY_DB_PATH=data/demo.db MERCURY_CONFIG=data/demo.mercury.yaml \
    MAILBOX_DEMO_PASSWORD=demo env -u APPIMAGE .venv/bin/mercury dashboard

Don't run ``mercury run`` with that environment: it is a dashboard demo.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo_registry import seed_registry  # noqa: E402  (next to this file)
from mercury.integrations.mailboxes import warmup_cap  # noqa: E402
from mercury.models.campaign import Campaign, EmailStep  # noqa: E402
from mercury.models.company import Company  # noqa: E402
from mercury.models.conversation import Conversation, Message  # noqa: E402
from mercury.models.prospect import Prospect  # noqa: E402
from mercury.state import StateManager  # noqa: E402
from demo_pains import extend_config as extend_pain_config, seed_pains  # noqa: E402

import demo_outbox  # noqa: E402  (scripts/demo_outbox.py: offers, pains, review drafts)
from demo_inbox import seed_inbox  # noqa: E402  (next to this file)

DEFAULT_DB = ROOT / "data" / "demo.db"
REAL_DB = ROOT / "data" / "mercury.db"

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
VOICE_OFFER = "voice"

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
              body="Hi {{first_name}},\n\nI searched \"{{search}}\" this morning. {{company}} "
                   "came up on page two, under three shops with fewer reviews than yours.\n\n"
                   "That gap is usually two or three fixes on the site, not your reputation. "
                   "Want the short list?\n\nJordan"),
    EmailStep(step=2, delay_days=4, subject="Re: {{company}} on page two",
              body="{{first_name}}, one more detail. The three shops above you all take "
                   "bookings online, and Google shows that right in the listing.\n\n"
                   "Want the short list of what they do differently?\n\nJordan"),
    EmailStep(step=3, delay_days=5, subject="Closing the loop",
              body="Last note, {{first_name}}. If page two isn't a priority this season, no "
                   "problem. Happy to send the list whenever timing is better.\n\nJordan"),
]

# What a homeowner types for each trade; the city comes from the company.
SEARCH_TERM = {"Roofing": "roof repair", "HVAC": "furnace repair"}
SEARCHES = {name: f"{SEARCH_TERM[industry]} {location.split(',')[0]}"
            for name, _, industry, location in COMPANIES}

REPLIES = [
    "We noticed we slipped this spring. What would fixing it look like?",
    "Who's above us? Send the list.",
    "How much does something like this run?",
    "We already have someone on our website, but go ahead and send it.",
    "Sure, send it over.",
]


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def fill(text: str, first_name: str, company: str, search: str) -> str:
    return (text.replace("{{first_name}}", first_name).replace("{{company}}", company)
            .replace("{{search}}", search))


def render(text: str, p: Prospect, company: str) -> str:
    return fill(text, p.first_name, company, SEARCHES.get(company, "roof repair Denver"))


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
    # The voice offer promises a line already built for each business, so
    # its emails wait in the Outbox until that contact's demo is ready.
    voice = Campaign(id="", name="Denver trades — after-hours line", sequence=SEQUENCE,
                     status="active", offer_key=VOICE_OFFER, created_at=NOW - timedelta(days=2))
    voice.id = await sm.add_campaign(voice)

    counts = {"prospects": 0, "conversations": 0, "outbox": 0}
    idx = 0
    thread_box: dict[str, str] = {}

    async def outbox(p, pid, company, step, status, send_at, sent_at=None, error="", mailbox="",
                     camp=None):
        s = SEQUENCE[step - 1]
        item = await sm.add_outbox_item(
            prospect_id=pid, to_email=p.email, subject=render(s.subject, p, company),
            body=render(s.body, p, company), send_at=iso(send_at), status=status,
            campaign_id=(camp or campaign).id, step=step, provider="smtp", mailbox=mailbox,
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
                # Three on the voice offer: a demo requested, none registered
                # yet, and one ready (so its emails are not held).
                camp = voice if k < 3 else None
                for step in (1, 2, 3):
                    at = first_at + timedelta(days=(step - 1) * 4 + (step > 2))
                    await outbox(p, pid, cname, step,
                                 "pending_review" if k % 2 else "approved", at, camp=camp)
                if k in (0, 2):
                    demo_id, _ = await sm.request_demo(pid, VOICE_OFFER, "voice")
                    if k == 2:
                        await sm.mark_demo_ready(demo_id, agent_id="agent_demo_summit",
                                                 recording_path="demos/summit-call.mp3",
                                                 built_by="Jordan")
                continue

            # Everyone else got step 1, from whichever mailbox rotation picked
            # (alex only once its ramp had started). The thread stays on it.
            box = (MB_WARM if start.date() < ALEX_START or idx % 2 else MB_ALEX)
            thread_box[pid] = box
            await outbox(p, pid, cname, 1, "sent", start, start + timedelta(minutes=3), mailbox=box)
            step2 = start + timedelta(days=4)
            step3 = start + timedelta(days=9)

            if column == "contacted":
                for step, at in ((2, step2), (3, step3)):
                    if at < NOW:
                        await outbox(p, pid, cname, step, "sent", at, at + timedelta(minutes=2),
                                     mailbox=box)
                    else:
                        # Queued follow-ups inherit the thread's mailbox when sent.
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

            reply = ("prospect", REPLIES[idx % len(REPLIES)])
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
            prospect_id=p.id, to_email=p.email, kind="reply", step=1, provider="smtp",
            mailbox=thread_box.get(p.id, MB_WARM),  # answered from the inbox it came to
            subject=f"Re: {p.company} on page two",
            body=f"Hi {p.first_name}, happy to walk through it. Does Tuesday or Wednesday morning work?",
            send_at=iso(NOW - timedelta(days=1) if i == 0 else NOW + timedelta(hours=3)),
            status="sent" if i == 0 else "pending_review",
        )
        if i == 0:
            await sm.update_outbox_item(item_id, sent_at=iso(NOW - timedelta(days=1) + timedelta(minutes=4)))
        counts["outbox"] += 1

    counts.update(await seed_inbox(sm, NOW))
    counts.update(await seed_history(sm))
    counts.update(await seed_warmup(sm))
    counts.update(await seed_placement(sm))
    counts.update(await seed_registry(sm, NOW))
    counts.update(await seed_pains(sm))
    counts.update(await seed_personas(sm))
    counts.update(await seed_signals(sm, companies))
    return counts


PERSONAS = [
    {"name": "Plain local", "avatar_seed": "mercury-persona-03", "sign_name": "Jordan",
     "description": "Short, neighborly, one concrete observation per email.",
     "tone": "plain, friendly, specific", "examples": "",
     "instructions": "Lead with what you saw. One question. No pitch in the first email."},
    {"name": "Numbers first", "avatar_seed": "mercury-persona-11", "sign_name": "Jordan",
     "description": "Opens with the data point, then the gap it implies.",
     "tone": "direct, factual, brief", "examples": "",
     "instructions": "Open with the number. Say what it costs them. Offer the list."},
]


async def seed_personas(sm: StateManager) -> dict:
    """Two writing voices, and a saved generation behind every board email, so
    the Outbox shows which voice wrote each draft (instead of "Unknown
    persona") and the Personas tab has sends and replies per version. No
    model is called: each generation records the seeded text as its output."""
    from types import SimpleNamespace

    from mercury.personas import PersonaStore

    config = SimpleNamespace(
        product=SimpleNamespace(name="Local search audit", description="A short list of fixes."),
        persona=SimpleNamespace(name="Jordan Hale", company="Northwind Outreach", email=MB_WARM,
                                tone="professional, consultative, confident"),
    )
    store = PersonaStore(sm)
    await store.ensure_default(config)  # the imported "Workspace voice", as on a real install
    ids = [await store.save(dict(p)) for p in PERSONAS]
    await store.set_default(ids[0])
    profiles = {p["id"]: p for p in await store.list()}
    async with sm._connect() as db:
        async with db.execute(
            "SELECT id, prospect_id, subject, body, kind FROM outbox "
            "WHERE prospect_id NOT LIKE 'hist%' AND COALESCE(generation_id, '') = '' "
            "ORDER BY prospect_id, step"
        ) as cur:
            rows = await cur.fetchall()
    n, order = 0, {}
    for item_id, prospect_id, subject, body, kind in rows:
        # A contact keeps one voice across its sequence; a third get the challenger.
        persona = ids[1] if order.setdefault(prospect_id, len(order)) % 3 == 2 else ids[0]
        gen = await store.record(
            profiles[persona], config, "Seeded by scripts/seed_demo.py; no model call.",
            {"subject": subject, "body": body}, "reply" if kind == "reply" else "sequence",
        )
        async with sm._connect() as db:
            await db.execute("UPDATE outbox SET generation_id = ? WHERE id = ?", (gen, item_id))
            await db.execute(
                "INSERT INTO email_generation_history (outbox_id, generation_id, original_subject, "
                "original_body) VALUES (?, ?, ?, ?)", (item_id, gen, subject, body),
            )
            await db.commit()
        n += 1
    return {"generations": n}


# Signals the demo workspace has confirmed. The rest stay proposed, so the
# Signals tab still has something to decide.
CONFIRMED = ["FOUND_IN_SERP", "REVIEW_COUNT", "REVIEW_RATING", "NO_ONLINE_BOOKING",
             "RUNNING_GOOGLE_ADS", "RUNNING_META_ADS", "INCUMBENT_AGENCY", "NO_SCHEMA_MARKUP",
             "BLOG_STALE", "TECH_STACK", "HIRING_ROLE", "EMAIL_STATUS"]
AGENCIES = ["Peakline Digital", "Front Range Web Co", "Summit Local Marketing"]
STACKS = ["WordPress, Google Tag Manager", "Wix", "Squarespace, Google Analytics",
          "WordPress, HubSpot", "Duda"]
HIRING = {"Roofing": "Estimator", "HVAC": "HVAC Service Technician"}


async def seed_signals(sm: StateManager, companies: list) -> dict:
    """Confirm a working set of signals and record what Mercury would have
    observed for each company: listing data, then what profiling read off the
    homepage. Two companies are left unread, so Today still has a nudge."""
    from mercury.signals import seed_signal_catalog

    rnd = random.Random(23)
    await seed_signal_catalog(sm)
    for code in CONFIRMED:
        await sm.set_signal_status(code, "confirmed")
    industry = {name: ind for name, _, ind, _ in COMPANIES}
    rows = []

    def obs(cid, code, collector, num=None, text="", url=""):
        rows.append({"company_id": cid, "signal_code": code, "collector": collector,
                     "value_num": num, "value_text": text, "confidence": 0.9,
                     "evidence_url": url,
                     "observed_at": iso(NOW - timedelta(days=rnd.randint(2, 18)))})

    for i, (cid, name, domain) in enumerate(companies):
        site = f"https://{domain}/"
        obs(cid, "FOUND_IN_SERP", "discover", text=SEARCHES[name])
        obs(cid, "REVIEW_COUNT", "discover", num=rnd.randint(18, 260))
        obs(cid, "REVIEW_RATING", "discover", num=round(rnd.uniform(3.7, 4.9), 1))
        if i >= 10:
            continue  # never profiled
        obs(cid, "NO_ONLINE_BOOKING", "profile", num=float(i % 3 != 0), url=site)
        obs(cid, "RUNNING_GOOGLE_ADS", "profile", num=float(i % 2 == 0), url=site)
        obs(cid, "RUNNING_META_ADS", "profile", num=float(i % 4 == 1), url=site)
        obs(cid, "NO_SCHEMA_MARKUP", "profile", num=float(i % 5 != 2), url=site)
        obs(cid, "TECH_STACK", "profile", text=STACKS[i % len(STACKS)], url=site)
        if i % 3 == 1:
            obs(cid, "INCUMBENT_AGENCY", "profile", text=AGENCIES[i % len(AGENCIES)], url=site)
        if i % 4 == 3:
            obs(cid, "BLOG_STALE", "profile", num=rnd.randint(220, 700), url=site + "sitemap.xml")
        if i in (1, 6):
            obs(cid, "HIRING_ROLE", "profile", text=HIRING[industry[name]], url=site + "careers")
    await sm.add_observations(rows, run_id="demo-seed")

    async with sm._connect() as db:
        async with db.execute(
            "SELECT id, company_id, email_status FROM prospects WHERE email_status != ''"
        ) as cur:
            people = await cur.fetchall()
    await sm.add_observations([
        {"company_id": company_id, "prospect_id": pid, "signal_code": "EMAIL_STATUS",
         "collector": "verify", "value_text": status, "confidence": 0.95}
        for pid, company_id, status in people
    ], run_id="demo-seed")
    return {"observations": len(rows) + len(people)}


async def seed_placement(sm: StateManager) -> dict:
    """One placement test from yesterday, so the Mailboxes domain drawer and
    the Today card have a result to show: the warm inbox lands in the inbox,
    the warming one mostly in spam, and the control in Primary, which reads
    as a domain problem for getnorthwind.com. No email is sent."""
    from mercury import placement

    await placement.ensure_schema(sm.db_path)
    run_id = "demo0001"
    at = iso(NOW - timedelta(days=1))
    landed = {
        MB_WARM: ("primary", "inbox", "inbox"),
        MB_ALEX: ("spam", "inbox", "spam"),
        "you@gmail.com": ("primary", "inbox", "inbox"),
    }
    seeds = (("seed.northwind@gmail.com", "gmail"), ("seed.northwind@outlook.com", "outlook"),
             ("seed.northwind@yahoo.com", "yahoo"))
    n = 0
    for sender, folders in landed.items():
        for (seed, provider), folder in zip(seeds, folders):
            n += 1
            await placement._insert(sm.db_path, {
                "id": f"demo-pl-{n}", "run_id": run_id, "sender": sender,
                "role": "control" if sender == "you@gmail.com" else "fleet",
                "domain": sender.split("@")[1], "seed": seed, "seed_provider": provider,
                "message_id": f"<demo-pl-{n}@{sender.split('@')[1]}>",
                "subject": "Summit Peak Roofing on page two", "folder": folder, "sent_at": at,
            })
    async with sm._connect() as db:
        await db.execute("UPDATE placement_tests SET created_at = ?, checked_at = ? WHERE run_id = ?",
                         (at, at, run_id))
        await db.commit()
    await placement.finish(sm, run_id)
    return {"placement_rows": n}


# ── The demo mail config (mailboxes come from config, not the DB) ──

MB_WARM = "jordan@northwind-outreach.com"      # warm, persona email -> owns pre-rotation rows
MB_ALEX = "alex@getnorthwind.com"        # warming
MB_SAM = "sam@trynorthwind.com"          # scheduled
ALEX_START_DAYS_AGO = 16            # day 17: week 3 of the ramp (15/day)
SAM_START_IN_DAYS = 3
DAILY_CAP = 30
INITIAL_CAP, WEEKLY_INCREASE = 5, 5
DEMO_PASSWORD_ENV = "MAILBOX_DEMO_PASSWORD"

TODAY = NOW.date()
ALEX_START = TODAY - timedelta(days=ALEX_START_DAYS_AGO)


def demo_config(template: Path) -> dict:
    """The tracked template with a demo persona and three SMTP mailboxes."""
    cfg = yaml.safe_load(template.read_text()) or {}
    cfg.setdefault("persona", {}).update({
        "name": "Jordan Hale", "company": "Northwind Outreach", "email": MB_WARM,
    })
    email = cfg.setdefault("channels", {}).setdefault("email", {})
    email.update({
        "provider": "smtp", "max_daily_sends": 50, "require_approval": True,
        "auto_approve_followups": True,
        "warmup_initial_cap": INITIAL_CAP, "warmup_weekly_increase": WEEKLY_INCREASE,
        # The demo inboxes stand in for Workspace seats, so they get Gmail's
        # ceiling rather than the generic SMTP one.
        "provider_daily_ceilings": {"smtp": DAILY_CAP},
        "mailboxes": [
            {"email": MB_WARM, "name": "Jordan | Northwind", "password_env": DEMO_PASSWORD_ENV,
             "smtp_host": "smtp.invalid", "daily_cap": DAILY_CAP},
            {"email": MB_ALEX, "name": "Alex | Northwind", "password_env": DEMO_PASSWORD_ENV,
             "smtp_host": "smtp.invalid", "daily_cap": DAILY_CAP,
             "warmup_start": ALEX_START.isoformat()},
            {"email": MB_SAM, "name": "Sam | Northwind", "password_env": DEMO_PASSWORD_ENV,
             "smtp_host": "smtp.invalid", "daily_cap": DAILY_CAP,
             "warmup_start": (TODAY + timedelta(days=SAM_START_IN_DAYS)).isoformat()},
        ],
    })
    cfg["icp"] = {**cfg.get("icp", {}),
                  "industries": ["Roofing", "HVAC"], "company_size": "2-50 employees",
                  "titles": ["Owner", "General Manager", "Office Manager"],
                  "geography": ["Denver, CO", "Boulder, CO", "Lakewood, CO"]}
    cfg["compliance"] = {"postal_address": "1550 Wewatta St, Denver, CO 80202"}
    cfg["offers"] = [{"key": VOICE_OFFER, "requires_demo": True, "demo_kind": "voice"}]
    cfg["offers"] += demo_outbox.demo_offers()
    cfg.setdefault("usage", {}).setdefault("quiet_hours", {})["timezone"] = "UTC"
    extend_pain_config(cfg)
    return cfg


def write_demo_config(db_path: Path) -> Path:
    path = db_path.with_suffix(".mercury.yaml")
    protected = {(ROOT / n).resolve() for n in ("mercury.yaml", "mercury.local.yaml",
                                                "harvey.local.yaml")}
    if path.resolve() in protected:
        raise SystemExit(f"Refusing to overwrite {path}.")
    header = ("# Demo mail config written by scripts/seed_demo.py. No secrets:\n"
              f"# every mailbox reads {DEMO_PASSWORD_ENV}. Dates are relative to the\n"
              "# day it was seeded; re-run the seed to refresh them.\n")
    path.write_text(header + yaml.safe_dump(demo_config(ROOT / "mercury.yaml"),
                                            sort_keys=False, allow_unicode=True))
    return path


# ── Trends history + warm-up ─────────────────────────────────────────

HISTORY_DAYS = 60

HIST_PREFIX = ["summit", "peak", "ridge", "canyon", "aspen", "granite", "mesa", "pine",
               "foothill", "timberline", "redrock", "bluebird", "highplains", "clearcreek"]
HIST_TRADE = ["roofing", "hvac", "exteriors", "heating", "mechanical", "roofco", "air"]
HIST_FIRST = ["owner", "info", "mike", "sarah", "dave", "jen", "tom", "kate", "luis", "amy"]


def _alex_cap(day: date) -> int:
    return warmup_cap(DAILY_CAP, ALEX_START, day, INITIAL_CAP, WEEKLY_INCREASE)


async def seed_history(sm: StateManager) -> dict:
    """~60 days of outreach so /api/trends 30 and 90 have something to show.

    Earlier history comes from a finished campaign on the warm mailbox; since
    alex@'s ramp started it carries a share too, kept under its ramp caps
    (the sender enforces them, so the demo shouldn't show them broken).
    History rows reference archived prospect ids that aren't on the board,
    so the pipeline stays exactly as seeded above.
    """
    rnd = random.Random(11)
    archived = Campaign(id="", name="Front Range trades — spring list", sequence=SEQUENCE,
                        status="completed", created_at=NOW - timedelta(days=HISTORY_DAYS + 2))
    archived.id = await sm.add_campaign(archived)

    async with sm._connect() as db:
        async with db.execute(
            "SELECT date(sent_at), COALESCE(mailbox, ''), COUNT(*) FROM outbox "
            "WHERE status = 'sent' GROUP BY 1, 2"
        ) as cur:
            existing = {(d, m): n for d, m, n in await cur.fetchall()}

    outbox_rows, events = [], []
    week_rows: list[tuple[str, str, str]] = []     # (pid, email, sent_at) on jordan@, last 7 days
    n = 0
    for days_ago in range(HISTORY_DAYS - 1, -1, -1):
        day = TODAY - timedelta(days=days_ago)
        weekend = day.weekday() >= 5
        progress = 1 - days_ago / HISTORY_DAYS
        # Caps are enforced over a rolling 24 hours, so yesterday evening and
        # today together stay under each mailbox's cap.
        last_two = days_ago <= 1
        share = 0.4 if last_two else rnd.uniform(0.75, 0.95)
        plan = {MB_WARM: rnd.randint(0, 3) if weekend else int(rnd.uniform(8, 13) + 8 * progress)}
        if day >= ALEX_START:
            plan[MB_ALEX] = int(_alex_cap(day) * share)
        if last_two:
            plan[MB_WARM] = min(plan[MB_WARM], 10)
        # Reply rate improves over the period (copy got better), bounces fall.
        p_reply = 0.05 + 0.035 * progress
        p_bounce = 0.028 - 0.016 * progress

        for mailbox, target in plan.items():
            volume = max(0, target - existing.get((day.isoformat(), mailbox), 0))
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
                    fill(s.subject, "there", "your shop", "roof repair Denver"),
                    fill(s.body, "there", "your shop", "roof repair Denver"),
                    iso(sent_at), iso(sent_at), mailbox,
                ))
                recent = NOW - sent_at < timedelta(days=6, hours=20)
                if recent and mailbox == MB_WARM:
                    week_rows.append((pid, email, iso(sent_at)))
                # Bounces in the health window are placed below on purpose.
                if not recent and rnd.random() < p_bounce:
                    at = min(sent_at + timedelta(minutes=rnd.randint(1, 40)), NOW)
                    events.append(("bounce", {"prospect": email, "prospect_id": pid,
                                              "mailbox": mailbox}, iso(at)))
                    continue
                if rnd.random() < p_reply:
                    at = min(sent_at + timedelta(hours=rnd.randint(1, 60)), NOW)
                    roll = rnd.random()
                    intent = ("interested" if roll < 0.38 else "ooo" if roll < 0.48 else
                              "question" if roll < 0.68 else "objection" if roll < 0.82
                              else "not_interested")
                    events.append(("reply_received",
                                   {"prospect_id": pid, "prospect_email": email,
                                    "intent": intent, "mailbox": mailbox}, iso(at)))

    # jordan@ this week: ~4% bounces — over the 3% hold line, under the 5%
    # pause line — so the demo shows a mailbox on hold. alex@ stays clean.
    async with sm._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'sent' AND kind = 'sequence' "
            "AND mailbox = ? AND datetime(sent_at) >= datetime('now', '-7 days')", (MB_WARM,)
        ) as cur:
            already = (await cur.fetchone())[0]
    week_total = already + len(week_rows)
    k = max(1, math.floor(week_total * 0.04))
    while k / max(1, week_total) <= 0.03:
        k += 1
    for pid, email, sent_at in rnd.sample(week_rows, min(k, len(week_rows))):
        at = min(datetime.fromisoformat(sent_at) + timedelta(minutes=12), NOW)
        events.append(("bounce", {"prospect": email, "prospect_id": pid,
                                  "mailbox": MB_WARM}, iso(at)))

    async with sm._connect() as db:
        await db.executemany(
            """INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email,
                                   subject, body, status, send_at, sent_at, provider, mailbox)
               VALUES (?, ?, ?, ?, 'sequence', ?, ?, ?, 'sent', ?, ?, 'smtp', ?)""",
            outbox_rows,
        )
        await db.commit()
    for action_type, details, at in events:
        await sm.log_action(action_type, "handler", details, created_at=at)
    return {"history_sends": len(outbox_rows), "history_events": len(events)}


async def seed_warmup(sm: StateManager) -> dict:
    """The overlay: checklist progress and notes. Caps and dates are config."""
    await sm.add_warmup_inbox(MB_ALEX, notes="Google Workspace on the secondary domain, bought for outreach.")
    done = ["secondary_domain", "domain_redirect", "profile", "personal_emails",
            "newsletters", "verified_only", "reply_same_day", "plain_text", "bounce_check"]
    await sm.update_warmup_inbox(MB_ALEX, tasks_json=json.dumps({k: True for k in done}))
    await sm.add_warmup_inbox(MB_WARM, notes="The original inbox. Warm since spring.")
    await sm.update_warmup_inbox(MB_WARM, tasks_json=json.dumps(
        {k: True for k in ("secondary_domain", "profile", "verified_only", "postmaster")}))
    await sm.add_warmup_inbox(MB_SAM, notes="Second Workspace seat; ramp starts in three days.")
    await sm.update_warmup_inbox(MB_SAM, tasks_json=json.dumps({"secondary_domain": True,
                                                                 "profile": True}))
    return {"warmup_inboxes": 3}


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_DB
    target = target if target.is_absolute() else (Path.cwd() / target)
    if target.resolve() == REAL_DB.resolve():
        print(f"Refusing to overwrite the real database at {REAL_DB}.", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    counts = asyncio.run(seed(target))
    config_path = write_demo_config(target)
    counts.update(asyncio.run(demo_outbox.seed_outbox_review(target, config_path)))
    print(f"Seeded {target}: {counts['prospects']} prospects, "
          f"{len(COMPANIES)} companies, {counts['conversations']} conversations, "
          f"{counts['outbox']} outbox rows, {counts['history_sends']} historical sends, "
          f"{counts['history_events']} reply/bounce events, "
          f"{counts['warmup_inboxes']} warm-up overlays, "
          f"{counts['pains']} pains ({counts['pain_emails']} emails tagged).")
    print(f"Demo mail config: {config_path}")
    print(f"Run: MERCURY_DB_PATH={target} MERCURY_CONFIG={config_path} "
          f"{DEMO_PASSWORD_ENV}=demo env -u APPIMAGE .venv/bin/mercury dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
