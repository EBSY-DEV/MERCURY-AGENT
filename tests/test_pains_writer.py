"""Pains wired into the Writer on top of offer routing (#59 on #57).

Synthetic only: PAIN_TEST_* pains, offer_a / offer_b / offer_c, TEST_SIGNAL_*
codes, fictional ceramics businesses on example.com. The distinctive words of
each pain show where it reaches a prompt, a draft or an API response.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from mercury.agents.sender import Sender
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.gate import pre_send_check
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.offers import ConfirmedPain
from mercury.pains import propose_pains, select_pain
from tests.test_offers import (  # noqa: F401  (client is a fixture)
    client,
    fake_brain,
    generation_prompt,
    run,
    seed,
)

HUMAN = "tester"

# Confirmed pains. Each is bound to an offer so routing decides which one fits.
PAIN_A = dict(label="Kiln orders wait in a queue", owner_words="MARKER_PAIN_A the kiln queue grows every week",
              scene="MARKER_SCENE_A Friday evening, trays stacked", cost="MARKER_COST_A lost firings",
              offer_key="offer_a")
PAIN_B = dict(label="Glaze samples go missing", owner_words="MARKER_PAIN_B glaze samples vanish from the shelf",
              offer_key="offer_b")
# Rejected: distinctive words that must only ever sit in the never-use list.
REJECTED_ANY = dict(label="Humidity cracks the glaze",
                    owner_words="MARKER_REJECTED_ANY humidity cracks the glaze overnight")
REJECTED_B = dict(label="Courier pickups are missed",
                  owner_words="MARKER_REJECTED_B courier pickups are missed", offer_key="offer_b")
DRAFT_WITH_REJECTED = "Pat, does humidity cracks the glaze overnight, and does the glaze cracks again?"


async def add(state, code, status="confirmed", **fields):
    await state.add_pain(code, status=status, status_by=HUMAN if status != "proposed" else "", **fields)


async def library(state):
    await add(state, "PAIN_TEST_A", **PAIN_A)
    await add(state, "PAIN_TEST_B", **PAIN_B)
    await add(state, "PAIN_TEST_R", status="rejected", **REJECTED_ANY)
    await add(state, "PAIN_TEST_RB", status="rejected", **REJECTED_B)


def never_use_part(prompt: str) -> str:
    assert "NEVER raise these" in prompt
    return prompt[prompt.index("NEVER raise these"):]


# ── Selection follows the routing ──

def test_each_prospect_gets_the_pain_of_its_routed_offer(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    writer = Writer(Brain(state), state, client.config)

    prompt_a, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_PAIN_A" in prompt_a and "MARKER_SCENE_A" in prompt_a and "MARKER_COST_A" in prompt_a
    assert "MARKER_PAIN_B" not in prompt_a
    assert "Pain reference (internal, never write it in the email): PAIN_TEST_A" in prompt_a

    prompt_b, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["b"]))))
    assert "MARKER_PAIN_B" in prompt_b and "MARKER_PAIN_A" not in prompt_b

    # offer_c has no confirmed pain: the brief says so and nothing is invented.
    prompt_c, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["c"]))))
    assert "Confirmed pain: none supplied" in prompt_c
    assert "MARKER_PAIN" not in prompt_c

    # The hook is replaceable and can be switched off.
    writer.pain_source = None
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_PAIN_A" not in prompt and "Confirmed pain: none supplied" in prompt


def test_default_pain_source_returns_the_confirmed_pain(client):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    writer = Writer(Brain(state), state, client.config)
    prospect = run(state.get_prospect(ids["a"]))
    pain = run(writer.pain_source(prospect, "offer_a", 1))
    assert pain == ConfirmedPain(code="PAIN_TEST_A", words=PAIN_A["owner_words"],
                                 scene=PAIN_A["scene"], cost=PAIN_A["cost"])
    # An offer-bound pain never reaches an email for another offer.
    assert run(writer.pain_source(prospect, "offer_b", 1)).code == "PAIN_TEST_B"
    assert run(writer.pain_source(prospect, "offer_c", 1)) is None


# ── Rejected pains: the prompt, the writer and the gate ──

def test_a_rejected_pain_is_only_in_the_never_use_block(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    writer = Writer(Brain(state), state, client.config)
    prospect = run(state.get_prospect(ids["a"]))

    prompt, _ = run(writer.build_personal_prompt(prospect))
    tail = never_use_part(prompt)
    assert "MARKER_REJECTED_ANY" in tail
    assert "MARKER_REJECTED_ANY" not in prompt[: prompt.index("NEVER raise these")]
    assert prompt.count("MARKER_REJECTED_ANY") == 1
    # The offer_b-bound rejected pain would leak another offer's content into offer_a's prompt.
    assert "MARKER_REJECTED_B" not in prompt
    # ...but offer_b's own prompt carries it.
    prompt_b, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["b"]))))
    assert "MARKER_REJECTED_B" in never_use_part(prompt_b)

    # Rewrites carry the same list.
    item_id = run(state.add_outbox_item(prospect_id=ids["a"], step=2, to_email="owner@a.example.com",
                                        subject="s", body="b", send_at="2026-10-08T12:00:00",
                                        campaign_id="c-test", offer_key="offer_a"))
    draft = run(writer.regenerate_email(run(state.get_outbox_item(item_id)), prospect))
    rewrite = generation_prompt(state, draft["generation_id"])
    assert "MARKER_REJECTED_ANY" in never_use_part(rewrite) and rewrite.count("MARKER_REJECTED_ANY") == 1
    assert "MARKER_PAIN_A" in rewrite


def test_a_sequence_prompt_carries_the_never_use_list(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    campaign = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_a")
    prompt = generation_prompt(state, campaign.sequence[0].generation_id)
    assert prompt.count("MARKER_REJECTED_ANY") == 1 and "MARKER_REJECTED_ANY" in never_use_part(prompt)
    assert "MARKER_PAIN_A" in prompt and "MARKER_REJECTED_B" not in prompt
    assert ids


def test_a_draft_that_raises_a_rejected_pain_is_never_staged(client, monkeypatch):
    state = client.state
    run(seed(state))
    run(library(state))
    fake_brain(monkeypatch, personal={"subject": "a note", "body": DRAFT_WITH_REJECTED})
    run(Writer(Brain(state), state, client.config).run())
    # No personalized first email was kept for anyone.
    assert run(state.get_outbox()) == []


def test_a_sequence_that_raises_a_rejected_pain_is_discarded(client, monkeypatch):
    state = client.state
    run(seed(state))
    run(library(state))

    async def think_json(prompt, session_id=None, agent="", task=""):
        if task == "write_sequence":
            return [{"step": i, "subject": f"note {i}", "body": DRAFT_WITH_REJECTED, "delay_days": 0}
                    for i in (1, 2, 3)]
        return {"subject": "a note", "body": "A specific observation. A question?"}
    monkeypatch.setattr(Brain, "think_json", AsyncMock(side_effect=think_json))
    run(Writer(Brain(state), state, client.config).run())
    assert run(state.get_campaigns_by_status("draft")) == []


def sender_for(client):
    from tests.test_outbox_native import Env, FakeProvider

    sender = Sender(None, client.state, client.config, Env())
    sender.provider = FakeProvider()
    sender.send_pacing = False
    return sender


def test_the_gate_stops_a_rejected_pain_added_after_staging(client, monkeypatch):
    """A reviewer's edit (or any other path) can put a rejected pain back in
    the text; the gate checks what actually leaves."""
    state = client.state
    ids = run(seed(state))
    run(library(state))
    now = datetime.now(timezone.utc).isoformat()
    bad = run(state.add_outbox_item(
        prospect_id=ids["a"], to_email="owner@a.example.com", subject="a note", body=DRAFT_WITH_REJECTED,
        send_at=now, status="approved", campaign_id="c-1", step=1, offer_key="offer_a",
        pain_code="PAIN_TEST_A"))
    good = run(state.add_outbox_item(
        prospect_id=ids["a"], to_email="owner@a.example.com", subject="a note",
        body="Pat, saw the studio on Main St. Open to a short chat?",
        send_at=now, status="approved", campaign_id="c-2", step=1, offer_key="offer_a"))
    sender = sender_for(client)
    run(sender._drain_due())
    failed = run(state.get_outbox_item(bad))
    assert failed["status"] == "failed" and "rejected pain PAIN_TEST_R" in failed["error"]
    assert [m["subject"] for m in sender.provider.sent] == ["a note"]
    assert run(state.get_outbox_item(good))["status"] == "sent"


def test_rejected_pains_and_blocked_case_studies_are_separate_reasons():
    rejected = [{"code": "PAIN_TEST_R", "label": REJECTED_ANY["label"], "owner_words": REJECTED_ANY["owner_words"],
                 "scene": "", "avoid_terms": []}]
    blocked = ["Example Garage Beta"]
    clean = "Pat, saw the studio on Main St. Open to a short chat?"
    only_case = pre_send_check("pat@example.com", "a note", clean + " Like Example Garage Beta did.",
                               blocked_references=blocked, rejected_pains=rejected)
    assert len(only_case.reasons) == 1 and "case study" in only_case.reasons[0]
    only_pain = pre_send_check("pat@example.com", "a note", DRAFT_WITH_REJECTED,
                               blocked_references=blocked, rejected_pains=rejected)
    assert len(only_pain.reasons) == 1 and "rejected pain PAIN_TEST_R" in only_pain.reasons[0]
    both = pre_send_check("pat@example.com", "a note", DRAFT_WITH_REJECTED + " Like Example Garage Beta did.",
                          blocked_references=blocked, rejected_pains=rejected)
    assert len(both.reasons) == 2


# ── pain_code on the outbox and in the API ──

def test_pain_code_lands_on_every_staged_row_and_in_the_api(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    sender = sender_for(client)
    for campaign in run(state.get_campaigns_by_status("draft")):
        run(sender._stage_campaign_native(campaign))
    rows = run(state.get_outbox())
    by = {(r["prospect_id"], r["step"]): r for r in rows}
    # A personalized first email and the template follow-ups of a one-prospect group.
    assert {by[(ids["a"], s)]["pain_code"] for s in (1, 2, 3)} == {"PAIN_TEST_A"}
    assert {by[(ids["b"], s)]["pain_code"] for s in (1, 2, 3)} == {"PAIN_TEST_B"}
    assert {by[(ids["c"], s)]["pain_code"] for s in (1, 2, 3)} == {""}

    pending = client.get("/api/outbox").json()["pending"]
    row = next(r for r in pending if r["prospect_id"] == ids["a"] and r["step"] == 2)
    assert row["pain_code"] == "PAIN_TEST_A"
    assert row["pain"]["code"] == "PAIN_TEST_A" and row["pain"]["words"] == PAIN_A["owner_words"]
    assert row["pain"]["label"] == PAIN_A["label"] and row["pain"]["status"] == "confirmed"
    assert row["offer"]["key"] == "offer_a"  # next to #57's offer object
    none = next(r for r in pending if r["prospect_id"] == ids["c"] and r["step"] == 1)
    assert none["pain_code"] == "" and none["pain"] is None
    assert client.get(f"/api/outbox/{row['id']}").json()["pain"]["code"] == "PAIN_TEST_A"

    # The recorded prompt reconstructs the brief the Writer was given.
    prompt = generation_prompt(state, by[(ids["a"], 1)]["generation_id"])
    assert "MARKER_PAIN_A" in prompt and "PAIN_TEST_A" in prompt


def test_a_shared_sequence_carries_a_pain_only_when_every_prospect_has_it(client, monkeypatch):
    state = client.state

    async def go():
        for code in ("TEST_SIGNAL_A", "TEST_SIGNAL_P"):
            await state.upsert_signal_code(code, label=code, value_type="bool", status="confirmed")
        ids = []
        for n, extra in enumerate(([], ["TEST_SIGNAL_P"])):
            cid = await state.add_company(Company(name=f"Example Mugs {n}", domain=f"m{n}.example.com",
                                                  location="Alphaville, Exampleland", industry="segment_a"))
            for code in ["TEST_SIGNAL_A", *extra]:
                await state.add_observation(code, company_id=cid, value_num=1, collector="test")
            ids.append(await state.add_prospect(Prospect(
                first_name=f"Sam{n}", last_name="Example", title="Owner", company=f"Example Mugs {n}",
                company_id=cid, industry="segment_a", email=f"owner@m{n}.example.com",
                email_status="verified", email_verified=True)))
        await add(state, "PAIN_TEST_A", signal_codes=["TEST_SIGNAL_A"], label="Plain pain",
                  owner_words="MARKER_SHARED_A the kiln queue")
        # More specific (offer-bound) and only for prospects with TEST_SIGNAL_P.
        await add(state, "PAIN_TEST_P", signal_codes=["TEST_SIGNAL_P"], offer_key="offer_a", label="Special pain",
                  owner_words="MARKER_SPECIAL_P the lid orders")
        return ids
    ids = run(go())
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    campaign = run(state.get_campaigns_by_status("draft"))[0]
    assert sorted(campaign.prospect_ids) == sorted(ids)
    # Prospect 0 gets PAIN_TEST_A, prospect 1 gets PAIN_TEST_P: no single shared pain.
    assert {s.pain_code for s in campaign.sequence} == {""}
    prompt = generation_prompt(state, campaign.sequence[0].generation_id)
    assert "MARKER_SHARED_A" not in prompt and "MARKER_SPECIAL_P" not in prompt
    assert "Confirmed pain: none supplied" in prompt
    # Their personalized first emails keep their own pain.
    run(sender_for(client)._stage_campaign_native(campaign))
    rows = {(r["prospect_id"], r["step"]): r["pain_code"] for r in run(state.get_outbox())}
    assert rows[(ids[0], 1)] == "PAIN_TEST_A" and rows[(ids[1], 1)] == "PAIN_TEST_P"
    assert rows[(ids[0], 2)] == "" and rows[(ids[1], 3)] == ""


def test_a_regenerated_draft_records_its_pain(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    for campaign in run(state.get_campaigns_by_status("draft")):
        run(sender_for(client)._stage_campaign_native(campaign))
    row = next(r for r in client.get("/api/outbox").json()["pending"]
               if r["prospect_id"] == ids["a"] and r["step"] == 2)
    run(state.update_outbox_item(row["id"], pain_code=""))  # as if recorded before pains existed
    response = client.post(f"/api/outbox/{row['id']}/regenerate",
                           json={"instruction": "Shorter", "revision": row["revision"]})
    assert response.status_code == 200, response.text
    assert response.json()["pain_code"] == "PAIN_TEST_A" and response.json()["pain"]["code"] == "PAIN_TEST_A"
    history = client.get(f"/api/outbox/{row['id']}/generation-history").json()["generations"]
    assert "MARKER_PAIN_A" in history[0]["prompt"] and history[0]["task"] == "regenerate_email"


def test_a_regenerated_draft_that_raises_a_rejected_pain_is_refused(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    fake_brain(monkeypatch, personal={"subject": "a note", "body": DRAFT_WITH_REJECTED})
    item_id = run(state.add_outbox_item(prospect_id=ids["a"], step=1, to_email="owner@a.example.com",
                                        subject="s", body="Fine text. A question?",
                                        send_at="2026-10-08T12:00:00", campaign_id="c-test",
                                        offer_key="offer_a"))
    row = run(state.get_outbox_item(item_id))
    response = client.post(f"/api/outbox/{item_id}/regenerate",
                           json={"instruction": "", "revision": row["revision"]})
    assert response.status_code >= 400
    assert run(state.get_outbox_item(item_id))["body"] == "Fine text. A question?"


# ── Without offers ──

def test_without_offers_the_pain_block_stands_alone(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    client.config.offers = []
    run(add(state, "PAIN_TEST_G", label="Unbound pain", owner_words="MARKER_GENERAL_PAIN the glaze test",
            signal_codes=["TEST_SIGNAL_A"]))
    run(add(state, "PAIN_TEST_R", status="rejected", **REJECTED_ANY))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    row = next(r for r in run(state.get_outbox()) if r["prospect_id"] == ids["a"])
    assert row["pain_code"] == "PAIN_TEST_G"
    prompt = generation_prompt(state, row["generation_id"])
    assert "OFFER BRIEF" not in prompt and "MARKER_GENERAL_PAIN" in prompt
    assert "the only pain you may raise" in prompt and "MARKER_REJECTED_ANY" in never_use_part(prompt)
    # A prospect no pain fits is told so, not left to invent one.
    other = next(r for r in run(state.get_outbox()) if r["prospect_id"] == ids["b"])
    assert other["pain_code"] == "" and "none confirmed" in generation_prompt(state, other["generation_id"])


# ── Retraining ──

def test_retraining_never_resurrects_a_rejected_pain_in_a_prompt(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    run(library(state))
    outcomes = run(propose_pains(state, [
        REJECTED_ANY["owner_words"],
        "Overnight humidity cracks the glaze again",
        "Glaze samples go missing from the shelf",
        "Customers never get the kiln schedule in time",
    ]))
    assert [o["outcome"] for o in outcomes][:2] == ["rejected", "rejected"]
    assert outcomes[2]["outcome"] == "exists"
    assert outcomes[3]["outcome"] == "proposed"
    rejected = {p["code"] for p in run(state.list_pains(status="rejected"))}
    assert rejected == {"PAIN_TEST_R", "PAIN_TEST_RB"}
    # The new proposal is not confirmed, so it cannot be written from.
    fake_brain(monkeypatch)
    prompt, _ = run(Writer(Brain(state), state, client.config).build_personal_prompt(
        run(state.get_prospect(ids["a"]))))
    assert "kiln schedule" not in prompt
    assert prompt.count("MARKER_REJECTED_ANY") == 1
    selection = run(select_pain(state, offer_key="offer_a"))
    assert selection.code == "PAIN_TEST_A"
    assert {p["code"] for p in selection.rejected} == {"PAIN_TEST_R", "PAIN_TEST_RB"}
