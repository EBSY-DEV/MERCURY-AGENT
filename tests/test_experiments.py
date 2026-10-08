"""A/B experiments (issue #6): assignment, exposure, controls, outcome
attribution and statistics.

Real SQLite databases, the real Writer, Sender and Handler with fake mail
providers and fake brains, a fake clock for observation windows, and
synthetic people, companies and reply text only.
"""

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mercury import experiments as ex
from mercury.config import ExperimentsConfig, ProductConfig
from mercury.control.context import OperatorContext
from mercury.control.errors import Conflict, Invalid
from mercury.control.experiments import ExperimentService
from mercury.experiment_stats import newcombe_difference, wilson
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.models.prospect import Prospect
from mercury.personas import AVATAR_SEEDS, PersonaStore
from mercury.state import MIGRATIONS, StateManager
from tests.test_outbox_native import FakeProvider

MAILBOX = "sam@example.com"


def drop_experiment_schema(conn) -> None:
    """Undo the experiments migration (v26) on an open sqlite3 connection (for tests that roll
    a database back to an older version)."""
    for trigger in ("outbox_experiment_exposure", "outbox_experiment_exposure_regen",
                    "experiment_revisions_frozen", "experiment_arms_frozen",
                    "experiment_arms_frozen_delete", "experiment_assignments_no_update"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP VIEW IF EXISTS experiment_generations")
    conn.execute("DROP INDEX IF EXISTS idx_outbox_experiment")
    for table in ("experiment_outcomes", "experiment_assignments", "experiment_arms",
                  "experiment_revisions", "experiments"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    for column in ("experiment_arm_id", "experiment_revision_id", "experiment_id"):
        if column in columns:
            conn.execute(f"ALTER TABLE outbox DROP COLUMN {column}")


def config(**experiments):
    email = SimpleNamespace(enabled=True, provider="smtp", max_daily_sends=200,
                            send_to_risky=False, require_approval=False, max_bounce_rate=0.9,
                            mailboxes=[], warmup_initial_cap=200, warmup_weekly_increase=5,
                            auto_approve_followups=False, spread_sends=False,
                            thread_followups=True)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Sam Rivera", email=MAILBOX, company="Example Co",
                                role="BD", tone="direct"),
        channels=SimpleNamespace(email=email, linkedin=SimpleNamespace(enabled=False)),
        product=ProductConfig(name="offer_a", description="A scheduling tool.", pricing="$",
                              key_benefits=["fewer no-shows"], objection_responses={}),
        icp=SimpleNamespace(markets=[]),
        compliance=SimpleNamespace(postal_address="1 Example St, Springfield",
                                   opt_out_line_en="Reply unsubscribe to opt out.",
                                   opt_out_line_es="Responde baja."),
        usage=SimpleNamespace(heartbeat_interval_minutes=15,
                              quiet_hours=SimpleNamespace(start="22:00", end="07:00",
                                                          timezone="UTC")),
        experiments=ExperimentsConfig(**experiments),
    )


def now():
    """Naive UTC, whole seconds, rounded up: never before a row just written."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)


def rfc(when: datetime) -> str:
    return format_datetime(when.replace(tzinfo=timezone.utc))


class WriterBrain:
    """Canned sequences and drafts; records every prompt with its task."""

    def __init__(self, on_call=None, outcome=None):
        self.calls = []
        self.on_call = on_call
        self.outcome = outcome

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think_json(self, prompt, session_id=None, agent="", task=""):
        self.calls.append((task, prompt))
        if self.on_call:
            await self.on_call(task)
        if task == "write_sequence":
            return [
                {"step": 1, "subject": "quick question", "delay_days": 0,
                 "body": "Hi {{first_name}}, is booking still manual at {{company}}?"},
                {"step": 2, "subject": "one idea", "delay_days": 3,
                 "body": "Hi {{first_name}}, teams like {{company}} cut no-shows with reminders. "
                         "Worth a look? It takes a minute to explain. Happy to share how."},
                {"step": 3, "subject": "closing the loop", "delay_days": 4,
                 "body": "Hi {{first_name}}, should I stop writing? No problem either way."},
            ]
        if task == "personal_email":
            return {"subject": "booking at your clinic",
                    "body": "Saw your team books by phone. Is that still working for you?"}
        if task == "classify_outcome":
            return self.outcome
        return None


class ReplyBrain:
    """The handler's brain: an intent from keywords in the reply."""

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task="", **_kw):
        if task == "classify_intent":
            text = prompt.lower()
            for word, intent in (("let's talk", "interested"), ("how much", "question"),
                                 ("not for us", "not_interested"), ("maybe", "objection")):
                if word in text:
                    return intent
            return "question"
        return "Thanks for the note. Does Tuesday work?"

    async def think_json(self, *a, **k):
        return None


@pytest_asyncio.fixture
async def world():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(os.path.join(tmp, "x.db"))
        await state.init_db()
        cfg = config()
        svc = await ExperimentService(OperatorContext.local("cli"), state, cfg).ready()
        yield SimpleNamespace(state=state, config=cfg, svc=svc, tmp=tmp)


def definition(**overrides):
    data = {
        "name": "Opening angle test", "hypothesis": "A question opener earns more replies.",
        "variable": "opening_angle",
        "arms": [{"name": "variant A", "instruction": "Open with a question about their week."},
                 {"name": "variant B", "instruction": "Open with an observation from their site."}],
        "response_window_days": 14, "min_per_arm": 3, "min_duration_days": 0,
    }
    data.update(overrides)
    return data


async def add_prospects(state, n, *, prefix="p", score=0, industry="dental", status="new"):
    ids = []
    for i in range(n):
        ids.append(await state.add_prospect(Prospect(
            first_name=f"Pat{i}", last_name="Example", title="Owner",
            company=f"Clinic {prefix}{i}", email=f"{prefix}{i}@clinic{prefix}{i}.example.com",
            email_status="verified", status=status, score=score, industry=industry)))
    return ids


async def add_prospects_in_both_arms(state, experiment_id, n, revision=1):
    """At least ``n`` new prospects, with at least one landing in each arm,
    so a test never depends on how a random experiment id happens to hash."""
    ids, arms = [], set()
    while len(ids) < n or arms != {"A", "B"}:
        (pid,) = await add_prospects(state, 1, prefix=f"c{len(ids)}-")
        ids.append(pid)
        arms.add(ex.arm_for_bucket(ex.assignment_bucket(experiment_id, revision, pid), 50))
    return ids


async def started(world, **overrides):
    created = await world.svc.create(definition(**overrides))
    return (await world.svc.start(created["experiment"]["id"]))["experiment"]


def rows(state, sql, params=()):
    conn = sqlite3.connect(state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def make_writer(world, brain=None):
    from mercury.agents.writer import Writer

    return Writer(brain or WriterBrain(), world.state, world.config,
                  SimpleNamespace(smtp_username="", instantly_api_key=""))


def make_sender(world, provider, clock):
    from mercury.agents.sender import Sender

    sender = Sender(brain=None, state=world.state, config=world.config,
                    env=SimpleNamespace(instantly_api_key=""))
    sender.mailboxes = MailboxPool([Mailbox(email=MAILBOX, provider=provider, daily_cap=200)])
    sender.provider = provider
    sender.send_pacing = False
    sender.clock = clock
    return sender


def make_handler(world, provider):
    from mercury.agents.handler import Handler

    handler = Handler(brain=ReplyBrain(), state=world.state, config=world.config,
                      env=SimpleNamespace(instantly_api_key=""))
    handler.mailboxes = MailboxPool([Mailbox(email=MAILBOX, provider=provider, daily_cap=200)])
    handler.provider = provider
    return handler


# ── Statistics against known count fixtures ──

# Newcombe (1998), Statistics in Medicine 17:873-890, Table II, method 10.
NEWCOMBE = [
    ((56, 70, 48, 80), (0.0524, 0.3339)),
    ((9, 10, 3, 10), (0.1705, 0.8090)),
    ((6, 7, 2, 7), (0.0582, 0.8062)),
    ((5, 56, 0, 29), (-0.0381, 0.1926)),
    ((0, 10, 0, 20), (-0.1611, 0.2775)),
    ((0, 10, 0, 10), (-0.2775, 0.2775)),
    ((10, 10, 0, 20), (0.6791, 1.0)),
    ((10, 10, 0, 10), (0.6075, 1.0)),
]


@pytest.mark.parametrize("counts,expected", NEWCOMBE)
def test_difference_interval_matches_newcombe_table(counts, expected):
    d, low, high = newcombe_difference(*counts)
    assert d == pytest.approx(counts[0] / counts[1] - counts[2] / counts[3])
    assert (round(low, 4), round(high, 4)) == expected


@pytest.mark.parametrize("x,n,expected", [
    (81, 263, (0.2553, 0.3662)), (15, 148, (0.0624, 0.1605)),
    (0, 20, (0.0, 0.1611)), (1, 29, (0.0061, 0.1718)),
])
def test_wilson_interval_matches_published_values(x, n, expected):
    low, high = wilson(x, n)
    assert (round(low, 4), round(high, 4)) == expected


def test_intervals_need_a_sample():
    assert wilson(0, 0) is None
    assert newcombe_difference(1, 10, 0, 0) is None


# ── Assignment ──

def test_assignment_is_a_pure_function_of_its_ids():
    first = ex.assignment_bucket("exp1", 1, "p1")
    assert first == ex.assignment_bucket("exp1", 1, "p1")
    assert 0 <= first < 1
    assert first != ex.assignment_bucket("exp2", 1, "p1")
    assert first != ex.assignment_bucket("exp1", 2, "p1")
    assert ex.arm_for_bucket(0.49, 50) == "A" and ex.arm_for_bucket(0.5, 50) == "B"


@pytest.mark.parametrize("allocation", [50, 30, 80])
def test_allocation_approaches_the_ratio_without_order_or_score_bias(allocation):
    # Prospects arrive in creation order with rising scores: a split that
    # depended on order or score would show up between the halves.
    ids = [f"prospect-{i:05d}" for i in range(6000)]
    scores = {pid: i for i, pid in enumerate(ids)}
    arm = {pid: ex.arm_for_bucket(ex.assignment_bucket("exp-alloc", 1, pid), allocation)
           for pid in ids}
    share = sum(v == "A" for v in arm.values()) / len(ids)
    assert abs(share - allocation / 100) < 0.025
    early, late = ids[:3000], ids[3000:]
    assert abs(sum(arm[p] == "A" for p in early) / 3000
               - sum(arm[p] == "A" for p in late) / 3000) < 0.04
    mean = lambda key: (sum(scores[p] for p in ids if arm[p] == key)  # noqa: E731
                        / max(1, sum(arm[p] == key for p in ids)))
    assert abs(mean("A") - mean("B")) / len(ids) < 0.03


@pytest.mark.asyncio
async def test_enrollment_is_stored_before_generation_and_stable_across_restarts(world):
    exp = await started(world)
    pids = await add_prospects(world.state, 6)
    seen = {}

    async def check(task):
        # The assignment exists before the first model call for the batch.
        if task == "write_sequence" and not seen:
            seen.update({r["prospect_id"]: r["arm_key"] for r in rows(
                world.state, "SELECT * FROM experiment_assignments")})
            raise RuntimeError("model unavailable")

    writer = make_writer(world, WriterBrain(on_call=check))
    with pytest.raises(RuntimeError):
        await writer.run()
    assert set(seen) == set(pids)
    # A restart: a new StateManager, a new Writer, the same database.
    world.state = StateManager(world.state.db_path)
    await make_writer(world).run()
    after = rows(world.state, "SELECT * FROM experiment_assignments")
    assert len(after) == len(pids)
    assert {r["prospect_id"]: r["arm_key"] for r in after} == seen
    for r in after:
        assert r["arm_key"] == ex.arm_for_bucket(ex.assignment_bucket(exp["id"], 1, r["prospect_id"]), 50)
    with pytest.raises(sqlite3.IntegrityError):
        conn = sqlite3.connect(world.state.db_path)
        try:
            conn.execute("UPDATE experiment_assignments SET arm_key = 'B'")
        finally:
            conn.close()


@pytest.mark.asyncio
async def test_concurrent_enrollment_assigns_each_prospect_once(world):
    await started(world)
    pids = await add_prospects(world.state, 20)
    prospects = [await world.state.get_prospect(p) for p in pids]
    results = await asyncio.gather(*[ex.enroll(world.state, world.config, prospects)
                                     for _ in range(6)])
    assigned = rows(world.state, "SELECT * FROM experiment_assignments")
    assert len(assigned) == 20
    for result in results:
        assert {k: v["arm_key"] for k, v in result.items()} == \
            {r["prospect_id"]: r["arm_key"] for r in assigned}


@pytest.mark.asyncio
async def test_a_prospect_enters_one_experiment_and_cohorts_filter(world):
    first = await started(world, name="First", cohort={"industries": ["dental"]})
    second = await started(world, name="Second")
    dental = await add_prospects(world.state, 4, prefix="d", industry="Dental")
    vets = await add_prospects(world.state, 3, prefix="v", industry="veterinary")
    everyone = [await world.state.get_prospect(p) for p in dental + vets]
    result = await ex.enroll(world.state, world.config, everyone)
    assert {result[p]["experiment_id"] for p in dental} == {first["id"]}
    assert {result[p]["experiment_id"] for p in vets} == {second["id"]}
    again = await ex.enroll(world.state, world.config, everyone)
    assert len(rows(world.state, "SELECT * FROM experiment_assignments")) == 7
    assert {k: v["experiment_id"] for k, v in again.items()} == \
        {k: v["experiment_id"] for k, v in result.items()}


@pytest.mark.asyncio
async def test_paused_enrollment_keeps_assigned_prospects_and_takes_no_new_ones(world):
    exp = await started(world)
    old = await add_prospects(world.state, 3, prefix="o")
    await ex.enroll(world.state, world.config, [await world.state.get_prospect(p) for p in old])
    await world.svc.pause(exp["id"])
    new = await add_prospects(world.state, 3, prefix="n")
    result = await ex.enroll(world.state, world.config,
                             [await world.state.get_prospect(p) for p in old + new])
    assert set(result) == set(old)
    await world.svc.resume(exp["id"])
    assert set(await ex.enroll(world.state, world.config,
                               [await world.state.get_prospect(p) for p in new])) == set(new)


@pytest.mark.asyncio
async def test_enrollment_window_and_cap(world):
    past = (now() - timedelta(days=1)).date().isoformat()
    await started(world, name="Closed", enroll_until=past)
    capped = await started(world, name="Capped", max_enrolled=2)
    pids = await add_prospects(world.state, 5)
    result = await ex.enroll(world.state, world.config,
                             [await world.state.get_prospect(p) for p in pids])
    assert len(result) == 2 and {v["experiment_id"] for v in result.values()} == {capped["id"]}


# ── Definitions, revisions and freezing ──

@pytest.mark.asyncio
async def test_one_variable_at_a_time(world):
    store = PersonaStore(world.state)
    await store.ensure_default(world.config)
    other = await store.save({"name": "Warm voice", "description": "", "avatar_seed": AVATAR_SEEDS[1],
                              "tone": "warm", "instructions": "", "examples": ""})
    with pytest.raises(Invalid, match="same persona"):
        await world.svc.validate(definition(arms=[{"instruction": "x"},
                                                  {"instruction": "y", "persona_id": other}]))
    with pytest.raises(Invalid, match="different instructions"):
        await world.svc.validate(definition(arms=[{"instruction": "x"}, {"instruction": "x"}]))
    with pytest.raises(Invalid, match="different persona"):
        await world.svc.validate(definition(variable="persona",
                                            arms=[{"persona_id": other}, {"persona_id": other}]))
    with pytest.raises(Invalid, match="same instruction"):
        await world.svc.validate(definition(variable="persona", arms=[
            {"instruction": "x"}, {"instruction": "y", "persona_id": other}]))
    ok = await world.svc.validate(definition(variable="persona", arms=[{}, {"persona_id": other}]))
    assert [a["name"] for a in ok["arms"]] == ["Variant A", "Variant B"]


@pytest.mark.asyncio
async def test_start_freezes_the_revision_and_edits_make_a_new_one(world):
    created = await world.svc.create(definition())
    exp_id = created["experiment"]["id"]
    # Drafts edit in place.
    edited = await world.svc.update(exp_id, {"allocation_a": 60})
    assert edited["experiment"]["revision"] == 1 and not edited["new_revision"]
    started_exp = (await world.svc.start(exp_id))["experiment"]
    assert started_exp["frozen"] and all(a["persona_version_id"] == "workspace-v1"
                                         for a in started_exp["arms"])
    conn = sqlite3.connect(world.state.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE experiment_arms SET instruction = 'changed'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE experiment_revisions SET min_per_arm = 1")
    conn.close()

    early = await add_prospects(world.state, 4, prefix="e")
    await ex.enroll(world.state, world.config, [await world.state.get_prospect(p) for p in early])
    renamed = await world.svc.update(exp_id, {"name": "Opening angle, round one"})
    assert renamed["experiment"]["revision"] == 1 and not renamed["new_revision"]
    with pytest.raises(Conflict):
        await world.svc.update(exp_id, {"hypothesis": "stale"}, expected_version=1)
    changed = await world.svc.update(exp_id, {"arms": [
        {"name": "variant A", "instruction": "Open with a question about their week."},
        {"name": "variant B", "instruction": "Open with a short customer story."}]})
    assert changed["new_revision"] and changed["experiment"]["revision"] == 2
    late = await add_prospects(world.state, 4, prefix="l")
    result = await ex.enroll(world.state, world.config,
                             [await world.state.get_prospect(p) for p in early + late])
    assert {result[p]["revision"] for p in early} == {1}
    assert {result[p]["revision"] for p in late} == {2}
    assert (await world.svc.get(exp_id, 1))["results"]["arms"][0]["instruction"] == \
        "Open with a question about their week."
    await world.svc.complete(exp_id, confirm=True)
    with pytest.raises(Conflict):
        await world.svc.update(exp_id, {"min_per_arm": 5})


@pytest.mark.asyncio
async def test_changed_persona_default_does_not_move_an_arm(world):
    store = PersonaStore(world.state)
    await store.ensure_default(world.config)
    warm = await store.save({"name": "Warm voice", "description": "", "avatar_seed": AVATAR_SEEDS[1],
                             "tone": "warm and brief", "instructions": "", "examples": ""})
    exp = await started(world, variable="persona", arms=[{"name": "variant A"},
                                                          {"name": "variant B", "persona_id": warm}])
    # The default persona changes after the start: a new default and a new
    # version of the old one.
    await store.save({"name": "Workspace voice", "description": "", "avatar_seed": AVATAR_SEEDS[0],
                      "tone": "formal", "instructions": "", "examples": "",
                      "expected_revision": 1}, "workspace")
    await store.set_default(warm)
    pids = await add_prospects_in_both_arms(world.state, exp["id"], 4)
    brain = WriterBrain()
    await make_writer(world, brain).run()
    gens = rows(world.state, "SELECT o.experiment_arm_id, g.persona_version_id, g.persona_json "
                "FROM outbox o JOIN email_generations g ON g.id = o.generation_id "
                "WHERE o.experiment_id = ?", (exp["id"],))
    arms = {a["id"]: a for a in exp["arms"]}
    assert gens and len({g["experiment_arm_id"] for g in gens}) == 2
    for g in gens:
        assert g["persona_version_id"] == arms[g["experiment_arm_id"]]["persona_version_id"]
    assert {a["persona_version_id"] for a in exp["arms"]} == {
        "workspace-v1", (await store.versions(warm))[0]["id"]}
    assert len(pids) == len(rows(world.state, "SELECT * FROM experiment_assignments"))


# ── Exposure: the Writer seam and the stamped outbox ──

@pytest.mark.asyncio
async def test_each_prospect_gets_one_arm_for_the_whole_sequence(world):
    exp = await started(world, cohort={"industries": ["dental"]})
    enrolled = await add_prospects_in_both_arms(world.state, exp["id"], 6)
    outside = await add_prospects(world.state, 2, prefix="v", industry="veterinary")
    brain = WriterBrain()
    await make_writer(world, brain).run()
    provider = FakeProvider()
    await make_sender(world, provider, now)._run_native()  # stages steps 2 and 3

    assignments = {r["prospect_id"]: r for r in rows(world.state, "SELECT * FROM experiment_assignments")}
    assert set(assignments) == set(enrolled)
    mail = rows(world.state, "SELECT * FROM outbox WHERE kind = 'sequence'")
    for pid in enrolled:
        steps = [m for m in mail if m["prospect_id"] == pid]
        assert sorted(m["step"] for m in steps) == [1, 2, 3]
        assert {m["experiment_arm_id"] for m in steps} == {assignments[pid]["arm_id"]}
        assert {m["campaign_id"] for m in steps} and len({m["campaign_id"] for m in steps}) == 1
    for pid in outside:
        assert {m["experiment_id"] for m in mail if m["prospect_id"] == pid} == {""}
    # A campaign never mixes arms, and the arm's instruction is in its prompts.
    for campaign_id in {m["campaign_id"] for m in mail if m["experiment_id"]}:
        assert len({m["experiment_arm_id"] for m in mail if m["campaign_id"] == campaign_id}) == 1
    instructions = {a["arm_key"]: a["instruction"] for a in exp["arms"]}
    prompts = [p for task, p in brain.calls if task == "write_sequence"]
    for text in instructions.values():
        assert sum(text in p for p in prompts) == 1
    assert not any(instructions["A"] in p and instructions["B"] in p for p in prompts)
    # Traceable through the API: assignment, emails, generation, persona version.
    trace = await world.svc.exposures(exp["id"])
    assert trace["total"] == len(enrolled)
    for item in trace["assignments"]:
        assert [e["step"] for e in item["emails"]] == [1, 2, 3]
        assert {e["persona_version_id"] for e in item["emails"]} == {"workspace-v1"}


@pytest.mark.asyncio
async def test_a_regenerated_draft_stays_in_its_arm(world):
    await started(world)
    await add_prospects(world.state, 2)
    await make_writer(world).run()
    item = rows(world.state, "SELECT * FROM outbox WHERE experiment_id != '' LIMIT 1")[0]
    writer = make_writer(world)
    prospect = await world.state.get_prospect(item["prospect_id"])
    draft = await writer.regenerate_email(item, prospect, "shorter")
    await PersonaStore(world.state).replace_draft(item["id"], draft)
    after = rows(world.state, "SELECT o.experiment_arm_id, v.arm_id FROM outbox o "
                 "JOIN experiment_generations v ON v.generation_id = o.generation_id "
                 "WHERE o.id = ?", (item["id"],))[0]
    assert after["experiment_arm_id"] == item["experiment_arm_id"] == after["arm_id"]


# ── Controls: pause enrollment vs hold unsent mail ──

@pytest.mark.asyncio
async def test_hold_stops_unsent_mail_without_touching_approvals(world):
    exp = await started(world)
    pids = await add_prospects(world.state, 3)
    plain = await add_prospects(world.state, 1, prefix="x", industry="vet")
    await world.svc.update(exp["id"], {"cohort": {"prospect_ids": pids}})
    await make_writer(world).run()
    provider = FakeProvider()
    sender = make_sender(world, provider, now)
    held = await world.svc.hold(exp["id"], reason="checking copy")
    assert held["experiment"]["hold_mail"] and held["experiment"]["controls"]["can_release"]
    await sender._run_native()
    sent_to = {s["to"] for s in provider.sent}
    plain_email = (await world.state.get_prospect(plain[0])).email
    assert sent_to == {plain_email}
    queued = rows(world.state, "SELECT status, approved_revision FROM outbox WHERE experiment_id = ?",
                  (exp["id"],))
    assert queued and {q["status"] for q in queued} == {"approved"}
    assert all(q["approved_revision"] == 1 for q in queued)
    assert held["experiment"]["queued"]["approved"] == len(pids)  # the openers, when held

    # Pausing enrollment does not stop approved mail; releasing the hold does.
    await world.svc.pause(exp["id"])
    await world.svc.release(exp["id"])
    await sender._run_native()
    assert len(provider.sent) == 1 + len(pids)


@pytest.mark.asyncio
async def test_hold_never_bypasses_exclusions(world):
    exp = await started(world)
    await add_prospects(world.state, 1)
    await make_writer(world).run()
    row = rows(world.state, "SELECT * FROM outbox WHERE experiment_id != '' AND step = 1")[0]
    await world.svc.hold(exp["id"])
    await world.state.add_suppression("email", row["to_email"], source="manual", reason="asked")
    await world.svc.release(exp["id"])
    provider = FakeProvider()
    await make_sender(world, provider, now)._run_native()
    assert provider.sent == []
    assert rows(world.state, "SELECT status FROM outbox WHERE id = ?", (row["id"],))[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_state_machine_and_confirmation(world):
    created = await world.svc.create(definition())
    exp_id = created["experiment"]["id"]
    assert created["results"]["decision"]["code"] == "not_started"
    with pytest.raises(Conflict):
        await world.svc.pause(exp_id)
    with pytest.raises(Conflict):
        await world.svc.hold(exp_id)
    await world.svc.start(exp_id)
    with pytest.raises(Conflict):
        await world.svc.start(exp_id)
    with pytest.raises(Conflict) as error:
        await world.svc.complete(exp_id)
    assert error.value.code == "confirmation_required"
    done = await world.svc.complete(exp_id, confirm=True)
    assert done["experiment"]["status_label"] == "Completed"
    assert not done["experiment"]["controls"]["can_resume"]
    audit = rows(world.state, "SELECT action, outcome FROM audit_log WHERE object_id = ?", (exp_id,))
    assert ("experiments.start", "ok") in {(a["action"], a["outcome"]) for a in audit}


# ── Attribution and outcomes ──

async def run_sequence(world, n=6, **overrides):
    """Start an experiment, write for ``n`` prospects and send their openers
    at t0 (real now). Returns (experiment, provider, sender, t0)."""
    exp = await started(world, **overrides)
    await add_prospects(world.state, n)
    await make_writer(world).run()
    provider = FakeProvider()
    t0 = now()
    sender = make_sender(world, provider, lambda: t0)
    await drain(sender, provider)
    return exp, provider, sender, t0


async def drain(sender, provider):
    """Run the sender until nothing more goes out (it sends at most eight a cycle)."""
    for _ in range(10):
        before = len(provider.sent)
        await sender._run_native()
        if len(provider.sent) == before:
            return


def opener(world, pid):
    return rows(world.state, "SELECT * FROM outbox WHERE prospect_id = ? AND step = 1", (pid,))[0]


def by_arm(world, exp_id):
    out = {"A": [], "B": []}
    for r in rows(world.state, "SELECT * FROM experiment_assignments WHERE experiment_id = ? "
                  "ORDER BY prospect_id", (exp_id,)):
        out[r["arm_key"]].append(r["prospect_id"])
    return out


def reply_to(world, pid, mid, text, when, message_id="", **kw):
    email = rows(world.state, "SELECT email FROM prospects WHERE id = ?", (pid,))[0]["email"]
    return InboundMessage(provider_id=message_id or f"in-{pid}-{when.isoformat()}", from_email=email,
                          subject="Re: booking", body=text, message_id=message_id,
                          in_reply_to=mid, references=mid, date=rfc(when), **kw)


def arm(result, key):
    return next(a for a in result["arms"] if a["key"] == key)


@pytest.mark.asyncio
async def test_follow_ups_and_repeated_replies_count_one_positive_per_prospect(world):
    exp, provider, sender, t0 = await run_sequence(world, n=8)
    groups = by_arm(world, exp["id"])
    target = (groups["A"] or groups["B"])[0]
    key = "A" if groups["A"] else "B"
    # Step 2 goes out three days later.
    sender.clock = lambda: t0 + timedelta(days=3, minutes=5)
    await drain(sender, provider)
    step2 = rows(world.state, "SELECT * FROM outbox WHERE prospect_id = ? AND step = 2", (target,))[0]
    assert step2["status"] == "sent" and step2["experiment_arm_id"]
    provider.inbound = [
        reply_to(world, target, step2["message_id"], "Yes, let's talk next week.",
                 t0 + timedelta(days=4), message_id="<r1@clinic.example.com>"),
        reply_to(world, target, step2["message_id"], "Also, let's talk about pricing.",
                 t0 + timedelta(days=5), message_id="<r2@clinic.example.com>"),
    ]
    await make_handler(world, provider)._run_native()

    result = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=15))
    a = arm(result, key)
    assert a["raw"]["sent"] >= a["contacted"] + 1  # follow-ups are emails, not samples
    assert a["mature"] == a["contacted"] == len(groups[key])
    assert a["positive"]["count"] == 1 and a["any_reply"]["count"] == 1
    assert a["labels"] == {"positive_interested": 1}


@pytest.mark.asyncio
async def test_duplicates_automatic_replies_bounces_and_own_mail(world):
    exp, provider, _sender, t0 = await run_sequence(world, n=10)
    everyone = by_arm(world, exp["id"])
    pids = everyone["A"] + everyone["B"]
    one, two, three, four = pids[:4]
    m1 = opener(world, one)["message_id"]
    second_inbox = FakeProvider()
    dup = reply_to(world, one, m1, "Yes, let's talk.", t0 + timedelta(days=1),
                   message_id="<same@clinic.example.com>")
    second_copy = reply_to(world, one, m1, "Yes, let's talk.", t0 + timedelta(days=1),
                           message_id="<same@clinic.example.com>")
    second_copy.provider_id = "other-id"
    provider.inbound = [
        dup,
        reply_to(world, two, opener(world, two)["message_id"], "I am out of the office until Monday.",
                 t0 + timedelta(days=1), message_id="<ooo@clinic.example.com>",
                 headers={"auto-submitted": "auto-replied"}),
        InboundMessage(provider_id="b-1", from_email="mailer-daemon@example.net",
                       subject="Delivery Status Notification (Failure)",
                       body="Status: 5.1.1 user unknown", is_bounce=True,
                       in_reply_to=opener(world, three)["message_id"], date=rfc(t0 + timedelta(hours=1))),
        InboundMessage(provider_id="own-1", from_email=MAILBOX, subject="Re: booking",
                       body="Forwarding this to myself.", message_id="<own@example.com>",
                       in_reply_to=opener(world, four)["message_id"], date=rfc(t0 + timedelta(days=1))),
    ]
    second_inbox.inbound = [second_copy]
    handler = make_handler(world, provider)
    handler.mailboxes = MailboxPool([Mailbox(email=MAILBOX, provider=provider, daily_cap=200),
                                     Mailbox(email="lee@example.net", provider=second_inbox, daily_cap=200)])
    await handler._run_native()

    result = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=20))
    totals = {k: sum(a[k]["count"] for a in result["arms"])
              for k in ("positive", "any_reply", "bounce")}
    assert totals == {"positive": 1, "any_reply": 1, "bounce": 1}
    assert result["data_quality"]["duplicates_excluded"] == 1
    assert result["data_quality"]["automatic_excluded"] == 1
    assert result["data_quality"]["own_mail_excluded"] == 1


@pytest.mark.asyncio
async def test_failed_sends_are_not_contacted(world):
    exp = await started(world)
    pids = await add_prospects(world.state, 4)
    await make_writer(world).run()
    provider = FakeProvider()
    provider.fail_next = True  # a permanent failure for the first send
    original = provider.send_email

    async def fail_first(*a, **k):
        if provider.fail_next:
            provider.fail_next = False
            from mercury.integrations.mail_provider import SendResult
            return SendResult(ok=False, error="550 mailbox does not exist")
        return await original(*a, **k)

    provider.send_email = fail_first
    t0 = now()
    await make_sender(world, provider, lambda: t0)._run_native()
    result = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=30))
    contacted = sum(a["contacted"] for a in result["arms"])
    awaiting = sum(a["awaiting_first_touch"] for a in result["arms"])
    assert contacted == len(pids) - 1 and awaiting == 1


@pytest.mark.asyncio
async def test_observation_windows_with_a_fake_clock(world):
    exp, provider, _sender, t0 = await run_sequence(world, n=6, response_window_days=10)
    pids = by_arm(world, exp["id"])
    early, late = (pids["A"] + pids["B"])[:2]
    provider.inbound = [
        reply_to(world, early, opener(world, early)["message_id"], "Sure, let's talk.",
                 t0 + timedelta(days=2), message_id="<early@clinic.example.com>"),
        reply_to(world, late, opener(world, late)["message_id"], "Sorry for the delay, let's talk.",
                 t0 + timedelta(days=12), message_id="<late@clinic.example.com>"),
    ]
    await make_handler(world, provider)._run_native()

    during = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=5))
    assert sum(a["mature"] for a in during["arms"]) == 0
    assert sum(a["pending"] for a in during["arms"]) == 6
    assert all(a["primary"]["rate"] is None for a in during["arms"])

    after = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=11))
    assert sum(a["mature"] for a in after["arms"]) == 6
    assert sum(a["positive"]["count"] for a in after["arms"]) == 1
    assert sum(a["late_replies"] for a in after["arms"]) == 1
    assert sum(a["raw"]["positive"] for a in after["arms"]) == 2  # late stays visible


@pytest.mark.asyncio
async def test_replies_follow_the_sent_thread_not_the_current_campaign(world):
    # Before the experiment, two prospects got mail from an old campaign.
    old = await add_prospects(world.state, 2, prefix="h", status="contacted")
    t_old = now() - timedelta(days=60)
    old_mid = {}
    for i, pid in enumerate(old):
        p = await world.state.get_prospect(pid)
        item = await world.state.add_outbox_item(prospect_id=pid, to_email=p.email, subject="hello",
                                                 body="Old campaign opener.", send_at=t_old.isoformat(),
                                                 status="approved", campaign_id="old-campaign", step=1)
        old_mid[pid] = f"<old{i}@example.com>"
        await world.state.update_outbox_item(item, status="sent", sent_at=t_old.isoformat(),
                                             message_id=old_mid[pid], mailbox=MAILBOX)
    conn = sqlite3.connect(world.state.db_path)
    conn.execute("UPDATE prospects SET status = 'new'")  # back in the pool
    conn.commit()
    conn.close()
    exp, provider, _sender, t0 = await run_sequence(world, n=0, cohort={"prospect_ids": old})
    assert set(by_arm(world, exp["id"])["A"] + by_arm(world, exp["id"])["B"]) == set(old)
    first, second = old
    provider.inbound = [
        # An answer to the OLD campaign's email: not the experiment's.
        reply_to(world, first, old_mid[first], "Yes, let's talk about the old offer.",
                 t0 + timedelta(days=1), message_id="<a@clinic.example.com>"),
        # No thread headers: the latest email sent to them from this inbox.
        InboundMessage(provider_id="nohdr", from_email=(await world.state.get_prospect(second)).email,
                       subject="booking", body="Saw your note, let's talk.",
                       message_id="<b@clinic.example.com>", date=rfc(t0 + timedelta(days=1))),
    ]
    await make_handler(world, provider)._run_native()
    result = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=30))
    assert sum(a["positive"]["count"] for a in result["arms"]) == 1
    arm_of_second = rows(world.state, "SELECT arm_key FROM experiment_assignments WHERE prospect_id = ?",
                         (second,))[0]["arm_key"]
    assert arm(result, arm_of_second)["positive"]["count"] == 1
    assert result["data_quality"]["unattributed_messages"] == 1


@pytest.mark.asyncio
async def test_a_reply_to_mercurys_reply_counts_for_the_original_arm(world):
    exp, provider, sender, t0 = await run_sequence(world, n=4)
    pid = (by_arm(world, exp["id"])["A"] + by_arm(world, exp["id"])["B"])[0]
    provider.inbound = [reply_to(world, pid, opener(world, pid)["message_id"], "How much is it?",
                                 t0 + timedelta(days=1), message_id="<q@clinic.example.com>")]
    handler = make_handler(world, provider)
    await handler._run_native()
    answer = rows(world.state, "SELECT * FROM outbox WHERE kind = 'reply' AND prospect_id = ?", (pid,))[0]
    await world.state.approve_outbox(answer["id"])
    sender.clock = lambda: t0 + timedelta(days=1, hours=2)
    await sender._run_native()
    answer = rows(world.state, "SELECT * FROM outbox WHERE id = ?", (answer["id"],))[0]
    assert answer["status"] == "sent" and answer["experiment_id"] == ""
    provider.inbound = [reply_to(world, pid, answer["message_id"], "Great, let's talk Tuesday.",
                                 t0 + timedelta(days=2), message_id="<yes@clinic.example.com>")]
    await handler._run_native()
    result = await ex.results(world.state, world.config, exp["id"], now=t0 + timedelta(days=20))
    key = rows(world.state, "SELECT arm_key FROM experiment_assignments WHERE prospect_id = ?", (pid,))[0]["arm_key"]
    assert arm(result, key)["positive"]["count"] == 1
    assert arm(result, key)["labels"] == {"neutral_question": 1, "positive_interested": 1}


@pytest.mark.asyncio
async def test_outcome_labels_confidence_threshold_and_manual_override(world):
    exp, provider, _sender, t0 = await run_sequence(world, n=4)
    pids = by_arm(world, exp["id"])
    pid = (pids["A"] + pids["B"])[0]
    key = "A" if pid in pids["A"] else "B"
    provider.inbound = [reply_to(world, pid, opener(world, pid)["message_id"], "Maybe in spring.",
                                 t0 + timedelta(days=1), message_id="<m@clinic.example.com>")]
    await make_handler(world, provider)._run_native()
    inbound_id = rows(world.state, "SELECT id FROM inbound_messages WHERE intent != ''")[0]["id"]
    later = t0 + timedelta(days=20)

    # objection maps to not_now at 0.5: not positive.
    assert arm(await ex.results(world.state, world.config, exp["id"], now=later), key)["labels"] == {"not_now": 1}
    # The classifier says soft positive with low confidence: uncertain, not positive.
    brain = WriterBrain(outcome={"label": "positive_soft", "confidence": 0.55})
    assert await ex.classify_pending(world.state, brain, world.config) == 1
    assert await ex.classify_pending(world.state, brain, world.config) == 0  # never twice
    res = arm(await ex.results(world.state, world.config, exp["id"], now=later), key)
    assert res["positive"]["count"] == 0 and res["uncertain"] == 1
    # A person settles it, and the classifier never overrides them.
    await world.svc.label(inbound_id, "positive_interested")
    await ex.set_outcome(world.state, inbound_id, "not_interested", confidence=0.9, source="classifier")
    res = arm(await ex.results(world.state, world.config, exp["id"], now=later), key)
    assert res["positive"]["count"] == 1 and res["uncertain"] == 0
    with pytest.raises(Invalid):
        await world.svc.label(inbound_id, "great")


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_the_intent_mapping_once(world):
    exp, provider, _sender, t0 = await run_sequence(world, n=2)
    pid = (by_arm(world, exp["id"])["A"] + by_arm(world, exp["id"])["B"])[0]
    provider.inbound = [reply_to(world, pid, opener(world, pid)["message_id"], "Yes, let's talk.",
                                 t0 + timedelta(days=1), message_id="<x@clinic.example.com>")]
    await make_handler(world, provider)._run_native()
    brain = WriterBrain(outcome="not json")
    assert await ex.classify_pending(world.state, brain, world.config) == 1
    stored = rows(world.state, "SELECT * FROM experiment_outcomes")[0]
    assert (stored["label"], stored["source"]) == ("positive_interested", "intent")
    assert await ex.classify_pending(world.state, brain, world.config) == 0
    off = config(classify_outcomes=False)
    assert await ex.classify_pending(world.state, brain, off) == 0


def test_intent_mapping_covers_every_handler_intent():
    from mercury.agents.handler import INTENT_LABELS

    assert set(INTENT_LABELS) <= set(ex.INTENT_OUTCOMES)
    assert all(label in ex.OUTCOME_LABELS for label, _ in ex.INTENT_OUTCOMES.values())
    assert ex.mapped_outcome({"kind": "bounce"})[0] == "bounce"
    assert ex.mapped_outcome({"kind": "automatic", "auto_kind": "out_of_office"})[0] == "ooo"
    assert ex.mapped_outcome({"kind": "message", "intent": "interested"}) == \
        ("positive_interested", 0.8, "intent")


# ── Decisions from known counts ──

async def seeded(world, counts, *, min_per_arm=3, days_since=30, window=14, min_days=0,
                 replies=None):
    """An experiment with assignments, sent openers and positive replies laid
    down directly. counts: {"A": (contacted, positives), "B": (...)}."""
    exp = await started(world, min_per_arm=min_per_arm, response_window_days=window,
                        min_duration_days=min_days)
    t0 = now() - timedelta(days=days_since)
    arms = {a["arm_key"]: a for a in exp["arms"]}
    store = PersonaStore(world.state)
    n = 0
    for key, (contacted, positives) in counts.items():
        for i in range(contacted):
            n += 1
            pid = await world.state.add_prospect(Prospect(
                first_name="Kim", last_name=f"Example{n}", email=f"kim{n}@example.org",
                email_status="verified", status="contacted"))
            conn = sqlite3.connect(world.state.db_path)
            conn.execute("INSERT INTO experiment_assignments VALUES (?, ?, ?, ?, ?, 0.1, ?)",
                         (exp["id"], pid, exp["revision_id"], arms[key]["id"], key, t0.isoformat()))
            conn.commit()
            conn.close()
            profile = await store.resolve(world.config)
            marker = {"experiment_id": exp["id"], "revision_id": exp["revision_id"],
                      "arm_id": arms[key]["id"], "arm_key": key, "instruction": ""}
            generation = await store.record(profile | {"experiment": marker}, world.config,
                                            "prompt", {}, "personal_email")
            item = await world.state.add_outbox_item(
                prospect_id=pid, to_email=f"kim{n}@example.org", subject="hello", body="Opener.",
                send_at=t0.isoformat(), status="approved", campaign_id=f"c-{key}", step=1,
                generation_id=generation)
            await world.state.update_outbox_item(item, status="sent", sent_at=t0.isoformat(),
                                                 message_id=f"<s{n}@example.com>", mailbox=MAILBOX)
            reply = (replies or {}).get(key, positives)
            if i < reply:
                row, _ = await world.state.record_inbound(
                    provider="fake", mailbox=MAILBOX, external_id=f"r{n}",
                    rfc_message_id=f"<r{n}@example.org>", in_reply_to=f"<s{n}@example.com>",
                    from_email=f"kim{n}@example.org", body="Reply.",
                    date_header=rfc(t0 + timedelta(days=1)))
                await world.state.link_inbound(row["id"], prospect_id=pid,
                                               intent="interested" if i < positives else "question")
                await world.state.finish_inbound(row["id"], "processed")
    return exp


@pytest.mark.asyncio
async def test_never_a_winner_on_low_samples(world):
    exp = await seeded(world, {"A": (2, 0), "B": (2, 2)}, min_per_arm=3)
    result = await ex.results(world.state, world.config, exp["id"])
    assert result["decision"]["code"] == "insufficient_data"
    assert result["decision"]["label"] == "Not enough data yet"
    assert not result["decision"]["recommends_winner"]
    assert result["decision"]["line"].startswith("Insufficient data: 2 of 3 mature per arm")
    assert result["decision"]["automatic_changes"] is False
    # The arms are untouched: nothing was paused or disabled.
    assert rows(world.state, "SELECT status FROM experiments")[0]["status"] == "running"


@pytest.mark.asyncio
async def test_known_counts_give_the_published_interval_and_a_direction(world):
    exp = await seeded(world, {"A": (80, 48), "B": (70, 56)}, min_per_arm=50)
    result = await ex.results(world.state, world.config, exp["id"])
    comparison = result["comparison"]
    assert comparison["difference"] == pytest.approx(0.2)
    assert (round(comparison["interval"]["low"], 4), round(comparison["interval"]["high"], 4)) == \
        (0.0524, 0.3339)
    assert result["decision"]["code"] == "b_ahead" and result["decision"]["recommends_winner"]
    b = arm(result, "B")
    assert (b["mature"], b["positive"]["count"], b["positive"]["rate"]) == (70, 56, 0.8)
    assert b["primary"]["interval"]["low"] < 0.8 < b["primary"]["interval"]["high"]


@pytest.mark.asyncio
async def test_no_clear_difference(world):
    exp = await seeded(world, {"A": (10, 0), "B": (10, 0)}, min_per_arm=10, replies={"A": 3, "B": 3})
    result = await ex.results(world.state, world.config, exp["id"])
    assert result["decision"]["code"] == "no_clear_difference"
    assert result["comparison"]["difference"] == 0
    assert arm(result, "A")["any_reply"] == {"count": 3, "rate": 0.3}


@pytest.mark.asyncio
async def test_minimum_duration_holds_the_decision(world):
    # Plenty of mature contacts, but the experiment started today and must
    # run a week before any comparison.
    exp = await seeded(world, {"A": (10, 0), "B": (10, 9)}, min_per_arm=10, min_days=7)
    decision = (await ex.results(world.state, world.config, exp["id"]))["decision"]
    assert decision["code"] == "insufficient_data" and not decision["duration_met"]
    assert decision["reasons"] == ["the experiment runs at least 7 days"]
    later = now() + timedelta(days=8)
    assert (await ex.results(world.state, world.config, exp["id"], now=later))["decision"]["code"] == "b_ahead"


@pytest.mark.asyncio
async def test_low_reply_rate_points_to_deliverability_first(world):
    world.config = config(health_min_mature=20, low_reply_rate=0.05)
    exp = await seeded(world, {"A": (30, 0), "B": (30, 1)}, min_per_arm=30)
    result = await ex.results(world.state, world.config, exp["id"])
    assert [w["code"] for w in result["health"]["warnings"]] == ["low_reply_rate"]
    assert result["health"]["warnings"][0]["action"]["tab"] == "mailboxes"
    assert result["decision"]["code"] == "check_deliverability"
    assert not result["decision"]["recommends_winner"]


@pytest.mark.asyncio
async def test_earliest_decision_date(world):
    exp = await seeded(world, {"A": (4, 0), "B": (4, 0)}, min_per_arm=4, days_since=3, window=14)
    result = await ex.results(world.state, world.config, exp["id"])
    decision = result["decision"]
    assert decision["code"] == "insufficient_data" and not decision["earliest_estimated"]
    sent = rows(world.state, "SELECT MAX(sent_at) AS t FROM outbox")[0]["t"]
    expected = (ex.parse_ts(sent) + timedelta(days=14)).date().isoformat()
    assert decision["earliest_decision_at"][:10] == expected
    assert decision["line"].endswith(f"earliest decision {expected}")


# ── Migration ──

def test_migration_adds_experiments_and_leaves_old_mail_unassigned():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "old.db")
        asyncio.run(StateManager(db).init_db())
        conn = sqlite3.connect(db)
        drop_experiment_schema(conn)
        conn.execute("INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, step) "
                     "VALUES ('legacy1', 'p1', 'jo@example.org', 's', 'b', 'sent', 1)")
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
        conn.commit()
        conn.close()

        asyncio.run(StateManager(db).init_db())
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 26
        legacy = dict(conn.execute("SELECT * FROM outbox WHERE id = 'legacy1'").fetchone())
        assert (legacy["experiment_id"], legacy["experiment_revision_id"],
                legacy["experiment_arm_id"]) == ("", "", "")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"experiments", "experiment_revisions", "experiment_arms",
                "experiment_assignments", "experiment_outcomes"} <= tables
        conn.close()
        asyncio.run(StateManager(db).init_db())  # idempotent on the next start


@pytest.mark.asyncio
async def test_no_decision_date_once_enrollment_ends_short(world):
    exp = await seeded(world, {"A": (4, 0), "B": (4, 0)}, min_per_arm=10, days_since=3)
    estimated = (await ex.results(world.state, world.config, exp["id"]))["decision"]
    assert estimated["earliest_estimated"] and estimated["earliest_decision_at"]
    await world.svc.complete(exp["id"], confirm=True)
    decision = (await ex.results(world.state, world.config, exp["id"]))["decision"]
    assert decision["earliest_decision_at"] is None
    assert decision["reasons"][-1] == "enrollment has ended with fewer than 10 in arm A and B"
