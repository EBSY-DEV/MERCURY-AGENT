"""Offer routing, the Writer's per-prospect offer brief, and case-study scope.

Synthetic fixtures only: offer_a / offer_b / offer_c, TEST_SIGNAL_* codes,
segment_a / segment_b, market_a / market_b, fictional businesses on
example.com. Distinctive MARKER strings show what reaches a prompt.
"""

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import mercury.config as config_module
import mercury.dashboard as dash
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.config import MercuryConfig
from mercury.gate import pre_send_check
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.offers import (
    ConfirmedPain,
    RoutingContext,
    build_brief,
    check_offers,
    route,
    route_prospect,
    routing_enabled,
)
from mercury.state import StateManager

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"

MARKETS = [
    {"name": "market_a", "places": ["Alphaville"], "terms": ["service_a"]},
    {"name": "market_b", "places": ["Betatown"], "terms": ["service_b"]},
]

OFFERS = [
    {
        "key": "offer_a",
        "markets": ["market_a"],
        "segments": ["segment_a"],
        "signals": {"require": ["TEST_SIGNAL_A"], "exclude": ["TEST_SIGNAL_X"]},
        "content": {"name": "Offer A", "summary": "MARKER_A_SUMMARY a synthetic service.",
                    "claims": ["MARKER_A_CLAIM"]},
        "restrictions": ["MARKER_A_RESTRICTION"],
        "facts": ["SERP_RANK", "TEST_SIGNAL_A"],
        "materials": [{"name": "MARKER_A_MATERIAL", "kind": "one-pager"}],
        "steps": {1: {"cta": "MARKER_A_CTA_1", "angle": "MARKER_A_ANGLE_1"},
                  2: {"cta": "MARKER_A_CTA_2", "angle": "MARKER_A_ANGLE_2"},
                  3: {"cta": "MARKER_A_CTA_3"}},
        "case_studies": [
            {"name": "Example Bakery Alpha", "summary": "MARKER_CASE_IN",
             "scope": {"markets": ["market_a"]}},
            {"name": "Example Garage Beta", "aliases": ["Garage Beta"], "summary": "MARKER_CASE_OUT",
             "scope": {"markets": ["market_b"]}},
        ],
        "evidence": {"description": "carried TEST_SIGNAL_A", "require": ["TEST_SIGNAL_A"],
                     "min_sample": 3},
    },
    {
        "key": "offer_b",
        "segments": ["segment_b"],
        "signals": {"require": ["TEST_SIGNAL_B"]},
        "content": {"name": "Offer B", "summary": "MARKER_B_SUMMARY", "claims": ["MARKER_B_CLAIM"]},
        "restrictions": ["MARKER_B_RESTRICTION"],
        "steps": {1: {"cta": "MARKER_B_CTA_1"}, 2: {"angle": "MARKER_B_ANGLE_2"}},
        "case_studies": [{"name": "Example Florist Gamma", "summary": "MARKER_B_CASE"}],
    },
    {"key": "offer_c", "default": True, "content": {"summary": "MARKER_OFFER_C_SUMMARY"}},
]

TEST_CODES = ("TEST_SIGNAL_A", "TEST_SIGNAL_B", "TEST_SIGNAL_X")


def run(coro):
    return asyncio.run(coro)


def make_config(offers=OFFERS, markets=MARKETS, **product) -> MercuryConfig:
    data = yaml.safe_load(TEMPLATE.read_text())
    data["icp"]["markets"] = copy.deepcopy(markets)
    data["offers"] = copy.deepcopy(offers)
    data["product"].update(product)
    config = MercuryConfig(**data)
    config.channels.email.provider = "smtp"
    config.compliance.postal_address = "1 Example Street"
    return config


def ctx(market="", segment="", **signals):
    return RoutingContext(market=market, segment=segment, observations={
        code: {"signal_code": code, "value_num": value} for code, value in signals.items()})


# ── Routing (pure) ──

def test_rule_match_and_reason():
    decision = route(make_config().offers, ctx("market_a", "segment_a", TEST_SIGNAL_A=1))
    assert decision.key == "offer_a" and not decision.is_default
    assert "market market_a" in decision.reason and "has TEST_SIGNAL_A" in decision.reason


def test_first_match_wins_in_configured_order():
    both = [{"key": "offer_a", "signals": {"require": ["TEST_SIGNAL_A"]}},
            {"key": "offer_b", "signals": {"require": ["TEST_SIGNAL_A"]}}]
    seen = ctx(TEST_SIGNAL_A=1)
    assert route(make_config(both).offers, seen).key == "offer_a"
    assert route(make_config(list(reversed(both))).offers, seen).key == "offer_b"


def test_exclusion_skips_offer_and_falls_back():
    decision = route(make_config().offers,
                     ctx("market_a", "segment_a", TEST_SIGNAL_A=1, TEST_SIGNAL_X=1))
    assert decision.key == "offer_c" and decision.is_default
    assert "excluded by TEST_SIGNAL_X" in decision.checks[0].why


def test_segment_and_market_eligibility():
    offers = make_config().offers
    # Right signal, wrong market: offer_a does not apply.
    assert route(offers, ctx("market_b", "segment_a", TEST_SIGNAL_A=1)).key == "offer_c"
    assert route(offers, ctx("market_b", "segment_b", TEST_SIGNAL_B=1)).key == "offer_b"
    assert route(offers, ctx("market_b", "segment_a", TEST_SIGNAL_B=1)).key == "offer_c"


def test_fallback_and_no_default():
    decision = route(make_config().offers, ctx())
    assert decision.key == "offer_c" and decision.is_default
    assert "default" in decision.reason
    no_default = [o for o in OFFERS if not o.get("default")]
    assert route(make_config(no_default).offers, ctx()).offer is None


def test_a_zero_observation_is_not_a_signal():
    assert route(make_config().offers, ctx("market_a", "segment_a", TEST_SIGNAL_A=0)).key == "offer_c"
    # A text signal's row is the finding.
    assert route(make_config().offers, ctx("market_a", "segment_a", TEST_SIGNAL_A=None)).key == "offer_a"


# ── Config ──

def test_legacy_and_empty_offer_configs_still_load_and_route_nothing():
    legacy = make_config([{"key": "offer_a", "requires_demo": True, "demo_kind": "voice"}])
    assert legacy.offers[0].requires_demo and not routing_enabled(legacy)
    assert route(legacy.offers, ctx(TEST_SIGNAL_A=1)).offer is None
    assert not routing_enabled(make_config([]))


@pytest.mark.parametrize("offers, message", [
    ([{"key": "offer_a", "markets": ["market_z"]}], "market_z"),
    ([{"key": "offer_a", "default": True}, {"key": "offer_b", "default": True}], "only one offer"),
    ([{"key": "offer_a", "signals": {"require": ["NOT A CODE"]}}], "not a signal code"),
    ([{"key": "offer_a", "steps": {9: {"cta": "x"}}}], "steps are numbered"),
    ([{"key": "offer_a", "case_studies": [{"name": "Example Co", "scope": {"markets": ["nowhere"]}}]}],
     "nowhere"),
    ([{"key": "offer_a", "evidence": {"description": "x", "require": []}}], "require"),
])
def test_invalid_offer_configs_fail_clearly(offers, message):
    with pytest.raises(ValidationError, match=message):
        make_config(offers)


def test_check_offers_warns_about_unknown_and_unconfirmed_codes():
    problems = check_offers(make_config(), {"TEST_SIGNAL_A": "confirmed", "TEST_SIGNAL_B": "proposed",
                                            "SERP_RANK": "confirmed"})
    text = "\n".join(problems)
    assert "TEST_SIGNAL_X is not in the signal vocabulary" in text
    assert "TEST_SIGNAL_B is proposed" in text
    unrouted = make_config([{"key": "offer_a", "signals": {"require": ["SERP_RANK"]}}, {"key": "offer_b"}])
    text = "\n".join(check_offers(unrouted, {"SERP_RANK": "confirmed"}))
    assert "offer_b: no markets" in text and "No offer is the default" in text


# ── Writer, outbox and API (mocked Brain) ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    config = make_config(description="PRODUCT_DESC mentions MARKER_B_SUMMARY too")
    monkeypatch.setattr(config_module, "load_config", lambda *a: config)
    monkeypatch.setattr(config_module, "_find_config_file", lambda: str(TEMPLATE))
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "mercury.db")
    # The trainer's product knowledge describes every offer.
    real_load_skill = Brain.load_skill
    monkeypatch.setattr(Brain, "load_skill", lambda self, name: (
        "PRODUCT_KNOWLEDGE: MARKER_B_SUMMARY and MARKER_A_SUMMARY" if name == "product_knowledge"
        else real_load_skill(self, name)))
    state = StateManager(str(dash.DB_PATH))
    run(state.init_db())
    with TestClient(dash.app) as c:
        c.state, c.config = state, config
        yield c


async def seed(state):
    for code in TEST_CODES:
        await state.upsert_signal_code(code, label=code.replace("_", " ").title(),
                                       value_type="bool", status="confirmed")
    from mercury.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    ids = {}
    rows = [
        ("a", "Example Bakery Delta", "Alphaville", "segment_a", {"TEST_SIGNAL_A": 1, "SERP_RANK": 14}),
        ("b", "Example Florist Epsilon", "Betatown", "segment_b", {"TEST_SIGNAL_B": 1}),
        ("c", "Example Tailor Zeta", "Betatown", "segment_a", {}),
    ]
    for tag, name, place, segment, signals in rows:
        cid = await state.add_company(Company(name=name, domain=f"{tag}.example.com",
                                              location=f"{place}, Exampleland", industry=segment))
        for code, value in signals.items():
            await state.add_observation(code, company_id=cid, value_num=value, collector="test")
        ids[tag] = await state.add_prospect(Prospect(
            first_name=f"Pat{tag.upper()}", last_name="Example", title="Owner", company=name,
            company_id=cid, industry=segment, email=f"owner@{tag}.example.com",
            email_status="verified", email_verified=True))
    return ids


def fake_brain(monkeypatch, personal=None):
    async def think_json(prompt, session_id=None, agent="", task=""):
        if task == "write_sequence":
            return [{"step": i, "subject": f"note {i}", "body": f"Hello {{{{first_name}}}}, step {i}. Question?",
                     "delay_days": 0 if i == 1 else 3} for i in (1, 2, 3)]
        return personal or {"subject": "a note", "body": "A specific observation. A question?"}
    model = AsyncMock(side_effect=think_json)
    monkeypatch.setattr(Brain, "think_json", model)
    return model


def generation_prompt(state, generation_id):
    async def go():
        async with state._connect() as db:
            cursor = await db.execute("SELECT prompt FROM email_generations WHERE id = ?", (generation_id,))
            return (await cursor.fetchone())[0]
    return run(go())


def unrelated_to_a(prompt):
    return [m for m in ("MARKER_B", "MARKER_OFFER_C", "Example Florist Gamma", "Example Garage Beta",
                        "Garage Beta", "MARKER_CASE_OUT", "MARKER_A_MATERIAL") if m in prompt]


def test_writer_routes_stamps_and_briefs_each_offer(client, monkeypatch):
    from mercury.agents.sender import Sender
    from tests.test_outbox_native import Env, FakeProvider

    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())

    campaigns = {c.offer_key: c for c in run(state.get_campaigns_by_status("draft"))}
    assert set(campaigns) == {"offer_a", "offer_b", "offer_c"}
    assert campaigns["offer_a"].prospect_ids == [ids["a"]]
    assert campaigns["offer_c"].prospect_ids == [ids["c"]]

    # Personalized first emails carry the offer; so do the template steps.
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    for campaign in campaigns.values():
        run(sender._stage_campaign_native(campaign))
    rows = run(state.get_outbox())
    assert len(rows) == 9
    by_prospect = {pid: {r["step"]: r for r in rows if r["prospect_id"] == pid} for pid in ids.values()}
    assert {r["offer_key"] for r in by_prospect[ids["a"]].values()} == {"offer_a"}
    assert {r["offer_key"] for r in by_prospect[ids["b"]].values()} == {"offer_b"}
    assert by_prospect[ids["a"]][1]["subject"] == "a note"  # the personalized draft, not the template

    # The API the Outbox reads.
    pending = client.get("/api/outbox").json()["pending"]
    row = next(r for r in pending if r["prospect_id"] == ids["a"] and r["step"] == 1)
    assert row["offer_key"] == "offer_a"
    assert row["offer"]["label"] == "Offer A" and row["offer"]["configured"]
    assert "offer_a rule matched" in row["offer"]["reason"] and not row["offer"]["is_default"]
    fallback = next(r for r in pending if r["prospect_id"] == ids["c"] and r["step"] == 2)
    assert fallback["offer"]["key"] == "offer_c" and fallback["offer"]["is_default"]
    assert client.get(f"/api/outbox/{row['id']}").json()["offer"]["key"] == "offer_a"

    # The prompts recorded for offer_a: its brief, nothing of any other offer.
    personal = generation_prompt(state, by_prospect[ids["a"]][1]["generation_id"])
    sequence = generation_prompt(state, campaigns["offer_a"].sequence[0].generation_id)
    for prompt in (personal, sequence):
        assert "OFFER BRIEF" in prompt and "MARKER_A_SUMMARY" in prompt and "MARKER_A_CLAIM" in prompt
        assert "MARKER_A_RESTRICTION" in prompt and "Example Bakery Alpha" in prompt
        assert unrelated_to_a(prompt) == [], unrelated_to_a(prompt)
        assert "PRODUCT_DESC" not in prompt and "PRODUCT_KNOWLEDGE" not in prompt
    assert "MARKER_A_CTA_1" in personal and "MARKER_A_CTA_2" not in personal
    assert all(f"MARKER_A_CTA_{n}" in sequence for n in (1, 2, 3)) and "MARKER_A_ANGLE_2" in sequence
    assert "Search rank position: 14" in personal  # an existing SERP_RANK observation

    b_prompt = generation_prompt(state, by_prospect[ids["b"]][1]["generation_id"])
    assert "MARKER_B_SUMMARY" in b_prompt and "Example Florist Gamma" in b_prompt
    assert not [m for m in ("MARKER_A", "Example Bakery Alpha", "MARKER_OFFER_C") if m in b_prompt]


def test_follow_up_rewrite_keeps_offer_and_gets_its_step(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch)
    writer = Writer(Brain(state), state, client.config)
    run(writer.run())
    campaign = next(c for c in run(state.get_campaigns_by_status("draft")) if c.offer_key == "offer_a")
    item_id = run(state.add_outbox_item(prospect_id=ids["a"], campaign_id=campaign.id, step=2,
                                        to_email="owner@a.example.com", subject="s", body="b",
                                        send_at="2026-10-08T12:00:00"))
    item = run(state.get_outbox_item(item_id))
    draft = run(writer.regenerate_email(item, run(state.get_prospect(ids["a"]))))
    prompt = generation_prompt(state, draft["generation_id"])
    assert "MARKER_A_ANGLE_2" in prompt and "MARKER_A_CTA_2" in prompt
    assert "MARKER_A_CTA_1" not in prompt and unrelated_to_a(prompt) == []


def test_no_offers_keeps_the_legacy_prompt(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch)
    client.config.offers = []
    writer = Writer(Brain(state), state, client.config)
    run(writer.run())
    assert {c.offer_key for c in run(state.get_campaigns_by_status("draft"))} == {""}
    row = next(r for r in run(state.get_outbox()) if r["prospect_id"] == ids["a"])
    prompt = generation_prompt(state, row["generation_id"])
    assert "OFFER BRIEF" not in prompt and "PRODUCT_DESC" in prompt and "PRODUCT_KNOWLEDGE" in prompt
    assert client.get("/api/outbox").json()["pending"][0]["offer"] is None


def test_aggregate_evidence_only_at_threshold(client):
    state = client.state
    run(seed(state))
    config = client.config
    decision = run(route_prospect(state, config, run(state.get_prospect_by_email("owner@a.example.com"))))
    brief = run(build_brief(state, config, decision, [1], [decision.context]))
    # Three companies were checked for TEST_SIGNAL_A? Only one: below min_sample 3.
    assert brief.evidence == "" and "Aggregate evidence" not in brief.render()

    async def more():
        for n, value in enumerate((1, 0)):
            cid = await state.add_company(Company(name=f"Example Shop {n}", domain=f"s{n}.example.com"))
            await state.add_observation("TEST_SIGNAL_A", company_id=cid, value_num=value)
    run(more())
    brief = run(build_brief(state, config, decision, [1], [decision.context]))
    assert brief.evidence.startswith("Of 3 businesses Mercury checked, 2 (67%) carried TEST_SIGNAL_A")
    assert brief.evidence in brief.render()


def test_confirmed_pain_hook_feeds_the_brief(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch)
    writer = Writer(Brain(state), state, client.config)
    calls = []

    async def pains(prospect, offer_key, step):
        calls.append((prospect.id, offer_key, step))
        return ConfirmedPain(code="pain_a", words="MARKER_PAIN_WORDS", scene="MARKER_PAIN_SCENE")
    writer.pain_source = pains
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "MARKER_PAIN_WORDS" in prompt and "MARKER_PAIN_SCENE" in prompt
    assert calls == [(ids["a"], "offer_a", 1)]
    writer.pain_source = None
    prompt, _ = run(writer.build_personal_prompt(run(state.get_prospect(ids["a"]))))
    assert "Confirmed pain: none supplied" in prompt and "MARKER_PAIN" not in prompt


# ── Case-study scope ──

def test_out_of_scope_case_study_draft_is_discarded(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch, personal={"subject": "a note", "body": "Like garage beta did. A question?"})
    writer = Writer(Brain(state), state, client.config)
    prospect = run(state.get_prospect(ids["a"]))
    assert run(writer._write_personal_email(prospect)) is None
    prompt, _ = run(writer.build_personal_prompt(prospect))
    assert "Example Garage Beta" not in prompt and "Example Bakery Alpha" in prompt


def test_gate_blocks_named_out_of_scope_case_study():
    result = pre_send_check("owner@a.example.com", "a note", "Example Garage Beta did this. Question?",
                            blocked_references=["Example Garage Beta"])
    assert not result and "case study outside its allowed scope" in result.reasons[0]
    assert pre_send_check("owner@a.example.com", "a note", "A question?",
                          blocked_references=["Example Garage Beta"])


def test_sender_blocks_out_of_scope_case_study(client):
    from datetime import datetime, timezone

    from mercury.agents.sender import Sender
    from tests.test_outbox_native import Env, FakeProvider

    state = client.state
    ids = run(seed(state))
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    for tag, offer, body in (("a", "offer_a", "Example Garage Beta saw this. A question?"),
                             ("b", "offer_b", "Example Bakery Alpha saw this. A question?")):
        run(state.add_outbox_item(prospect_id=ids[tag], to_email=f"owner@{tag}.example.com",
                                  subject="a note", body=body, send_at=now, status="approved",
                                  campaign_id=f"c-{tag}", step=1, offer_key=offer))
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    sender.send_pacing = False
    run(sender._drain_due())
    failed = {r["prospect_id"]: r for r in run(state.get_outbox(status="failed"))}
    # offer_a in market_a: Garage Beta is scoped to market_b.
    assert "case study outside its allowed scope" in failed[ids["a"]]["error"]
    # offer_b's email names offer_a's case study: not offer_b's to cite.
    assert "Example Bakery Alpha" in failed[ids["b"]]["error"]
    assert sender.provider.sent == []


def test_cli_route_explains_the_choice(client, monkeypatch, capsys):
    import sys

    from mercury import cli

    state = client.state
    run(seed(state))
    monkeypatch.setattr("mercury.state.DB_PATH", dash.DB_PATH)
    monkeypatch.setattr(sys, "argv", ["mercury", "offers", "route", "owner@a.example.com", "--json"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["offer_key"] == "offer_a" and out["market"] == "market_a"
    assert [c["offer_key"] for c in out["checks"]] == ["offer_a"]
    monkeypatch.setattr(sys, "argv", ["mercury", "offers"])
    cli.main()
    listing = capsys.readouterr().out
    assert "offer_a" in listing and "[default]" in listing and "TEST_SIGNAL_X" in listing
