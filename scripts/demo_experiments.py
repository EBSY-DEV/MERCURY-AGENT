"""Demo data for the Experiments tab (called once from seed_demo.py).

Three synthetic experiments, so every state of the screen has something to show:

* a running opening-angle test, 86 contacted per arm, 64 per arm past their
  14-day window: the result reads "Not enough data yet" (100 are needed);
* a draft subject-line test, nothing enrolled;
* a completed persona test, 60 per arm, "No clear difference".

Everything goes through the real service (create, start, complete) and the
real tables, so the numbers on screen are the ones the results code computes.
Prospects are made up (example.com addresses); nothing is ever sent. They
are marked contacted and show up on the Contacts and Pipeline screens, plus
a few dozen unassigned new ones, which is what a new experiment would draw on.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

from mercury import experiments as ex
from mercury.config import ExperimentsConfig
from mercury.control.context import OperatorContext
from mercury.control.experiments import ExperimentService
from mercury.models.prospect import Prospect
from mercury.personas import AVATAR_SEEDS, PersonaStore

FIRST = ["Alex", "Blake", "Casey", "Devon", "Emery", "Finley", "Harper", "Jamie", "Kai", "Logan",
         "Morgan", "Noel", "Parker", "Quinn", "Reese", "Sasha", "Taylor", "Wren"]
LAST = ["Abbott", "Bishop", "Carver", "Dalton", "Ellis", "Foster", "Garner", "Holt", "Irwin",
        "Jensen", "Keller", "Lowell", "Mercer", "Nolan", "Osborne", "Prescott", "Quigley", "Rowan"]
TRADES = ["Dental", "Physio", "Optical", "Vet", "Chiropractic", "Podiatry", "Skin", "Hearing"]
PLACES = ["Harbor", "Cedar", "Summit", "Lakeside", "Maple", "Riverside", "Foothill", "Granite"]


def config():
    """Just what the service and the persona store read."""
    return SimpleNamespace(
        persona=SimpleNamespace(name="Sam Rivera", email="sam@example.com", company="Example Co",
                                role="BD", tone="direct"),
        product=SimpleNamespace(name="offer_a", description="A scheduling tool.", pricing="",
                                key_benefits=["fewer no-shows"]),
        experiments=ExperimentsConfig())


def iso(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def rfc(when: datetime) -> str:
    return format_datetime(when.replace(tzinfo=timezone.utc))


async def seed_experiments(sm, now: datetime, mailbox: str, rnd: random.Random | None = None) -> dict:
    rnd = rnd or random.Random(11)
    cfg = config()
    store = PersonaStore(sm)
    await store.ensure_default(cfg)
    plain = await store.save({
        "name": "Plain and direct", "description": "Short, factual, no fluff.",
        "avatar_seed": AVATAR_SEEDS[1], "sign_name": "Sam", "tone": "plain and direct",
        "instructions": "Keep sentences short. One idea per email.", "examples": ""})
    warm = await store.save({
        "name": "Warm and personal", "description": "Friendly, a little more chatty.",
        "avatar_seed": AVATAR_SEEDS[2], "sign_name": "Sam", "tone": "warm and personal",
        "instructions": "Sound like a neighbour. One idea per email.", "examples": ""})

    def service(when: datetime):
        return ExperimentService(OperatorContext.local("cli"), sm, cfg, clock=lambda: when)

    counts = {"experiments": 3, "experiment_prospects": 0}
    n = [0]

    async def prospect(status: str) -> str:
        n[0] += 1
        trade, place = rnd.choice(TRADES), rnd.choice(PLACES)
        first, last = rnd.choice(FIRST), rnd.choice(LAST)
        return await sm.add_prospect(Prospect(
            first_name=first, last_name=last, title="Owner", status=status, score=rnd.randint(40, 90),
            email=f"{first}.{last}{n[0]}@{place.lower()}{trade.lower()}{n[0]}.example.com".lower(),
            email_status="verified", company=f"{place} {trade} {n[0]}", industry="segment_a"))

    async def populate(exp: dict, counts_by_arm: dict, *, mature_days: tuple, pending_days: tuple,
                       positives: dict, replies: dict, opt_out_arm: str = "", bounce_arm: str = "",
                       pending: int = 0, subject: str):
        """Give an experiment its sent openers and replies. counts_by_arm is the number
        of mature prospects per arm; `pending` more per arm are still inside their window."""
        arms = {a["arm_key"]: a for a in exp["arms"]}
        marker_gen = {}
        for key, arm in arms.items():
            arm_profile = await store.resolve(cfg, arm["persona_version_id"])
            marker = {"experiment_id": exp["id"], "revision_id": exp["revision_id"],
                      "arm_id": arm["id"], "arm_key": key, "instruction": arm["instruction"] or ""}
            marker_gen[key] = await store.record(arm_profile | {"experiment": marker}, cfg, "prompt",
                                                 {}, "personal_email")
        slots = {key: [("mature", i) for i in range(counts_by_arm[key])]
                 + [("pending", i) for i in range(pending)] for key in arms}
        total = {key: len(slots[key]) for key in arms}
        done = {key: 0 for key in arms}
        reply_idx = {key: set(rnd.sample(range(counts_by_arm[key]), replies[key])) for key in arms}
        positive_idx = {key: set(rnd.sample(sorted(reply_idx[key]), positives[key])) for key in arms}
        spare = [i for i in sorted(reply_idx[opt_out_arm] - positive_idx[opt_out_arm])] if opt_out_arm else []
        opt_idx = {spare[0]} if spare else set()
        bounce_idx = ({rnd.choice([i for i in range(counts_by_arm[bounce_arm]) if i not in reply_idx[bounce_arm]])}
                      if bounce_arm else set())

        conn = sqlite3.connect(sm.db_path)
        try:
            while any(done[k] < total[k] for k in arms):
                pid = await prospect("contacted")
                key = ex.arm_for_bucket(ex.assignment_bucket(exp["id"], exp["revision"], pid), exp["allocation_a"])
                if done[key] >= total[key]:
                    conn.execute("UPDATE prospects SET status = 'new' WHERE id = ?", (pid,))
                    conn.commit()
                    continue
                kind, i = slots[key][done[key]]
                done[key] += 1
                lo, hi = mature_days if kind == "mature" else pending_days
                span = counts_by_arm[key] if kind == "mature" else pending
                day = lo + (hi - lo) * i / max(1, span - 1)
                sent = now + timedelta(days=day, hours=rnd.randint(-4, 2), minutes=rnd.randint(0, 59))
                sent = min(sent, now - timedelta(minutes=5))
                conn.execute("INSERT INTO experiment_assignments VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (exp["id"], pid, exp["revision_id"], arms[key]["id"], key,
                              ex.assignment_bucket(exp["id"], exp["revision"], pid), iso(sent - timedelta(hours=1))))
                conn.commit()
                row = (await sm.get_prospect(pid))
                item = await sm.add_outbox_item(
                    prospect_id=pid, to_email=row.email, subject=subject, send_at=iso(sent),
                    body=f"Hi {row.first_name}, a short note about booking at {row.company}.",
                    status="approved", campaign_id=f"exp-{exp['id']}-{key}", step=1, mailbox=mailbox,
                    provider="smtp", generation_id=marker_gen[key])
                message_id = f"<{item}@example.com>"
                await sm.update_outbox_item(item, status="sent", sent_at=iso(sent),
                                            message_id=message_id, mailbox=mailbox)
                if kind != "mature":
                    continue
                answered = (i in reply_idx[key]) or (key == bounce_arm and i in bounce_idx)
                if not answered:
                    continue
                when = sent + timedelta(days=rnd.randint(1, 6), hours=rnd.randint(0, 5))
                if key == bounce_arm and i in bounce_idx:
                    msg, _ = await sm.record_inbound(
                        provider="smtp", mailbox=mailbox, external_id=f"b-{item}", in_reply_to=message_id,
                        from_email="mailer-daemon@example.net", subject="Delivery Status Notification (Failure)",
                        body="Status: 5.1.1 user unknown", kind="bounce", date_header=rfc(sent + timedelta(minutes=2)))
                    await sm.link_inbound(msg["id"], prospect_id=pid, intent="bounce")
                    await sm.finish_inbound(msg["id"], "processed")
                    continue
                intent = ("interested" if i in positive_idx[key] else
                          "unsubscribe" if i in opt_idx and key == opt_out_arm else
                          rnd.choice(["question", "not_interested", "objection"]))
                text = {"interested": "Yes, happy to talk. Send me a time.",
                        "unsubscribe": "Please remove me from your list.",
                        "question": "How does this work with our current setup?",
                        "not_interested": "Not for us, thanks.",
                        "objection": "Maybe later in the year."}[intent]
                msg, _ = await sm.record_inbound(
                    provider="smtp", mailbox=mailbox, external_id=f"r-{item}", rfc_message_id=f"<r-{item}@{row.email.split('@')[1]}>",
                    in_reply_to=message_id, thread_references=message_id, from_email=row.email,
                    subject="Re: " + subject, body=text, date_header=rfc(when))
                await sm.link_inbound(msg["id"], prospect_id=pid, intent=intent)
                await sm.finish_inbound(msg["id"], "processed")
        finally:
            conn.close()
        counts["experiment_prospects"] += sum(total.values())

    # ── Running: observation opener vs question opener ──
    started_at = now - timedelta(days=27)
    svc = service(started_at)
    running = (await svc.create({
        "name": "Observation opener vs question opener",
        "hypothesis": "Opening with a question gets more positive replies than opening with an "
                      "observation, for prospects in segment_a.",
        "variable": "opening_angle",
        "arms": [{"name": "Observation opener", "instruction": "Open with one fact about the business.",
                  "persona_id": plain},
                 {"name": "Question opener", "instruction": "Open with one question about the business.",
                  "persona_id": plain}],
        "cohort": {"industries": ["segment_a"]}, "allocation_a": 50,
        "enroll_until": iso(now + timedelta(days=13))[:10], "response_window_days": 14,
        "min_per_arm": 100, "min_duration_days": 14, "primary_metric": "positive_reply_rate"}))["experiment"]
    running = (await svc.start(running["id"]))["experiment"]
    await populate(running, {"A": 64, "B": 64}, mature_days=(-26, -15), pending_days=(-13, -2),
                   positives={"A": 5, "B": 7}, replies={"A": 9, "B": 10}, opt_out_arm="B",
                   bounce_arm="B", pending=22, subject="quick question")

    # ── Draft: two subject line styles ──
    await service(now - timedelta(days=1)).create({
        "name": "Two subject line styles",
        "hypothesis": "A short subject gets opened more than a specific one.",
        "variable": "subject_line",
        "arms": [{"name": "Short subject", "instruction": "Use a subject of three words or fewer.",
                  "persona_id": plain},
                 {"name": "Specific subject", "instruction": "Use a subject that names one detail of their business.",
                  "persona_id": plain}],
        "cohort": {"industries": ["segment_a"]}, "allocation_a": 50, "response_window_days": 14,
        "min_per_arm": 100, "min_duration_days": 14})

    # ── Completed: plain voice vs warm voice ──
    long_ago = now - timedelta(days=62)
    svc = service(long_ago)
    done = (await svc.create({
        "name": "Plain voice vs warm voice",
        "hypothesis": "A warmer voice earns more positive replies than a plain one.",
        "variable": "persona",
        "arms": [{"name": "Plain voice", "persona_id": plain}, {"name": "Warm voice", "persona_id": warm}],
        "cohort": {}, "allocation_a": 50, "response_window_days": 14, "min_per_arm": 50,
        "min_duration_days": 14}))["experiment"]
    done = (await svc.start(done["id"]))["experiment"]
    await populate(done, {"A": 60, "B": 60}, mature_days=(-60, -36), pending_days=(0, 0),
                   positives={"A": 4, "B": 5}, replies={"A": 7, "B": 8}, subject="booking at your practice")
    await service(now - timedelta(days=21)).complete(done["id"], confirm=True)
    return counts
