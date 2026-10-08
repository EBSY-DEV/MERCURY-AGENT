"""An experiment arm keeps everything else the Writer applies (#6 on #57, #59, #60).

The experiment split happens after the Writer has grouped prospects by offer
(and by mailbox voice). Each arm's share must still write from its offer's
brief, with the pain chosen for that offer, within the step word limits, and
with the arm's instruction (and persona, if it has one) ahead of the mailbox
voice. Synthetic fixtures only: offer_a / offer_b / offer_c, PAIN_TEST_*,
MARKER_* strings and fictional businesses on example.com.
"""

import json

from mercury import experiments as ex
from mercury.agents.sender import Sender
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.config import MailboxConfig
from mercury.control.context import OperatorContext
from mercury.control.experiments import ExperimentService
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.personas import PersonaStore
from mercury.voices import MailboxVoices
from tests.test_experiments import definition
from tests.test_offers import (  # noqa: F401  (client is a fixture)
    client,
    fake_brain,
    generation_prompt,
    run,
    seed,
    unrelated_to_a,
)
from tests.test_outbox_native import Env, FakeProvider
from tests.test_pains_writer import library

ARM_A = "MARKER_ARM_A open with a question about their week"
ARM_B = "MARKER_ARM_B open with an observation from their site"


async def more_offer_a_prospects(state, experiment_id, arm_counts=(2, 2)):
    """New offer_a prospects until each arm has at least the wanted number,
    so no test depends on how a random id hashes."""
    wanted = dict(zip("AB", arm_counts))
    seen = {"A": 0, "B": 0}
    n = 0
    while any(seen[k] < wanted[k] for k in seen):
        cid = await state.add_company(Company(
            name=f"Example Bakery Extra {n}", domain=f"extra{n}.example.com",
            location="Alphaville, Exampleland", industry="segment_a"))
        await state.add_observation("TEST_SIGNAL_A", company_id=cid, value_num=1, collector="test")
        pid = await state.add_prospect(Prospect(
            first_name=f"Pat{n}", last_name="Extra", title="Owner", company=f"Example Bakery Extra {n}",
            company_id=cid, industry="segment_a", email=f"owner@extra{n}.example.com",
            email_status="verified", email_verified=True))
        seen[ex.arm_for_bucket(ex.assignment_bucket(experiment_id, 1, pid), 50)] += 1
        n += 1


async def start_experiment(state, config, **overrides):
    svc = await ExperimentService(OperatorContext.local("cli"), state, config).ready()
    created = await svc.create(definition(**overrides))
    return (await svc.start(created["experiment"]["id"]))["experiment"]


def arm_of(snapshot_json):
    return (json.loads(snapshot_json).get("experiment") or {}).get("arm_key")


async def offer_routes(state):
    async with state._connect() as db:
        cursor = await db.execute("SELECT prospect_id, offer_key FROM offer_routes")
        return {row[0]: row[1] for row in await cursor.fetchall()}


async def generation_row(state, generation_id):
    async with state._connect() as db:
        cursor = await db.execute(
            "SELECT persona_json, prompt FROM email_generations WHERE id = ?", (generation_id,))
        return await cursor.fetchone()


def test_an_arm_keeps_the_offer_brief_pain_block_and_word_limits(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    exp = run(start_experiment(state, client.config, arms=[
        {"name": "variant A", "instruction": ARM_A}, {"name": "variant B", "instruction": ARM_B}]))
    run(more_offer_a_prospects(state, exp["id"]))
    instructions = {a["arm_key"]: a["instruction"] for a in exp["arms"]}

    writer = Writer(Brain(state), state, client.config)
    run(writer.run())

    campaigns = run(state.get_campaigns_by_status("draft"))
    assert all(c.sequence for c in campaigns)
    routes = run(offer_routes(state))
    # Every campaign, split by arm or not, carries exactly its prospects' offer.
    for campaign in campaigns:
        assert {routes[p] for p in campaign.prospect_ids} == {campaign.offer_key}
    arm_campaigns = [c for c in campaigns if c.offer_key == "offer_a" and "variant" in c.name]
    assert {("variant A" in c.name) for c in arm_campaigns} == {True, False}
    # The offer_b and offer_c prospects were enrolled too and kept their offers.
    assert {c.offer_key for c in campaigns} == {"offer_a", "offer_b", "offer_c"}
    assert ids["b"] in [p for c in campaigns if c.offer_key == "offer_b" for p in c.prospect_ids]

    for campaign in arm_campaigns:
        arm_key = "A" if "variant A" in campaign.name else "B"
        other = "B" if arm_key == "A" else "A"
        sequence = generation_prompt(state, campaign.sequence[0].generation_id)
        # The arm's instruction, once, and never the other arm's.
        assert instructions[arm_key] in sequence and instructions[other] not in sequence
        # #57: the offer_a brief, and nothing of another offer.
        assert "OFFER BRIEF" in sequence and "MARKER_A_SUMMARY" in sequence
        assert unrelated_to_a(sequence) == [], unrelated_to_a(sequence)
        # #59: the confirmed pain of offer_a, and the rejected one only as never-use.
        assert "MARKER_PAIN_A" in sequence and "MARKER_PAIN_B" not in sequence
        assert "NEVER raise these" in sequence
        # #60: the step limits.
        assert "email 1 at most 90 words, email 2 at most 80 words, email 3 at most 50 words" in sequence
        snapshot = run(generation_row(state, campaign.sequence[0].generation_id))
        assert arm_of(snapshot[0]) == arm_key

        # Email 1 is drafted per prospect, inside the arm.
        rows = {r["prospect_id"]: r for r in run(state.get_outbox()) if r["campaign_id"] == campaign.id
                and r["step"] == 1}
        assert set(rows) == set(campaign.prospect_ids)
        for row in rows.values():
            assert row["offer_key"] == "offer_a" and row["pain_code"] == "PAIN_TEST_A"
            assert row["word_limit"] == 90 and row["experiment_id"] == exp["id"]
            personal = generation_prompt(state, row["generation_id"])
            assert instructions[arm_key] in personal and instructions[other] not in personal
            assert "OFFER BRIEF" in personal and "MARKER_A_SUMMARY" in personal
            assert "MARKER_PAIN_A" in personal and unrelated_to_a(personal) == []
            assert "at most 90 words" in personal

    # Follow-ups come from the arm's sequence and stay in its persona.
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    for campaign in arm_campaigns:
        run(sender._stage_campaign_native(campaign))
    arm_ids = {a["id"]: a["arm_key"] for a in exp["arms"]}
    for campaign in arm_campaigns:
        arm_key = "A" if "variant A" in campaign.name else "B"
        for row in run(state.get_outbox()):
            if row["campaign_id"] != campaign.id or row["step"] == 1:
                continue
            assert arm_ids[row["experiment_arm_id"]] == arm_key
            assert row["offer_key"] == "offer_a" and row["word_limit"] == {2: 80, 3: 50}[row["step"]]
            earlier = run(writer._earlier_emails(row))
            voice = run(writer._voice_for(row, earlier))
            assert voice["experiment"]["arm_key"] == arm_key
            prospect = run(state.get_prospect(row["prospect_id"]))
            draft = run(writer.regenerate_email(row, prospect, "shorter"))
            rewrite = generation_prompt(state, draft["generation_id"])
            assert instructions[arm_key] in rewrite and "OFFER BRIEF" in rewrite
            assert "MARKER_PAIN_A" in rewrite and unrelated_to_a(rewrite) == []
            assert draft["pain_code"] == "PAIN_TEST_A"


def pinned_voices(client, monkeypatch):
    """Two mailboxes with different sign-offs and a voice of their own, so the
    Writer pins a mailbox to every new thread before the experiment splits."""
    fake_brain(monkeypatch)
    client.config.channels.email.mailboxes = [
        MailboxConfig(email="robin@one.example", name="Robin Ruiz", daily_cap=20),
        MailboxConfig(email="hello@two.example", name="Team Two", daily_cap=20)]
    store = PersonaStore(client.state)
    mailbox_voice = run(store.save({"name": "Mailbox voice", "description": "", "tone": "MARKER_MAILBOX_TONE",
                                    "instructions": "", "examples": "", "avatar_seed": "a"}))
    arm_voice = run(store.save({"name": "Arm voice", "description": "", "tone": "MARKER_ARM_TONE",
                                "instructions": "", "examples": "", "avatar_seed": "b"}))
    voices = MailboxVoices(client.state, client.config)
    run(voices.assign("robin@one.example", mailbox_voice, "Robin"))
    run(voices.assign("hello@two.example", mailbox_voice, "Team Two"))
    assert run(voices.plan())["pinned"]
    return arm_voice


def arm_campaigns_and_prompts(client, exp):
    """Each offer_a arm campaign with the prompts of its sequence and of its
    personalized first emails, after one Writer run."""
    state = client.state
    run(Writer(Brain(state), state, client.config).run())
    found = []
    for campaign in run(state.get_campaigns_by_status("draft")):
        if campaign.offer_key != "offer_a" or " · " not in campaign.name or "variant" not in campaign.name:
            continue
        assert campaign.mailbox in ("robin@one.example", "hello@two.example")
        rows = [r for r in run(state.get_outbox()) if r["campaign_id"] == campaign.id and r["step"] == 1]
        assert rows and all(r["mailbox"] == campaign.mailbox for r in rows)
        prompts = [generation_prompt(state, campaign.sequence[0].generation_id)]
        prompts += [generation_prompt(state, r["generation_id"]) for r in rows]
        found.append((campaign, "B" if "variant B" in campaign.name else "A", prompts))
    assert {arm for _c, arm, _p in found} == {"A", "B"}
    return found


def assert_offer_pain_and_sign_off(campaign, prompts):
    signer = "Robin" if campaign.mailbox.startswith("robin") else "Team Two"
    for prompt in prompts:
        # The mailbox's sign-off, #57's brief and #59's pain survive the split.
        assert f"Sign off with this name and no other: {signer}." in prompt
        assert "MARKER_A_SUMMARY" in prompt and "MARKER_PAIN_A" in prompt
        assert unrelated_to_a(prompt) == []


def assert_follow_ups_keep_the_arm(client, campaign, arm_key, tone):
    state = client.state
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    run(sender._stage_campaign_native(campaign))
    writer = Writer(Brain(state), state, client.config)
    signer = "Robin" if campaign.mailbox.startswith("robin") else "Team Two"
    follow_ups = [r for r in run(state.get_outbox()) if r["campaign_id"] == campaign.id and r["step"] > 1]
    assert follow_ups
    for row in follow_ups:
        # #60's resolution (own generation, opener, mailbox voice, default)
        # lands on the arm's snapshot, so a follow-up never leaves its arm.
        voice = run(writer._voice_for(row, run(writer._earlier_emails(row))))
        assert (voice["mailbox"], voice["signer"], voice["tone"]) == (campaign.mailbox, signer, tone)
        assert voice["experiment"]["arm_key"] == arm_key


def test_an_arm_persona_wins_over_the_mailbox_voice_inside_each_offer(client, monkeypatch):
    state = client.state
    run(seed(state))
    run(library(state))
    arm_voice = pinned_voices(client, monkeypatch)
    exp = run(start_experiment(state, client.config, variable="persona", arms=[
        {"name": "variant A"}, {"name": "variant B", "persona_id": arm_voice}]))
    run(more_offer_a_prospects(state, exp["id"], (3, 3)))

    default_tone = client.config.persona.tone
    for campaign, arm_key, prompts in arm_campaigns_and_prompts(client, exp):
        assert_offer_pain_and_sign_off(campaign, prompts)
        # A persona experiment compares fixed personas: the arm's persona (the
        # workspace default for an arm that names none) replaces the mailbox
        # voice, while the sign-off stays the mailbox's.
        tone = "MARKER_ARM_TONE" if arm_key == "B" else default_tone
        for prompt in prompts:
            assert f"Tone: {tone}" in prompt and "MARKER_MAILBOX_TONE" not in prompt
        assert_follow_ups_keep_the_arm(client, campaign, arm_key, tone)


def test_an_instruction_arm_writes_in_its_frozen_persona_not_the_mailbox_voice(client, monkeypatch):
    state = client.state
    run(seed(state))
    run(library(state))
    pinned_voices(client, monkeypatch)
    exp = run(start_experiment(state, client.config, arms=[
        {"name": "variant A", "instruction": ARM_A}, {"name": "variant B", "instruction": ARM_B}]))
    run(more_offer_a_prospects(state, exp["id"], (3, 3)))
    instructions = {"A": ARM_A, "B": ARM_B}
    default_tone = client.config.persona.tone

    for campaign, arm_key, prompts in arm_campaigns_and_prompts(client, exp):
        assert_offer_pain_and_sign_off(campaign, prompts)
        other = "B" if arm_key == "A" else "A"
        for prompt in prompts:
            # Every arm freezes a persona version when the experiment starts
            # (the workspace default when it names none), and that persona, not
            # the mailbox voice, carries the arm's instruction.
            assert f"Tone: {default_tone}" in prompt and "MARKER_MAILBOX_TONE" not in prompt
            assert instructions[arm_key] in prompt and instructions[other] not in prompt
        assert_follow_ups_keep_the_arm(client, campaign, arm_key, default_tone)
