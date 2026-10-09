"""Writer consistency (#60) and the Writer half of #56.

One word limit per step that the prompts state and the code enforces, a draft
over it never staged unflagged, the persona and the brief's step in every
follow-up, a configured offer sentence, per-market language rules, short
business names, no review counts in facts, and greetings from the registry
resolver. Synthetic only: offer_a / offer_b / offer_c, market_a / market_b,
fictional businesses on example.com, MARKER strings to show what reaches a prompt.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mercury.agents.sender import Sender
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.config import MailboxConfig, OfferDefinition
from mercury.draft_rules import count_words
from mercury.gate import pre_send_check
from mercury.greeting import NAMED, NONE, ROUTING, plan_greeting
from mercury.models.prospect import Prospect
from mercury.personas import AVATAR_SEEDS, PersonaStore
from mercury.voices import MailboxVoices
from tests.test_offers import (  # noqa: F401  (client is a fixture)
    OFFERS,
    client,
    fake_brain,
    generation_prompt,
    run,
    seed,
)
from tests.test_outbox_native import Env, FakeProvider

ROOT = Path(__file__).resolve().parent.parent
WORDS = lambda n: " ".join(["word"] * n)  # noqa: E731
LONG = WORDS(120)


def scripted(monkeypatch, personal, sequence=None):
    """A Brain whose personal/follow-up answers come from ``personal(prompt)``."""
    async def think(prompt, session_id=None, agent="", task=""):
        if task == "write_sequence":
            return sequence or [
                {"step": i, "subject": f"note {i}", "body": f"Hello {{{{first_name}}}}, step {i}. Question?",
                 "delay_days": 0 if i == 1 else 3} for i in (1, 2, 3)]
        return personal(prompt)
    model = AsyncMock(side_effect=think)
    monkeypatch.setattr(Brain, "think_json", model)
    return model


def calls(model, task):
    return [c for c in model.await_args_list if c.kwargs.get("task") == task]


def rows(state):
    return run(state.get_outbox())


def step_rows(state, step):
    return [r for r in rows(state) if r["step"] == step]


def stage(client):
    sender = Sender(None, client.state, client.config, Env())
    sender.provider = FakeProvider()
    for campaign in run(client.state.get_campaigns_by_status("draft")):
        run(sender._stage_campaign_native(campaign))
    return sender


def add_inbox(client, ids, tag="a", email=None, **fields):
    """A contact the public-inbox sweep found: the business as the first name."""
    base = run(client.state.get_prospect(ids[tag]))
    data = dict(first_name="Example Bakery Delta", last_name="Team", title="Owner", company=base.company,
                company_id=base.company_id, industry=base.industry, email=email or f"info@{tag}.example.com",
                email_status="verified", email_verified=True, source="public_inbox") | fields
    return run(client.state.add_prospect(Prospect(**data)))


def writer_for(client):
    return Writer(Brain(client.state), client.state, client.config)


# ── One source of truth for the word caps ──

def test_prompt_numbers_are_rendered_from_the_configured_limits(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    client.config.writer.word_limits = {1: 70, 2: 60, 3: 40}
    writer = writer_for(client)

    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["c"]))))
    assert ("email 1 at most 70 words, email 2 at most 60 words, email 3 at most 40 words" in prompt)
    assert "At most 70 words in the body, counting the greeting and the sign-off" in prompt

    item = run(state.add_outbox_item(prospect_id=ids["c"], campaign_id="c-test", step=2,
                                     to_email="owner@c.example.com", subject="s", body="b",
                                     send_at="2026-10-08T12:00:00"))
    two = run(writer.regenerate_email(run(state.get_outbox_item(item)), run(state.get_prospect(ids["c"]))))
    three_item = run(state.add_outbox_item(prospect_id=ids["c"], campaign_id="c-test", step=3,
                                           to_email="owner@c.example.com", subject="s", body="b",
                                           send_at="2026-10-08T12:00:00"))
    three = run(writer.regenerate_email(run(state.get_outbox_item(three_item)), run(state.get_prospect(ids["c"]))))
    assert "At most 60 words in the body" in generation_prompt(state, two["generation_id"])
    assert "At most 40 words in the body" in generation_prompt(state, three["generation_id"])

    run(writer.run())
    sequence = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_c")
    shared = generation_prompt(state, sequence.sequence[0].generation_id)
    shared = shared.lower()
    assert "at most 70 words" in shared and "at most 60 words" in shared and "at most 40 words" in shared
    assert (two["word_limit"], three["word_limit"]) == (60, 40)


def test_defaults_are_90_80_50_and_no_prompt_text_states_another_number(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    prompt, _ = run(writer_for(client).build_personal_prompt(run(state.get_prospect(ids["c"]))))
    assert "email 1 at most 90 words, email 2 at most 80 words, email 3 at most 50 words" in prompt
    for old in ("50-90", "60-110", "30-50", "75-100", "under 75 words", "Under 75", "Under 60", "Under 40"):
        assert old not in prompt, old


def test_static_markdown_states_no_body_length_number():
    """Rules read 'the word limit for this step'; only subject lines carry a word count."""
    for name in ("prompts/writer.md", "skills/email_frameworks.md"):
        for number, line in enumerate((ROOT / name).read_text().splitlines(), 1):
            if re.search(r"\b\d+\s*(?:-|–|to)?\s*\d*\s*words\b", line) and "ubject" not in line \
                    and "2-5 words max" not in line:
                pytest.fail(f"{name}:{number} states a body length: {line.strip()}")


def test_followup_roles_do_not_state_their_own_numbers(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    client.config.writer.word_limits = {2: 33, 3: 22}
    writer = writer_for(client)
    for step, expected in ((2, "at most 33 words"), (3, "at most 22 words")):
        item = run(state.add_outbox_item(prospect_id=ids["c"], campaign_id="c-test", step=step,
                                         to_email="owner@c.example.com", subject="s", body="b",
                                         send_at="2026-10-08T12:00:00"))
        draft = run(writer.regenerate_email(run(state.get_outbox_item(item)), run(state.get_prospect(ids["c"]))))
        prompt = generation_prompt(state, draft["generation_id"])
        assert expected in prompt and "60-110" not in prompt and "30-50" not in prompt


# ── Enforcement before anything is staged ──

def test_an_over_limit_draft_is_rewritten_once_and_then_flagged_never_unflagged(client, monkeypatch):
    state = client.state
    run(seed(state))
    client.config.channels.email.require_approval = False  # autopilot must not approve it
    model = scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": LONG})
    run(writer_for(client).run())

    first_emails = step_rows(state, 1)
    assert len(first_emails) == 3
    for row in first_emails:
        assert row["status"] == "pending_review"
        assert (row["word_count"], row["word_limit"], row["flags"]) == (120, 90, '["over_word_limit"]')
        assert row["flags_accepted_by"] == ""
    # One call, then exactly one retry per prospect, with the explicit instruction.
    personal = calls(model, "personal_email")
    assert len(personal) == 6
    retries = [c.args[0] for c in personal if "PREVIOUS DRAFT IS TOO LONG" in c.args[0]]
    assert len(retries) == 3
    assert all("at most 90 words including greeting and sign-off" in p for p in retries)
    assert all("120 words" in p for p in retries)


def test_a_shorter_retry_is_staged_unflagged_and_its_prompt_is_the_one_recorded(client, monkeypatch):
    state = client.state
    run(seed(state))
    model = scripted(monkeypatch, lambda prompt: {
        "subject": "a note", "body": WORDS(40) if "PREVIOUS DRAFT IS TOO LONG" in prompt else LONG})
    run(writer_for(client).run())
    for row in step_rows(state, 1):
        assert (row["word_count"], row["flags"]) == (40, "")
        assert "PREVIOUS DRAFT IS TOO LONG" in generation_prompt(state, row["generation_id"])
    assert len(calls(model, "personal_email")) == 6


def test_a_retry_that_is_not_shorter_is_ignored(client, monkeypatch):
    state = client.state
    run(seed(state))
    scripted(monkeypatch, lambda prompt: {
        "subject": "a note", "body": WORDS(150) if "PREVIOUS DRAFT IS TOO LONG" in prompt else LONG})
    run(writer_for(client).run())
    assert {r["word_count"] for r in step_rows(state, 1)} == {120}


def test_greeting_and_sign_off_count_toward_the_limit(client, monkeypatch):
    state = client.state
    run(seed(state))
    body = f"Hi PatA,\n\n{WORDS(88)}\n\nSam"
    assert count_words(body) == 91 and count_words(WORDS(88)) == 88
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": body})
    run(writer_for(client).run())
    for row in step_rows(state, 1):
        assert (row["word_count"], row["word_limit"], row["flags"]) == (91, 90, '["over_word_limit"]')

    # The same words without the greeting and sign-off fit.
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": f"Hi PatA,\n\n{WORDS(87)}\n\nSam"})
    item = step_rows(state, 1)[0]
    draft = run(writer_for(client).regenerate_email(item, run(state.get_prospect(item["prospect_id"]))))
    assert draft["flags"] == [] and count_words(draft["body"]) == 90


def test_a_draft_exactly_at_the_limit_is_not_flagged(client, monkeypatch):
    state = client.state
    run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": WORDS(90)})
    run(writer_for(client).run())
    assert all(r["flags"] == "" and r["word_count"] == 90 for r in step_rows(state, 1))


def test_the_sender_flags_a_staged_follow_up_over_its_limit_and_never_auto_approves_it(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    client.config.channels.email.require_approval = False
    sequence = [{"step": 1, "subject": "s1", "body": "Hello {{first_name}}, one. Q?", "delay_days": 0},
                {"step": 2, "subject": "s2", "body": "Hello {{first_name}}, " + WORDS(100), "delay_days": 3},
                {"step": 3, "subject": "s3", "body": "Three. Q?", "delay_days": 4}]
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "Short. Q?"}, sequence)
    run(writer_for(client).run())
    stage(client)
    by_step = {r["step"]: r for r in rows(state) if r["prospect_id"] == ids["a"]}
    assert by_step[2]["flags"] == '["over_word_limit"]' and by_step[2]["word_limit"] == 80
    assert by_step[2]["status"] == "pending_review"
    assert by_step[3]["flags"] == "" and by_step[3]["status"] == "approved"
    assert by_step[3]["word_limit"] == 50 and by_step[3]["word_count"] == count_words(by_step[3]["body"])


# ── Outbox API: counts, flags, explicit approval ──

def flagged_client(client, monkeypatch):
    ids = run(seed(client.state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": LONG})
    run(writer_for(client).run())
    row = next(r for r in client.get("/api/outbox").json()["pending"] if r["prospect_id"] == ids["a"])
    return ids, row


def test_outbox_rows_expose_count_limit_and_flags(client, monkeypatch):
    _ids, row = flagged_client(client, monkeypatch)
    assert row["word_count"] == 120 and row["word_limit"] == 90
    assert row["flags"] == ["over_word_limit"]
    assert row["flag_details"] == [{"code": "over_word_limit", "label": "Over the word limit"}]
    assert row["needs_flag_approval"] is True
    one = client.get(f"/api/outbox/{row['id']}").json()
    assert (one["word_count"], one["word_limit"], one["flags"]) == (120, 90, ["over_word_limit"])


def test_a_clean_row_has_no_flags_and_an_old_row_is_counted_on_read(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "Short. Q?"})
    run(writer_for(client).run())
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com",
                                     subject="s", body="five words in this body",
                                     send_at="2026-10-08T12:00:00"))

    async def forget():
        async with state._connect() as db:
            await db.execute("UPDATE outbox SET word_count = NULL WHERE id = ?", (item,))
            await db.commit()
    run(forget())
    row = client.get(f"/api/outbox/{item}").json()
    assert (row["word_count"], row["word_limit"], row["flags"], row["needs_flag_approval"]) == (5, 0, [], False)


def test_approving_a_flagged_draft_is_an_explicit_choice(client, monkeypatch):
    _ids, row = flagged_client(client, monkeypatch)
    refused = client.post(f"/api/outbox/{row['id']}/approve", json={"revision": row["revision"]})
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "flagged" and body["flags"] == ["over_word_limit"]
    assert (body["word_count"], body["word_limit"]) == (120, 90)
    assert run(client.state.get_outbox_item(row["id"]))["status"] == "pending_review"
    # A string that merely looks truthy is not the choice.
    assert client.post(f"/api/outbox/{row['id']}/approve",
                       json={"revision": row["revision"], "approve_flagged": "true"}).status_code == 409

    ok = client.post(f"/api/outbox/{row['id']}/approve",
                     json={"revision": row["revision"], "approve_flagged": True})
    assert ok.status_code == 200 and ok.json()["flags_accepted"] == ["over_word_limit"]
    stored = run(client.state.get_outbox_item(row["id"]))
    assert stored["status"] == "approved" and stored["flags_accepted_by"] != ""
    assert client.get(f"/api/outbox/{row['id']}").json()["needs_flag_approval"] is False


def test_approve_all_and_batch_leave_flagged_drafts_in_review_unless_named(client, monkeypatch):
    ids, row = flagged_client(client, monkeypatch)
    pending = client.get("/api/outbox").json()["pending"]
    items = [{"id": r["id"], "revision": r["revision"]} for r in pending]
    result = client.post("/api/outbox/approve-all", json={"items": items}).json()
    assert result["approved"] == 0 and result["failed"] == len(items)
    assert {r["code"] for r in result["results"]} == {"flagged"}
    assert all(r["status"] == "pending_review" for r in step_rows(client.state, 1))

    chosen = [dict(items[0], approve_flagged=True)] + items[1:]
    result = client.post("/api/outbox/batch", json={"action": "approve", "items": chosen}).json()
    assert result["succeeded"] == 1 and result["failed"] == len(items) - 1
    assert next(r for r in result["results"] if r["ok"])["id"] == items[0]["id"]
    # Rejecting a flagged draft needs no special choice.
    other = items[1]
    assert client.post(f"/api/outbox/{other['id']}/reject", json={"revision": other["revision"]}).json()["rejected"]


def test_editing_measures_the_draft_again_and_drops_the_acceptance(client, monkeypatch):
    _ids, row = flagged_client(client, monkeypatch)
    item, revision = row["id"], row["revision"]
    client.post(f"/api/outbox/{item}/approve", json={"revision": revision, "approve_flagged": True})

    shorter = client.put(f"/api/outbox/{item}", json={"subject": "a note", "body": WORDS(60), "revision": revision})
    assert shorter.status_code == 200, shorter.text
    edited = shorter.json()
    assert (edited["word_count"], edited["word_limit"], edited["flags"], edited["needs_flag_approval"]) == (60, 90, [], False)
    assert edited["approval_cleared"] is True
    assert run(client.state.get_outbox_item(item))["flags_accepted_by"] == ""

    longer = client.put(f"/api/outbox/{item}", json={"subject": "a note", "body": WORDS(95), "revision": edited["revision"]}).json()
    assert (longer["word_count"], longer["flags"], longer["needs_flag_approval"]) == (95, ["over_word_limit"], True)
    stale = client.post(f"/api/outbox/{item}/approve", json={"revision": longer["revision"]})
    assert stale.status_code == 409 and stale.json()["code"] == "flagged"


def test_editing_uses_the_limit_the_config_sets_now(client, monkeypatch):
    _ids, row = flagged_client(client, monkeypatch)
    client.config.writer.word_limits = {1: 150, 2: 80, 3: 50}
    edited = client.put(f"/api/outbox/{row['id']}", json={"subject": "a note", "body": WORDS(120),
                                                          "revision": row["revision"]}).json()
    assert (edited["word_limit"], edited["flags"]) == (150, [])


def test_regenerating_measures_the_new_draft_and_clears_the_acceptance(client, monkeypatch):
    _ids, row = flagged_client(client, monkeypatch)
    client.post(f"/api/outbox/{row['id']}/approve", json={"revision": row["revision"], "approve_flagged": True})
    revision = run(client.state.get_outbox_item(row["id"]))["revision"]

    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": LONG})
    again = client.post(f"/api/outbox/{row['id']}/regenerate", json={"revision": revision}).json()
    assert again["success"] and again["flags"] == ["over_word_limit"] and again["word_count"] == 120
    assert again["status"] == "pending_review" and again["needs_flag_approval"] is True

    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": WORDS(30)})
    fixed = client.post(f"/api/outbox/{row['id']}/regenerate", json={"revision": again["revision"]}).json()
    assert fixed["flags"] == [] and fixed["word_count"] == 30 and fixed["needs_flag_approval"] is False


# ── The send path: policy cannot approve a flagged draft, the gate and the claim check it ──

def test_policy_approval_and_follow_up_promotion_skip_flagged_drafts(client):
    state = client.state
    ids = run(seed(client.state))
    flagged = run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com",
                                        subject="s", body=WORDS(120), send_at="2026-10-08T12:00:00",
                                        status="approved", campaign_id="c-1", word_limit=90))
    assert run(state.get_outbox_item(flagged))["status"] == "pending_review"
    follow = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com",
                                       subject="s", body=WORDS(100), send_at="2026-10-08T12:00:00",
                                       campaign_id="c-1", word_limit=80))
    run(state.add_outbox_item(prospect_id=ids["b"], step=1, to_email="owner@b.example.com", subject="s",
                              body="fine", send_at="2026-10-08T12:00:00", status="approved", campaign_id="c-2"))
    ok_follow = run(state.add_outbox_item(prospect_id=ids["b"], step=2, to_email="owner@b.example.com",
                                          subject="s", body="fine two", send_at="2026-10-08T12:00:00",
                                          campaign_id="c-2", word_limit=80))
    run(state.approve_outbox(flagged, accept_flags=True, approved_by="tester"))
    assert run(state.approve_ready_followups()) >= 1
    assert run(state.get_outbox_item(follow))["status"] == "pending_review"
    assert run(state.get_outbox_item(ok_follow))["status"] == "approved"


def test_state_refuses_to_approve_a_flagged_draft_without_the_choice(client):
    state = client.state
    ids = run(seed(client.state))
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com",
                                     subject="s", body=WORDS(120), send_at="2026-10-08T12:00:00",
                                     word_limit=90))
    assert run(state.approve_outbox(item, approved_by="tester")) == 0
    assert run(state.approve_outbox(item, approved_by="tester", accept_flags=True)) == 1
    assert run(state.get_outbox_item(item))["flags_accepted_by"] == "tester"


def test_gate_and_claim_refuse_an_unaccepted_flag(client):
    state = client.state
    ids = run(seed(client.state))
    prospect = run(state.get_prospect(ids["a"]))
    assert pre_send_check("owner@a.example.com", "a note", "Short. Q?", prospect=prospect).ok
    blocked = pre_send_check("owner@a.example.com", "a note", "Short. Q?", prospect=prospect,
                             unaccepted_flags=["over_word_limit"])
    assert not blocked.ok and "flagged draft not accepted" in blocked.reasons[0]

    item = run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com",
                                     subject="s", body=WORDS(120), send_at="2026-10-08T12:00:00",
                                     word_limit=90))
    run(state.approve_outbox(item, accept_flags=True, approved_by="tester"))

    async def forget_acceptance():
        async with state._connect() as db:
            await db.execute("UPDATE outbox SET flags_accepted_by = '' WHERE id = ?", (item,))
            await db.commit()
    run(forget_acceptance())
    verdict, _ = run(state.claim_for_send(run(state.get_outbox_item(item)), "robin@one.example"))
    assert verdict == "stale"
    run(state.approve_outbox(item, accept_flags=True, approved_by="tester"))  # already approved: no-op
    async def accept():
        async with state._connect() as db:
            await db.execute("UPDATE outbox SET flags_accepted_by = 'tester' WHERE id = ?", (item,))
            await db.commit()
    run(accept())
    verdict, _ = run(state.claim_for_send(run(state.get_outbox_item(item)), "robin@one.example"))
    assert verdict == "claimed"


def test_the_sender_drain_blocks_a_flagged_row_nobody_accepted(client, monkeypatch):
    state = client.state
    ids = run(seed(client.state))
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com",
                                     subject="a note", body=WORDS(120), send_at="2020-01-01T00:00:00",
                                     word_limit=90))
    # Approved by some path that skipped the choice.
    import aiosqlite

    async def force_approved():
        async with aiosqlite.connect(state.db_path) as db:
            from mercury.state import _register_functions as register
            await register(db)
            await db.execute("UPDATE outbox SET status = 'approved', approved_revision = revision, "
                             "approved_hash = outbox_hash(to_email, subject, body, mailbox, generation_id) "
                             "WHERE id = ?", (item,))
            await db.commit()
    run(force_approved())
    run(sender._drain_due())
    row = run(state.get_outbox_item(item))
    assert row["status"] == "failed" and "flagged draft not accepted by a reviewer" in row["error"]
    assert not sender.provider.sent


# ── Personas and the brief's step in every follow-up ──

def two_voices(client):
    client.config.channels.email.mailboxes = [
        MailboxConfig(email="one@one.example", name="One", daily_cap=20),
        MailboxConfig(email="two@two.example", name="Two", daily_cap=20)]
    voices = MailboxVoices(client.state, client.config)
    store = PersonaStore(client.state)
    markers = {}
    for tag, email in (("ALPHA", "one@one.example"), ("OMEGA", "two@two.example")):
        pid = run(store.save({"name": f"Voice {tag}", "description": "", "avatar_seed": AVATAR_SEEDS[1],
                              "sign_name": tag.title(), "tone": f"TONE_{tag}", "instructions": f"INSTR_{tag}",
                              "examples": f"EXAMPLE_{tag}"}))
        run(voices.assign(email, pid))
        markers[email] = tag
    return markers


def more_prospects(client, ids, count=3):
    """Named contacts at the third company, so its group is split across mailboxes."""
    base = run(client.state.get_prospect(ids["c"]))
    for i in range(count):
        ids[f"x{i}"] = run(client.state.add_prospect(Prospect(
            first_name=f"Dana{i}", last_name="Example", title="Owner", company=base.company,
            company_id=base.company_id, industry=base.industry, email=f"dana{i}@c.example.com",
            email_status="verified", email_verified=True)))
    return ids


def test_every_step_of_every_thread_carries_its_personas_voice(client, monkeypatch):
    state = client.state
    ids = more_prospects(client, run(seed(state)))
    markers = two_voices(client)
    model = scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    run(writer.run())
    stage(client)

    campaigns = run(state.get_campaigns_by_status("active"))
    assert {c.mailbox for c in campaigns} == set(markers)
    by_prospect = {}
    for campaign in campaigns:
        tag = markers[campaign.mailbox]
        other = next(t for t in markers.values() if t != tag)
        # The shared template for steps 1 to 3.
        sequence = generation_prompt(state, campaign.sequence[0].generation_id)
        assert f"INSTR_{tag}" in sequence and f"TONE_{tag}" in sequence and f"INSTR_{other}" not in sequence
        assert "Write all three emails in the WRITING VOICE above" in sequence
        for row in rows(state):
            if row["campaign_id"] != campaign.id:
                continue
            prospect = run(state.get_prospect(row["prospect_id"]))
            by_prospect[row["prospect_id"]] = tag
            draft = run(writer.regenerate_email(row, prospect))
            prompt = generation_prompt(state, draft["generation_id"])
            assert f"INSTR_{tag}" in prompt and f"INSTR_{other}" not in prompt, (row["step"], tag)
            assert f"TONE_{tag}" in prompt and f"EXAMPLE_{tag}" in prompt
            assert f"no other: {tag.title()}." in prompt, "the sign-off belongs to the persona's mailbox"
            if row["step"] > 1:
                assert "Write in the WRITING VOICE above, the same voice as email 1" in prompt
    assert set(by_prospect) == set(ids.values())


def test_a_follow_up_without_a_generation_takes_its_mailbox_voice(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    markers = two_voices(client)
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    for email, tag in markers.items():
        item = run(state.add_outbox_item(prospect_id=ids["c"], step=2, to_email="owner@c.example.com",
                                         subject="s", body="b", send_at="2026-10-08T12:00:00",
                                         mailbox=email, campaign_id=f"c-{tag}"))
        draft = run(writer.regenerate_email(run(state.get_outbox_item(item)), run(state.get_prospect(ids["c"]))))
        prompt = generation_prompt(state, draft["generation_id"])
        other = next(t for t in markers.values() if t != tag)
        assert f"INSTR_{tag}" in prompt and f"INSTR_{other}" not in prompt


def test_a_follow_up_keeps_the_persona_its_thread_started_with(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    markers = two_voices(client)
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    first_email = next(e for e, t in markers.items() if t == "ALPHA")
    opener = run(writer.personas.record(run(MailboxVoices(state, client.config).profile_for(first_email)),
                                        client.config, "opener prompt", {"subject": "s", "body": "b"}, "personal_email"))
    run(state.add_outbox_item(prospect_id=ids["c"], step=1, to_email="owner@c.example.com", subject="s",
                              body="b", send_at="2026-10-08T12:00:00", campaign_id="thread", generation_id=opener))
    # The follow-up row has lost its generation and its mailbox: the opener decides.
    item = run(state.add_outbox_item(prospect_id=ids["c"], step=2, to_email="owner@c.example.com",
                                     subject="s", body="b", send_at="2026-10-08T12:00:00", campaign_id="thread"))
    draft = run(writer.regenerate_email(run(state.get_outbox_item(item)), run(state.get_prospect(ids["c"]))))
    prompt = generation_prompt(state, draft["generation_id"])
    assert "INSTR_ALPHA" in prompt and "INSTR_OMEGA" not in prompt


def test_follow_ups_take_the_briefs_step_and_see_the_earlier_emails(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com", subject="first subject",
                              body="MARKER_EARLIER_ONE we describe the service here.",
                              send_at="2026-10-08T12:00:00", campaign_id="thread-a", offer_key="offer_a"))
    two = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com", subject="s2",
                                    body="MARKER_EARLIER_TWO a follow-up.", send_at="2026-10-08T12:00:00",
                                    campaign_id="thread-a", offer_key="offer_a"))
    three = run(state.add_outbox_item(prospect_id=ids["a"], step=3, to_email="owner@a.example.com", subject="s3",
                                      body="x", send_at="2026-10-08T12:00:00", campaign_id="thread-a",
                                      offer_key="offer_a"))
    prospect = run(state.get_prospect(ids["a"]))

    p2 = generation_prompt(state, run(writer.regenerate_email(run(state.get_outbox_item(two)), prospect))["generation_id"])
    assert "MARKER_A_ANGLE_2" in p2 and "MARKER_A_CTA_2" in p2 and "MARKER_A_CTA_3" not in p2
    assert "Use the angle and call to action the OFFER BRIEF gives for email 2" in p2
    assert "a FOLLOW-UP sent 3 days after" not in p2, "the brief's step replaces the fixed role string"
    assert "MARKER_EARLIER_ONE" in p2 and "MARKER_EARLIER_TWO" not in p2
    assert "do not say again what the product or the offer is" in p2
    assert "Do not repeat or paraphrase any sentence of the earlier" in p2

    p3 = generation_prompt(state, run(writer.regenerate_email(run(state.get_outbox_item(three)), prospect))["generation_id"])
    assert "MARKER_A_CTA_3" in p3 and "MARKER_EARLIER_ONE" in p3 and "MARKER_EARLIER_TWO" in p3

    # offer_c configures no step, so the fixed description still applies there.
    c_item = run(state.add_outbox_item(prospect_id=ids["c"], step=2, to_email="owner@c.example.com", subject="s",
                                       body="b", send_at="2026-10-08T12:00:00", campaign_id="thread-c",
                                       offer_key="offer_c"))
    pc = generation_prompt(state, run(writer.regenerate_email(
        run(state.get_outbox_item(c_item)), run(state.get_prospect(ids["c"]))))["generation_id"])
    assert "a FOLLOW-UP sent 3 days after the first email: at most 80 words" in pc


def test_the_shared_sequence_says_each_email_adds_something_new(client, monkeypatch):
    state = client.state
    run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    run(writer_for(client).run())
    campaign = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_a")
    prompt = generation_prompt(state, campaign.sequence[0].generation_id)
    assert "never repeats the product or offer sentence of an earlier one" in prompt
    plain = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_c")
    assert "never repeats the product or offer sentence of an earlier one" in generation_prompt(
        state, plain.sequence[0].generation_id)


def test_an_over_limit_template_is_asked_for_again_once(client, monkeypatch):
    state = client.state
    run(seed(state))
    fat = [{"step": 1, "subject": "s1", "body": "Hello {{first_name}}, one. Q?", "delay_days": 0},
           {"step": 2, "subject": "s2", "body": WORDS(100), "delay_days": 3},
           {"step": 3, "subject": "s3", "body": WORDS(70), "delay_days": 4}]
    thin = [dict(s, body=s["body"] if s["step"] == 1 else WORDS(30)) for s in fat]
    seen = []

    async def think(prompt, session_id=None, agent="", task=""):
        if task == "write_sequence":
            seen.append(prompt)
            return thin if "EMAILS THAT ARE TOO LONG" in prompt else fat
        return {"subject": "a note", "body": "Short. Q?"}
    monkeypatch.setattr(Brain, "think_json", AsyncMock(side_effect=think))
    run(writer_for(client).run())
    assert len(seen) == 6, "one retry per campaign, three campaigns"
    assert all("email 2 is 100 words, the limit is 80" in p for p in seen[1::2])
    campaign = run(state.get_campaigns_by_status("draft"))[0]
    assert [count_words(s.body) for s in campaign.sequence[1:]] == [30, 30]


# ── A configured offer sentence ──

def with_offer_sentence(client, state_offer=(1,)):
    offers = [o.model_copy(deep=True) for o in client.config.offers]
    a = offers[0]
    a.content.sentence = "MARKER_A_SENTENCE offer_a is a synthetic service."
    for n in state_offer:
        a.steps[n].state_offer = True
    client.config.offers = offers


def test_the_first_message_states_the_configured_offer_when_the_brief_asks(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    with_offer_sentence(client, (1,))
    writer = writer_for(client)

    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_A_SENTENCE" in prompt
    assert "state the offer in one plain sentence, in these words or very close to them" in prompt
    assert "then the offer in one plain" in prompt and "the brief asks this email" in prompt

    # Only the step that asks for it, and only that offer.
    for tag in ("b", "c"):
        other, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids[tag]))))
        assert "MARKER_A_SENTENCE" not in other and "then the offer in one plain" not in other
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", campaign_id="thread-a",
                                     offer_key="offer_a"))
    two = generation_prompt(state, run(writer.regenerate_email(
        run(state.get_outbox_item(item)), run(state.get_prospect(ids["a"]))))["generation_id"])
    assert "MARKER_A_SENTENCE" not in two, "a later email never states it again"
    assert "MARKER_A_ANGLE_2" in two


def test_a_later_step_that_asks_for_the_sentence_gets_it(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    with_offer_sentence(client, (2,))
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", campaign_id="thread-a",
                                     offer_key="offer_a"))
    draft = run(writer_for(client).regenerate_email(run(state.get_outbox_item(item)),
                                                    run(state.get_prospect(ids["a"]))))
    assert "MARKER_A_SENTENCE" in generation_prompt(state, draft["generation_id"])
    first, _ = run(writer_for(client).build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_A_SENTENCE" not in first


def test_without_the_flag_the_offer_is_not_stated(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    client.config.offers[0].content.sentence = "MARKER_A_SENTENCE"
    prompt, _ = run(writer_for(client).build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_A_SENTENCE" not in prompt and "then the offer in one plain" not in prompt
    assert "No pitch, no product name" not in prompt  # the brief's call to action shapes it


def test_the_sentence_falls_back_to_the_summary_and_needs_one_of_them():
    base = {"key": "offer_x", "content": {"summary": "MARKER_SUMMARY_X"}, "steps": {1: {"state_offer": True}}}
    offer = OfferDefinition(**base)
    assert offer.steps[1].state_offer
    from mercury.offers import OfferBrief
    assert OfferBrief(offer=offer, steps=[1]).offer_sentence(1) == "MARKER_SUMMARY_X"
    with pytest.raises(ValidationError, match="state_offer"):
        OfferDefinition(key="offer_x", steps={1: {"state_offer": True}})


# ── Per-market language and terminology rules ──

def test_market_language_rules_and_terminology_reach_that_markets_prompts(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    market_a = client.config.icp.markets[0]
    market_a.language_rules = ["MARKER_RULE_ONE keep it formal.", "MARKER_RULE_TWO no slang."]
    market_a.terminology = {"avoided_term": "preferred_term"}
    writer = writer_for(client)

    prompt_a, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "Market language rules (binding):" in prompt_a
    assert "MARKER_RULE_ONE keep it formal." in prompt_a and "MARKER_RULE_TWO no slang." in prompt_a
    assert 'say "preferred_term", never "avoided_term"' in prompt_a
    # The built-in line is still there when the market sets no language_line.
    assert "English, for prospects in the United States" in prompt_a

    prompt_b, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["b"]))))
    assert "MARKER_RULE_ONE" not in prompt_b and "preferred_term" not in prompt_b

    # Follow-ups and the shared sequence get them too.
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=3, to_email="owner@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", campaign_id="t"))
    three = generation_prompt(state, run(writer.regenerate_email(
        run(state.get_outbox_item(item)), run(state.get_prospect(ids["a"]))))["generation_id"])
    assert "MARKER_RULE_TWO no slang." in three
    run(writer.run())
    sequence = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_a")
    assert "MARKER_RULE_ONE" in generation_prompt(state, sequence.sequence[0].generation_id)


def test_a_market_can_replace_the_built_in_language_line(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    client.config.icp.markets[1].language_line = "MARKER_LINE_B write in the language of market_b."
    client.config.icp.markets[1].lang = "es"
    writer = writer_for(client)
    prompt_b, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["b"]))))
    assert "MARKER_LINE_B write in the language of market_b." in prompt_b
    assert "Dominican" not in prompt_b.split("Language and register:")[1].split("- Sign off")[0]
    prompt_a, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_LINE_B" not in prompt_a and "English, for prospects in the United States" in prompt_a


def test_prospects_in_no_market_use_the_writer_defaults(client, monkeypatch):
    state = client.state
    run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    stranger = run(state.add_prospect(Prospect(first_name="Sam", last_name="Nowhere", title="Owner",
                                               company="Elsewhere Studio", email="sam@elsewhere.example.com",
                                               email_status="verified")))
    writer = writer_for(client)
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(stranger))))
    assert "English, for prospects in the United States" in prompt and "Market language rules" not in prompt
    client.config.writer.language_rules = ["MARKER_DEFAULT_RULE"]
    client.config.writer.default_language_line = "MARKER_DEFAULT_LINE"
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(stranger))))
    assert "MARKER_DEFAULT_LINE" in prompt and "MARKER_DEFAULT_RULE" in prompt
    assert "English, for prospects in the United States" not in prompt


def test_unconfigured_markets_keep_the_existing_language_lines():
    """What the new language config could replace stays in place until moved."""
    source = (ROOT / "mercury/agents/writer.py").read_text()
    assert "Never mention the" in source and "DOMINICAN REGISTER above" in source
    assert "DOMINICAN REGISTER" in (ROOT / "prompts/writer.md").read_text()


# ── Short business names ──

def named_business(client, ids, name="Acme Roofing LLC - Springfield"):
    async def go():
        async with client.state._connect() as db:
            await db.execute("UPDATE prospects SET company = ? WHERE id = ?", (name, ids["a"]))
            await db.commit()
    run(go())


def test_the_short_name_is_a_fact_and_the_full_name_is_limited(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    named_business(client, ids)
    scripted(monkeypatch, lambda prompt: {
        "subject": "acme roofing llc quotes",
        "body": "Acme Roofing LLC - Springfield posts slowly. Acme Roofing LLC again. Acme Roofing LLC once more."})
    writer = writer_for(client)
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "- Company (full legal name, use it at most once): Acme Roofing LLC - Springfield" in prompt
    assert "- Short business name (use it after the first mention and in subject lines): Acme Roofing" in prompt
    assert 'use "Acme Roofing LLC - Springfield" in full at most once' in prompt

    draft = run(writer._write_personal_email(run(state.get_prospect(ids["a"]))))
    assert draft["subject"] == "Acme Roofing quotes"
    assert draft["body"].count("Acme Roofing LLC") == 1 and draft["body"].count("Acme Roofing") == 3

    # Follow-ups get the same facts.
    item = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", campaign_id="t"))
    follow = generation_prompt(state, run(writer.regenerate_email(
        run(state.get_outbox_item(item)), run(state.get_prospect(ids["a"]))))["generation_id"])
    assert "Short business name" in follow and "Acme Roofing" in follow


def test_a_plain_business_name_adds_no_name_rule(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    prompt, _ = run(writer_for(client).build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "- Company: Example Bakery Delta" in prompt and "Short business name" not in prompt


# ── No review counts or ratings in the Writer's facts ──

def test_review_counts_and_ratings_stay_out_of_every_fact(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    company_id = run(state.get_prospect(ids["a"])).company_id

    async def prepare():
        await state.add_observation("REVIEW_COUNT", company_id=company_id, value_num=72, collector="test")
        await state.add_observation("REVIEW_RATING", company_id=company_id, value_num=4.8, collector="test")
        async with state._connect() as db:
            await db.execute("UPDATE companies SET description = ? WHERE id = ?",
                             ("Family bakery, 4.8 stars from 120 reviews. MARKER_KEEP_DESCRIPTION.", company_id))
            await db.execute(
                "UPDATE prospects SET personalization_notes = ? WHERE id = ?",
                ("pays for local ads. established and busy (72 Google reviews, 4.7) — context only. "
                 "MARKER_KEEP_NOTE", ids["a"]))
            await db.commit()
    run(prepare())
    client.config.offers[0].facts = ["REVIEW_COUNT", "REVIEW_RATING", "SERP_RANK"]
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    prompt, _ = run(writer_for(client).build_personal_prompt(run(state.get_prospect(ids["a"]))))
    for leaked in ("4.8", "120 reviews", "72", "Google reviews", "Number of reviews", "Review rating", "4.7"):
        assert leaked not in prompt.split("FACTS")[1], leaked
    assert "MARKER_KEEP_DESCRIPTION" in prompt and "MARKER_KEEP_NOTE" in prompt and "pays for local ads" in prompt
    assert "Search rank position: 14" in prompt, "other verified facts are still given"

    # Scoring still has them.
    kept = run(state.company_signals(company_id))
    assert kept["REVIEW_COUNT"]["value_num"] == 72
    assert "72 Google reviews" in run(state.get_prospect(ids["a"])).personalization_notes


def test_the_shared_sequence_summary_drops_review_claims_too(client, monkeypatch):
    state = client.state
    ids = run(seed(state))

    async def prepare():
        async with state._connect() as db:
            await db.execute("UPDATE prospects SET personalization_notes = ? WHERE id = ?",
                             ("established and busy (72 Google reviews, 4.7). MARKER_KEEP_NOTE", ids["a"]))
            await db.commit()
    run(prepare())
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    run(writer_for(client).run())
    campaign = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_a")
    prompt = generation_prompt(state, campaign.sequence[0].generation_id)
    assert "MARKER_KEEP_NOTE" in prompt and "72 Google reviews" not in prompt.split("prospects like these:")[1]


# ── Greeting (the Writer half of #56) ──

def test_a_named_contact_is_greeted_by_first_name(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "Hi PatA,\n\nA thing. A question?\n\nSam"})
    writer = writer_for(client)
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "First name to greet them by: PatA" in prompt
    assert "open by greeting them with their first name only (PatA)" in prompt
    assert "shared business inbox" not in prompt and "routing request" not in prompt
    draft = run(writer._write_personal_email(run(state.get_prospect(ids["a"]))))
    assert draft["body"].startswith("Hi PatA,") and draft["flags"] == []


def test_an_unnamed_shared_inbox_gets_the_briefs_routing_role_and_no_generic_greeting(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    client.config.offers[0].routing_role = "the person who books jobs"
    inbox = add_inbox(client, ids)
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(inbox))))
    assert "a shared business inbox (info@), no person's name is known" in prompt
    assert "Make one short routing request addressed to the person who books jobs" in prompt
    assert "takes the place of the call to action" in prompt
    assert "No generic greeting in any language" in prompt
    # The routing request is the one ask: the brief's call to action is not used.
    assert "the routing request from the Greeting requirement below, as its one ask" in prompt
    assert "call to action is not used in this email" in prompt
    assert "Example Bakery Delta Team" not in prompt and "First name to greet them by" not in prompt


def test_the_routing_role_falls_back_to_the_writer_default_and_stays_configurable(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    inbox = add_inbox(client, ids, tag="c", email="office@c.example.com")
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(inbox))))
    assert "addressed to the person who handles this" in prompt
    client.config.writer.routing_role = "the owner or manager"
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(inbox))))
    assert "addressed to the owner or manager" in prompt
    # No commercial role is hard-coded anywhere in the module.
    source = (ROOT / "mercury/greeting.py").read_text().lower()
    assert "job" not in source and "estimate" not in source


def test_follow_ups_to_an_unnamed_inbox_keep_the_role_and_skip_the_greeting(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    client.config.offers[0].routing_role = "the person who books jobs"
    inbox = add_inbox(client, ids)
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    item = run(state.add_outbox_item(prospect_id=inbox, step=2, to_email="info@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", campaign_id="t", offer_key="offer_a"))
    draft = run(writer_for(client).regenerate_email(
        run(state.get_outbox_item(item)), run(state.get_prospect(inbox))))
    prompt = generation_prompt(state, draft["generation_id"])
    assert "still a shared inbox with no known name" in prompt.lower()
    assert "Keep writing to the person who books jobs" in prompt
    assert "do not repeat the routing request word for word" in prompt


@pytest.mark.parametrize("greeting", ["Hi there,", "Hello team,", "Hello,", "Hola, equipo de Example Bakery Delta,",
                                      "Hi Example Bakery Delta,"])
def test_a_generic_greeting_the_model_writes_anyway_is_stripped(client, monkeypatch, greeting):
    state = client.state
    ids = run(seed(state))
    inbox = add_inbox(client, ids)
    scripted(monkeypatch, lambda prompt: {"subject": "a note",
                                          "body": f"{greeting}\n\nYour quote form takes days. Who handles it?"})
    draft = run(writer_for(client)._write_personal_email(run(state.get_prospect(inbox))))
    assert draft["body"] == "Your quote form takes days. Who handles it?"
    assert draft["flags"] == []


def test_a_generic_greeting_to_a_named_contact_is_flagged(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "Hi there,\n\nYour form takes days. Why?"})
    draft = run(writer_for(client)._write_personal_email(run(state.get_prospect(ids["a"]))))
    assert draft["flags"] == ["generic_greeting"]
    assert draft["body"].startswith("Hi there,"), "a named reader's draft is flagged, not rewritten"


def test_the_flag_reaches_the_outbox_and_needs_the_same_explicit_approval(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "Hi there,\n\nYour form takes days. Why?"})
    run(writer_for(client).run())
    row = next(r for r in client.get("/api/outbox").json()["pending"] if r["prospect_id"] == ids["a"] and r["step"] == 1)
    assert row["flags"] == ["generic_greeting"]
    assert row["flag_details"] == [{"code": "generic_greeting", "label": "Generic greeting"}]
    assert client.post(f"/api/outbox/{row['id']}/approve", json={"revision": row["revision"]}).status_code == 409
    # Editing the greeting away clears it.
    edited = client.put(f"/api/outbox/{row['id']}", json={"subject": "a note", "body": "Hi PatA,\n\nYour form takes days. Why?",
                                                          "revision": row["revision"]}).json()
    assert edited["flags"] == []


async def lookup(state, company_id, people=None):
    await state.save_registry_lookup(company_id, {
        "provider": "fl_sunbiz", "status": "matched", "entity_name": "EXAMPLE BAKERY DELTA, LLC",
        "document_number": "L15000000009", "confidence": 0.95, "source_url": "https://search.example.com/x",
        "people": people or [{"name": "Jane Doe", "title": "Manager", "raw_title": "MGR"}]})


def test_a_registry_name_waiting_for_review_is_not_used_and_an_accepted_one_is(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    inbox = add_inbox(client, ids)
    company_id = run(state.get_prospect(inbox)).company_id
    run(lookup(state, company_id))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    writer = writer_for(client)

    prospect = run(state.get_prospect(inbox))
    company = run(state.get_company(company_id))
    pending = run(plan_greeting(state, client.config, prospect, company))
    assert pending.mode == ROUTING and pending.first_name == "" and pending.status == "registry_pending"
    prompt, _ = run(writer.build_personal_prompt(prospect))
    assert "Jane" not in prompt and "routing request" in prompt

    run(state.set_registry_review(inbox, company_id, "Jane Doe", "accepted", "tester"))
    accepted = run(plan_greeting(state, client.config, prospect, company))
    assert accepted.mode == NAMED and accepted.first_name == "Jane" and accepted.status == "registry"
    prompt, _ = run(writer.build_personal_prompt(prospect))
    assert "First name to greet them by: Jane" in prompt and "name from a public business registry" in prompt
    assert "routing request" not in prompt

    run(state.set_registry_review(inbox, company_id, "Jane Doe", "dismissed", "tester"))
    assert run(plan_greeting(state, client.config, prospect, company)).mode == ROUTING


def test_a_personal_address_without_a_name_gets_neither_greeting_nor_routing(client):
    state = client.state
    ids = run(seed(state))
    nameless = run(state.add_prospect(Prospect(
        first_name="", last_name="", title="Owner", company="Example Bakery Delta",
        company_id=run(state.get_prospect(ids["a"])).company_id, email="owner2@a.example.com",
        email_status="verified")))
    plan = run(plan_greeting(state, client.config, run(state.get_prospect(nameless)), None))
    assert plan.mode == NONE and not plan.shared_inbox
    from mercury.greeting import prompt_lines
    text = prompt_lines(plan)
    assert "routing request" not in text and "No generic greeting" in text


def test_the_shared_sequence_for_unnamed_inboxes_has_no_greeting_line_and_the_sender_drops_it(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    inbox = add_inbox(client, ids)
    run(state.update_prospect_status(ids["a"], "queued"))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    run(writer_for(client).run())
    campaign = next(c for c in run(state.get_campaigns_by_status("draft")) if inbox in c.prospect_ids)
    prompt = generation_prompt(state, campaign.sequence[0].generation_id)
    assert "none of these readers has a known name" in prompt
    stage(client)
    for row in rows(state):
        if row["prospect_id"] == inbox and row["step"] > 1:
            assert not row["body"].lower().startswith(("hello", "hi")), row["body"]
            assert "Example Bakery Delta" not in row["body"]
            assert row["body"].startswith("Step ")


def test_the_sender_greets_a_named_contact_and_an_accepted_registry_name(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    inbox = add_inbox(client, ids)
    company_id = run(state.get_prospect(inbox)).company_id
    run(lookup(state, company_id))
    run(state.set_registry_review(inbox, company_id, "Jane Doe", "accepted", "tester"))
    scripted(monkeypatch, lambda prompt: {"subject": "a note", "body": "A thing. A question?"})
    run(writer_for(client).run())
    stage(client)
    followups = {r["prospect_id"]: r for r in rows(state) if r["step"] == 2}
    assert followups[ids["a"]]["body"].startswith("Hello PatA, step 2")
    assert followups[inbox]["body"].startswith("Hello Jane, step 2")
