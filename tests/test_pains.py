"""The governed pain library (#59): Mercury proposes, a person decides, and
only confirmed pains are written from.

Every pain here is invented and labelled: codes are PAIN_TEST_*, the markets
are segment_a / segment_b, the trades are fictional, URLs are example.com.
"""

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import mercury.dashboard as dash
import mercury.trainer as trainer_module
from mercury import cli
from mercury.control.context import OperatorContext
from mercury.control.errors import Conflict, Invalid, NotFound
from mercury.control.pains import PainService
from mercury.gate import pre_send_check
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.pains import (
    PainSelection, choose_pain, derive_code, find_rejected_pain_hits, never_use_block,
    pain_block, pain_prompt_blocks, propose_pains, same_pain, select_pain,
    select_pain_for_prospect,
)
from mercury.state import StateManager

HUMAN = "cli:local"

# Invented statements. Fictional trade: "lantern repair".
LANTERN_A = "Customers drop off a broken lantern and never hear back about the repair quote"
LANTERN_A_REWORDED = "Broken lantern customers drop off never hear back about a repair quote"
WICK_B = "Wick suppliers deliver late so the workshop sits idle on busy mornings"
OTHER_C = "Invoices for glassware orders stay unpaid for months"


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    return sm


async def seed(state, code, status="confirmed", **fields):
    fields.setdefault("label", f"{code} label")
    await state.add_pain(code, status=status,
                         status_by=HUMAN if status != "proposed" else "", **fields)
    return await state.get_pain(code)


# ── The table ──


@pytest.mark.asyncio
async def test_a_decision_needs_a_person(state):
    """The database itself refuses a confirmed pain nobody decided."""
    with pytest.raises(sqlite3.IntegrityError):
        await state.add_pain("PAIN_TEST_A", label="x", status="confirmed", status_by="")
    with pytest.raises(sqlite3.IntegrityError):
        await state.add_pain("PAIN_TEST_B", label="x", status="confirmed", status_by="trainer")
    await state.add_pain("PAIN_TEST_C", label="x")  # proposed needs no one
    with pytest.raises(sqlite3.IntegrityError):
        await state.set_pain_status("PAIN_TEST_C", "confirmed", actor="trainer")
    assert await state.set_pain_status("PAIN_TEST_C", "confirmed", actor=HUMAN)
    pain = await state.get_pain("PAIN_TEST_C")
    assert (pain["status"], pain["status_by"]) == ("confirmed", HUMAN) and pain["status_at"]


@pytest.mark.asyncio
async def test_add_pain_never_overwrites_an_existing_one(state):
    await seed(state, "PAIN_TEST_A", status="rejected", label="first")
    assert not await state.add_pain("PAIN_TEST_A", label="second")
    pain = await state.get_pain("PAIN_TEST_A")
    assert (pain["label"], pain["status"]) == ("first", "rejected")


@pytest.mark.asyncio
async def test_outbox_records_the_pain_it_used(state):
    item = await state.add_outbox_item(
        prospect_id="p1", to_email="a@example.com", subject="s", body="b",
        send_at="2030-01-01T00:00:00", pain_code="pain_test_a")
    assert (await state.get_outbox_item(item))["pain_code"] == "PAIN_TEST_A"
    plain = await state.add_outbox_item(
        prospect_id="p2", to_email="b@example.com", subject="s", body="b",
        send_at="2030-01-01T00:00:00")
    assert (await state.get_outbox_item(plain))["pain_code"] == ""


@pytest.mark.asyncio
async def test_pain_counters_count_sends_and_replies(state):
    await seed(state, "PAIN_TEST_A")
    await seed(state, "PAIN_TEST_B")
    ids = {}
    for name, pain in (("p1", "PAIN_TEST_A"), ("p2", "PAIN_TEST_A"), ("p3", "PAIN_TEST_B")):
        ids[name] = await state.add_outbox_item(
            prospect_id=name, to_email=f"{name}@example.com", subject="s", body="b",
            send_at="2030-01-01T00:00:00", pain_code=pain)
        await state.update_outbox_item(ids[name], status="sent", sent_at="2030-01-01T10:00:00")
    # A second email to p1 on the same pain: another send, not another person.
    again = await state.add_outbox_item(
        prospect_id="p1", to_email="p1@example.com", subject="s2", body="b",
        send_at="2030-01-02T00:00:00", step=2, pain_code="PAIN_TEST_A")
    await state.update_outbox_item(again, status="sent", sent_at="2030-01-02T10:00:00")
    async with state._connect() as db:
        await db.execute(
            "INSERT INTO actions (id, agent, action_type, details_json, created_at) "
            "VALUES ('a1', 'handler', 'reply_received', ?, '2030-01-03 09:00:00')",
            (json.dumps({"prospect_id": "p1", "intent": "interested"}),))
        await db.execute(
            "INSERT INTO actions (id, agent, action_type, details_json, created_at) "
            "VALUES ('a2', 'handler', 'reply_received', ?, '2030-01-03 09:00:00')",
            (json.dumps({"prospect_id": "p3", "intent": "ooo"}),))
        await db.commit()
    stats = await state.pain_stats()
    assert stats["PAIN_TEST_A"] == {"sends": 3, "prospects": 2, "replies": 1, "positive": 1}
    # An out-of-office notice is not a reply.
    assert stats["PAIN_TEST_B"] == {"sends": 1, "prospects": 1, "replies": 0, "positive": 0}


# ── The trainer proposes ──


def test_same_pain_rule():
    assert same_pain(LANTERN_A, LANTERN_A.upper() + "!")
    assert same_pain(LANTERN_A, LANTERN_A_REWORDED)
    assert not same_pain(LANTERN_A, WICK_B)
    assert not same_pain(LANTERN_A, OTHER_C)
    assert derive_code(LANTERN_A) == derive_code(LANTERN_A.lower() + " ")
    assert derive_code(LANTERN_A) != derive_code(WICK_B)


@pytest.mark.asyncio
async def test_proposals_are_proposed_never_confirmed(state):
    results = await propose_pains(state, [LANTERN_A, WICK_B, {"label": OTHER_C, "sector": "glassware"}],
                                  evidence="https://example.com/research")
    assert [r["outcome"] for r in results] == ["proposed"] * 3
    pains = await state.list_pains()
    assert {p["status"] for p in pains} == {"proposed"}
    assert all(p["status_by"] == "" and p["source"] == "trainer" for p in pains)
    assert pains[0]["evidence"] == ["https://example.com/research"]


@pytest.mark.asyncio
async def test_rerunning_the_trainer_changes_nothing(state):
    await propose_pains(state, [LANTERN_A, WICK_B])
    await state.set_pain_status(derive_code(LANTERN_A), "confirmed", HUMAN)
    await state.update_pain(derive_code(LANTERN_A), {"scene": "edited by a person"})
    again = await propose_pains(state, [LANTERN_A, WICK_B])
    assert [r["outcome"] for r in again] == ["exists", "exists"]
    pains = {p["code"]: p for p in await state.list_pains()}
    assert len(pains) == 2
    assert pains[derive_code(LANTERN_A)]["status"] == "confirmed"
    assert pains[derive_code(LANTERN_A)]["scene"] == "edited by a person"
    assert pains[derive_code(WICK_B)]["status"] == "proposed"  # not silently confirmed


@pytest.mark.asyncio
async def test_a_rejected_pain_is_not_resurrected(state):
    await propose_pains(state, [LANTERN_A])
    code = derive_code(LANTERN_A)
    await state.set_pain_status(code, "rejected", HUMAN, "not real")
    for text in (LANTERN_A, LANTERN_A_REWORDED):
        (result,) = await propose_pains(state, [text])
        assert result["outcome"] == "rejected" and result["matched"] == code
    pains = await state.list_pains()
    assert [(p["code"], p["status"]) for p in pains] == [(code, "rejected")]


@pytest.mark.asyncio
async def test_a_rejected_pain_matches_by_code_even_if_the_text_was_edited(state):
    await seed(state, derive_code(WICK_B), status="rejected", label="something else entirely")
    (result,) = await propose_pains(state, [WICK_B])
    assert result["outcome"] == "rejected"
    assert len(await state.list_pains()) == 1


@pytest.mark.asyncio
async def test_a_reworded_copy_of_the_original_wording_is_still_recognised_after_an_edit(state):
    """The first wording is kept, so editing the label doesn't make the
    trainer's next copy of it look new."""
    await propose_pains(state, [LANTERN_A])
    code = derive_code(LANTERN_A)
    await state.update_pain(code, {"label": "Reworded by a person: nobody calls back"})
    await state.set_pain_status(code, "rejected", HUMAN)
    (result,) = await propose_pains(state, [LANTERN_A_REWORDED])
    assert result["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_unusable_statements_are_skipped(state):
    assert await propose_pains(state, ["", "  ", "ok", None, 5]) == []


@pytest.mark.asyncio
async def test_the_trainer_proposes_and_writes_only_confirmed_pains(tmp_path, monkeypatch):
    """End to end through Trainer.train with the crawl and the model stubbed:
    two runs, a rejection in between."""
    sm = StateManager(str(tmp_path / "t.db"))
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(trainer_module, "PROJECT_ROOT", tmp_path)

    class Crawl:
        async def crawl(self, *args, **kwargs):
            return {"https://example.com/": "Lantern repair, in segment_a."}

    monkeypatch.setattr(trainer_module, "FallbackCrawler", Crawl)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    t = trainer_module.Trainer.__new__(trainer_module.Trainer)
    t.state, t.brain, t.scraped_pages = sm, None, {}

    async def product():
        return {"product_name": "Offer A", "company_name": "Example Co"}

    async def icp():
        return {"industries": ["lantern repair"], "titles": ["Owner"],
                "pain_points": [LANTERN_A, WICK_B]}

    async def intel(_):
        return {"competitors": []}

    async def objections(*_):
        return {}

    t._extract_product_info, t._extract_icp = product, icp
    t._extract_competitive_intel, t._generate_objections = intel, objections

    await t.train("https://example.com", output_path="out.yaml")
    knowledge = (tmp_path / "skills" / "product_knowledge.md").read_text()
    assert "lantern" not in knowledge.lower() and "Review proposals" in knowledge
    assert {p["status"] for p in await sm.list_pains()} == {"proposed"}

    await sm.set_pain_status(derive_code(LANTERN_A), "confirmed", HUMAN)
    await sm.set_pain_status(derive_code(WICK_B), "rejected", HUMAN)
    await t.train("https://example.com", output_path="out.yaml")
    knowledge = (tmp_path / "skills" / "product_knowledge.md").read_text()
    assert "lantern" in knowledge.lower() and "wick" not in knowledge.lower()
    status = {p["code"]: p["status"] for p in await sm.list_pains()}
    assert status == {derive_code(LANTERN_A): "confirmed", derive_code(WICK_B): "rejected"}


# ── Selection ──


CONFIRMED = [
    {"code": "PAIN_TEST_GENERIC", "status": "confirmed", "market": "", "sector": "",
     "offer_key": "", "signal_codes": []},
    {"code": "PAIN_TEST_LANTERN", "status": "confirmed", "market": "", "sector": "lantern repair",
     "offer_key": "", "signal_codes": []},
    {"code": "PAIN_TEST_SIGNAL", "status": "confirmed", "market": "", "sector": "",
     "offer_key": "", "signal_codes": ["TEST_SIGNAL_A", "TEST_SIGNAL_B"]},
    {"code": "PAIN_TEST_OFFER_B", "status": "confirmed", "market": "", "sector": "",
     "offer_key": "offer_b", "signal_codes": []},
    {"code": "PAIN_TEST_SEG_B", "status": "confirmed", "market": "segment_b", "sector": "",
     "offer_key": "", "signal_codes": []},
    {"code": "PAIN_TEST_PROPOSED", "status": "proposed", "market": "", "sector": "lantern repair",
     "offer_key": "", "signal_codes": ["TEST_SIGNAL_A"]},
]


def pick(**kw):
    pain, reason = choose_pain(CONFIRMED, **kw)
    return pain["code"] if pain else None, reason


def test_selection_only_considers_confirmed_pains():
    code, _ = pick(sector="lantern repair", signal_codes=["TEST_SIGNAL_A"])
    assert code != "PAIN_TEST_PROPOSED"
    assert choose_pain([CONFIRMED[-1]], sector="lantern repair")[0] is None


def test_selection_matches_signals_sector_market_and_offer():
    assert pick(signal_codes=["TEST_SIGNAL_B"])[0] == "PAIN_TEST_SIGNAL"
    assert pick(sector="Lantern Repair Shops")[0] == "PAIN_TEST_LANTERN"
    assert pick(offer_key="offer_b")[0] == "PAIN_TEST_OFFER_B"
    assert pick(market="segment_b")[0] == "PAIN_TEST_SEG_B"
    # The offer-bound pain never goes into an email for another offer, or none.
    assert pick(offer_key="offer_a")[0] == "PAIN_TEST_GENERIC"
    assert pick()[0] == "PAIN_TEST_GENERIC"
    # A market-bound pain never leaks into another market.
    assert pick(market="segment_a")[0] == "PAIN_TEST_GENERIC"


def test_selection_prefers_the_most_specific_and_is_deterministic():
    # Two signals matched beat one named sector.
    code, reason = pick(sector="lantern repair", signal_codes=["TEST_SIGNAL_A", "TEST_SIGNAL_B"])
    assert code == "PAIN_TEST_SIGNAL" and "TEST_SIGNAL_A, TEST_SIGNAL_B" in reason
    runs = {pick(sector="lantern repair", offer_key="offer_b")[0] for _ in range(5)}
    assert len(runs) == 1
    shuffled = list(reversed(CONFIRMED))
    assert choose_pain(shuffled, sector="lantern repair")[0]["code"] == "PAIN_TEST_LANTERN"


def test_no_fitting_pain_gives_none_with_a_reason():
    only_signal = [CONFIRMED[2]]
    pain, reason = choose_pain(only_signal, signal_codes=["OTHER"])
    assert pain is None and "no confirmed pain" in reason


@pytest.mark.asyncio
async def test_select_pain_returns_the_never_use_list(state):
    await seed(state, "PAIN_TEST_OK", label="ok pain", sector="lantern repair")
    await seed(state, "PAIN_TEST_NO", status="rejected", label="rejected pain")
    await seed(state, "PAIN_TEST_MAYBE", status="proposed", label="maybe pain")
    chosen = await select_pain(state, sector="lantern repair")
    assert chosen.code == "PAIN_TEST_OK"
    assert [p["code"] for p in chosen.rejected] == ["PAIN_TEST_NO"]
    none = await select_pain(state, sector="glassware", offer_key="offer_a")
    assert none.pain is None and none.code == "" and len(none.rejected) == 1


@pytest.mark.asyncio
async def test_selection_for_a_prospect_reads_confirmed_signals(state):
    class Cfg:
        class icp:
            class _M:
                name = "Segment_A"
                places = ["Exampleville"]
            markets = [_M()]

    for code, status in (("TEST_SIGNAL_A", "confirmed"), ("TEST_SIGNAL_B", "proposed")):
        await state.upsert_signal_code(code, status=status)
    company_id = await state.add_company(Company(
        name="Example Lanterns", domain="example.com", industry="Lantern repair",
        location="Exampleville"))
    await state.add_observation("TEST_SIGNAL_A", company_id=company_id, value_num=1)
    await state.add_observation("TEST_SIGNAL_B", company_id=company_id, value_num=1)
    prospect = Prospect(first_name="Pat", email="pat@example.com", company_id=company_id)
    await seed(state, "PAIN_TEST_PLAIN", label="plain")
    await seed(state, "PAIN_TEST_A", label="a", signal_codes=["TEST_SIGNAL_A"], market="segment_a")
    await seed(state, "PAIN_TEST_B", label="b", signal_codes=["TEST_SIGNAL_B"])
    chosen = await select_pain_for_prospect(state, prospect, config=Cfg)
    # TEST_SIGNAL_B is only proposed, so it does not count as a signal the company has.
    assert chosen.code == "PAIN_TEST_A"
    assert "TEST_SIGNAL_A" in chosen.reason and "segment_a" in chosen.reason


def test_prompt_blocks():
    pain = {"code": "PAIN_TEST_A", "owner_words": "Nobody calls me back", "label": "l",
            "scene": "Closing time, quote unsent.", "cost": "Lost repairs."}
    rejected = [{"code": "PAIN_TEST_R", "owner_words": "", "label": "Wick delivery is late"}]
    text = pain_prompt_blocks(PainSelection(pain, "why", tuple(rejected)))
    assert "only pain you may raise" in text and "Nobody calls me back" in text
    assert "Closing time" in text and "Lost repairs" in text
    assert "NEVER raise these" in text and "Wick delivery is late" in text
    empty = pain_prompt_blocks(PainSelection(None, "why"))
    assert "none confirmed" in empty and "NEVER" not in empty
    assert never_use_block([]) == "" and "none confirmed" in pain_block(PainSelection(None, ""))


# ── The guard ──

REJECTED = {"code": "PAIN_TEST_R", "label": "Wick suppliers deliver late",
            "owner_words": "my wick supplier always shows up late and the workshop sits idle",
            "scene": "", "avoid_terms": []}


def test_guard_catches_the_distinctive_words_of_a_rejected_pain():
    draft = "Hi Pat, when your wick supplier shows up late, does the workshop sit idle?"
    (hit,) = find_rejected_pain_hits(draft, [REJECTED])
    assert hit.code == "PAIN_TEST_R" and "wick" in hit.words


def test_guard_leaves_unrelated_drafts_alone():
    assert find_rejected_pain_hits("Hi Pat, saw the lantern workshop on Main St. Worth a chat?",
                                   [REJECTED]) == []
    # One shared everyday word is not enough.
    assert find_rejected_pain_hits("Your workshop looks great.", [REJECTED]) == []


def test_guard_honours_avoid_terms_and_allowed_vocabulary():
    rejected = {**REJECTED, "avoid_terms": ["candle melt"]}
    assert find_rejected_pain_hits("Do you worry about candle melt?", [rejected])
    allowed = {"code": "PAIN_TEST_OK", "label": "Wick suppliers", "owner_words": "", "scene": ""}
    draft = "Hi Pat, when your wick supplier shows up late, does the workshop sit idle?"
    # The confirmed pain shares "wick"/"supplier"; the rest still trips the guard
    # only if two distinctive words remain.
    hits = find_rejected_pain_hits(draft, [REJECTED], allowed=[allowed])
    assert all("wick" not in h.words for h in hits)


def test_the_send_gate_blocks_a_rejected_pain():
    body = "Hi Pat, when your wick supplier shows up late, does the workshop sit idle?"
    assert pre_send_check("pat@example.com", "quick question", body).ok
    result = pre_send_check("pat@example.com", "quick question", body, rejected_pains=[REJECTED])
    assert not result.ok and any("PAIN_TEST_R" in r for r in result.reasons)
    clean = pre_send_check("pat@example.com", "quick question", "Saw your lantern shop. Open to a chat?",
                           rejected_pains=[REJECTED])
    assert clean.ok


# ── The service, CLI and API ──


@pytest_asyncio.fixture
async def service(state):
    return await PainService(state, OperatorContext.local("cli")).ready()


@pytest.mark.asyncio
async def test_add_edit_and_confirm_round_trip(service, state):
    await state.upsert_signal_code("TEST_SIGNAL_A")
    pain = await service.add({
        "code": "pain_test_a", "label": "Quotes never get sent", "owner_words": "I never send quotes",
        "scene": "End of day.\nStill unsent.", "cost": "Lost jobs", "sector": "lantern repair",
        "market": "Segment_A", "signal_codes": ["test_signal_a"], "offer_key": "Offer_A",
        "evidence": ["https://example.com/notes"]})
    assert pain["code"] == "PAIN_TEST_A" and pain["status"] == "proposed"
    assert (pain["market"], pain["offer_key"], pain["signal_codes"]) == ("segment_a", "offer_a", ["TEST_SIGNAL_A"])
    assert pain["stats"]["sends"] == 0 and "origin_text" not in pain

    edited = await service.edit("PAIN_TEST_A", {"cost": "Lost repair jobs"}, expected_revision=1)
    assert edited["cost"] == "Lost repair jobs" and edited["revision"] == 2
    assert edited["status"] == "proposed" and edited["label"] == "Quotes never get sent"
    with pytest.raises(Conflict) as stale:
        await service.edit("PAIN_TEST_A", {"cost": "x"}, expected_revision=1)
    assert stale.value.code == "stale_revision"

    result = await service.set_status(["pain_test_a", "PAIN_NOPE"], "confirmed", "checked")
    assert (result["changed"], result["unknown"]) == (1, ["PAIN_NOPE"])
    got = await service.get("PAIN_TEST_A")
    assert (got["status"], got["status_by"], got["status_note"]) == ("confirmed", "cli:local", "checked")
    summary = (await service.list())["summary"]
    assert summary == {"proposed": 0, "confirmed": 1, "rejected": 0, "total": 1}
    assert [p["code"] for p in (await service.list(status="confirmed", market="segment_a"))["pains"]] == ["PAIN_TEST_A"]
    assert (await service.list(status="confirmed", market=""))["pains"] == []


@pytest.mark.asyncio
async def test_service_refusals(service, state):
    with pytest.raises(Invalid):
        await service.add({"label": ""})
    with pytest.raises(Invalid):
        await service.add({"label": "x pain", "scene": "one\ntwo\nthree"})
    with pytest.raises(Invalid) as bad:
        await service.add({"label": "Needs a signal", "signal_codes": ["NOT_A_SIGNAL"]})
    assert bad.value.code == "unknown_signal"
    with pytest.raises(Invalid):
        await service.add({"label": "Bad code here", "code": "x"})
    await service.add({"label": "Quotes never get sent", "code": "PAIN_TEST_A"})
    with pytest.raises(Conflict) as dup:
        await service.add({"label": "Something else", "code": "PAIN_TEST_A"})
    assert dup.value.code == "duplicate"
    with pytest.raises(NotFound):
        await service.edit("PAIN_NOPE", {"cost": "x"})
    with pytest.raises(Invalid):
        await service.edit("PAIN_TEST_A", {"code": "PAIN_OTHER", "cost": "x"})
    with pytest.raises(Invalid):
        await service.set_status(["PAIN_TEST_A"], "maybe")


@pytest.mark.asyncio
async def test_adding_a_copy_of_a_rejected_pain_is_refused(service):
    await service.add({"label": LANTERN_A, "code": "PAIN_TEST_A"})
    await service.set_status(["PAIN_TEST_A"], "rejected")
    with pytest.raises(Conflict) as err:
        await service.add({"label": LANTERN_A_REWORDED})
    assert err.value.code == "matches_rejected" and err.value.details["matched"] == "PAIN_TEST_A"
    # A person can still change their mind about the original.
    await service.set_status(["PAIN_TEST_A"], "confirmed")


@pytest.mark.asyncio
async def test_pain_commands_are_audited(service, state):
    await service.add({"label": "Quotes never get sent", "code": "PAIN_TEST_A"})
    await service.set_status(["PAIN_TEST_A"], "rejected", "no evidence")
    async with state._connect() as db:
        async with db.execute("SELECT action, object_type, object_id, operator, client, outcome "
                              "FROM audit_log ORDER BY id") as cur:
            rows = await cur.fetchall()
    assert rows == [("pains.add", "pain", "PAIN_TEST_A", "local", "cli", "ok"),
                    ("pains.status", "pain", "PAIN_TEST_A", "local", "cli", "ok")]


def cli_args(**kw):
    base = dict(confirm="", reject="", reopen="", note="", status="", market=None, json=False,
                pain_action=None, label=None, words=None, scene=None, cost=None, sector=None,
                offer=None, signal=None, evidence=None, avoid=None, confirmed=False,
                expected_revision=None, code="")
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def cli_state(tmp_path, monkeypatch):
    path = str(tmp_path / "cli.db")
    import mercury.state as state_module
    monkeypatch.setattr(state_module, "DB_PATH", Path(path))
    return StateManager(path)


def test_cli_round_trip(cli_state, capsys):
    cli.cmd_pains(cli_args(pain_action="add", label=LANTERN_A, code="PAIN_TEST_A",
                           sector="lantern repair"))
    assert "Added PAIN_TEST_A as proposed" in capsys.readouterr().out
    cli.cmd_pains(cli_args())
    out = capsys.readouterr().out
    assert "0 confirmed, 1 awaiting you" in out and "PAIN_TEST_A" in out and "[ ? ]" in out

    cli.cmd_pains(cli_args(confirm="pain_test_a,PAIN_NOPE", note="seen it"))
    out = capsys.readouterr().out
    assert "1 pain(s) confirmed" in out and "PAIN_NOPE" in out
    cli.cmd_pains(cli_args(status="confirmed"))
    assert "[on]" in capsys.readouterr().out
    cli.cmd_pains(cli_args(pain_action="show", code="PAIN_TEST_A"))
    out = capsys.readouterr().out
    assert "cli:local" in out and "seen it" in out and "lantern repair" in out

    cli.cmd_pains(cli_args(pain_action="edit", code="PAIN_TEST_A", cost="Lost jobs"))
    assert "Saved PAIN_TEST_A (rev" in capsys.readouterr().out
    cli.cmd_pains(cli_args(reject="PAIN_TEST_A"))
    assert "1 pain(s) rejected" in capsys.readouterr().out
    cli.cmd_pains(cli_args(confirm="all"))  # nothing proposed: nothing to do
    assert "0 pain(s) confirmed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.cmd_pains(cli_args(pain_action="edit", code="PAIN_TEST_A"))


def test_cli_parser_wires_the_command(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_pains", seen.append)
    for argv in (["pains", "--confirm", "PAIN_TEST_A", "--note", "ok"],
                 ["pains", "add", "--label", "x pain", "--signal", "A", "--signal", "B"],
                 ["pains", "show", "PAIN_TEST_A"],
                 ["pains", "edit", "PAIN_TEST_A", "--cost", "c", "--expected-revision", "2"]):
        monkeypatch.setattr(sys, "argv", ["mercury", *argv])
        cli.main()
    confirm, add, show, edit = seen
    assert confirm.confirm == "PAIN_TEST_A" and confirm.note == "ok" and confirm.pain_action is None
    assert add.pain_action == "add" and add.signal == ["A", "B"] and add.confirmed is False
    assert (show.pain_action, show.code) == ("show", "PAIN_TEST_A")
    assert (edit.cost, edit.expected_revision) == ("c", 2)


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(dash, "DB_PATH", Path(tmp) / "mercury.db")
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "mercury" / "web")
        with TestClient(dash.app) as c:
            yield c


def test_api_round_trip(client):
    empty = client.get("/api/pains").json()
    assert empty["pains"] == [] and empty["summary"]["total"] == 0
    assert any(s["code"] == "SERP_RANK" for s in empty["signals"])

    made = client.post("/api/pains", json={
        "code": "PAIN_TEST_A", "label": LANTERN_A, "owner_words": "Nobody calls me back",
        "scene": "End of day.", "cost": "Lost repairs", "sector": "lantern repair",
        "market": "segment_a", "signal_codes": ["INCUMBENT_AGENCY"], "offer_key": "offer_a",
        "evidence": ["https://example.com/n"]}).json()
    assert made["success"] and made["pain"]["status"] == "proposed"
    assert set(made["pain"]) >= {"code", "label", "market", "sector", "owner_words", "scene", "cost",
                                 "signal_codes", "offer_key", "evidence", "avoid_terms", "status",
                                 "status_by", "status_at", "status_note", "revision", "stats"}

    dup = client.post("/api/pains", json={"code": "PAIN_TEST_A", "label": "another pain here"})
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate"

    saved = client.post("/api/pains/PAIN_TEST_A/save",
                        json={"cost": "Lost repair jobs", "expected_revision": 1}).json()
    assert saved["success"] and saved["pain"]["revision"] == 2
    stale = client.post("/api/pains/PAIN_TEST_A/save", json={"cost": "x", "expected_revision": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert client.post("/api/pains/PAIN_NOPE/save", json={"cost": "x"}).status_code == 404

    r = client.post("/api/pains/status", json={"codes": ["PAIN_TEST_A", "PAIN_NOPE"],
                                               "status": "confirmed", "note": "ok"}).json()
    assert r["success"] and r["changed"] == 1 and r["unknown"] == ["PAIN_NOPE"]
    one = client.get("/api/pains/PAIN_TEST_A").json()
    assert one["status"] == "confirmed" and one["status_by"] == "dashboard:local"

    r = client.post("/api/pains/status", json={"code": "PAIN_TEST_A", "status": "rejected"}).json()
    assert r["changed"] == 1
    listed = client.get("/api/pains?status=rejected").json()
    assert [p["code"] for p in listed["pains"]] == ["PAIN_TEST_A"]
    assert listed["summary"] == {"proposed": 0, "confirmed": 0, "rejected": 1, "total": 1}
    assert client.get("/api/pains?status=confirmed").json()["pains"] == []
    assert client.post("/api/pains/status", json={"code": "PAIN_TEST_A", "status": "bogus"}).status_code == 400
    assert client.get("/api/pains/PAIN_NOPE").status_code == 404


def test_api_has_no_way_to_set_a_status_through_edit_or_create(client):
    assert client.post("/api/pains", json={"label": "a pain here", "status": "confirmed"}).status_code == 422
    client.post("/api/pains", json={"code": "PAIN_TEST_A", "label": "a pain here"})
    assert client.post("/api/pains/PAIN_TEST_A/save", json={"status": "confirmed"}).status_code == 422
    assert client.get("/api/pains/PAIN_TEST_A").json()["status"] == "proposed"


@pytest.mark.asyncio
async def test_the_sender_stops_a_draft_that_raises_a_rejected_pain(state):
    """The never-use list in the prompt is a request; this is the guarantee."""
    from datetime import datetime, timezone

    from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect

    await seed(state, "PAIN_TEST_R", status="rejected", label=REJECTED["label"],
               owner_words=REJECTED["owner_words"])
    now = datetime.now(timezone.utc).isoformat()
    pid = await seed_prospect(state)
    bad = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="quick question",
        body="Jane, when your wick supplier shows up late, does the workshop sit idle?",
        send_at=now, status="approved", campaign_id="c1", step=1)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    item = await state.get_outbox_item(bad)
    assert item["status"] == "failed" and "rejected pain PAIN_TEST_R" in item["error"]
    assert provider.sent == []

    good = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="quick question",
        body="Jane, saw the lantern workshop on Main St. Open to a short chat?",
        send_at=now, status="approved", campaign_id="c2", step=1)
    await make_sender(state, provider)._drain_due()
    assert (await state.get_outbox_item(good))["status"] == "sent"
