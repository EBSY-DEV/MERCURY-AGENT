"""The experiments API and CLI (issue #6): every number the Experiments
screen shows comes from these JSON shapes, and the CLI drives the same
service. Real SQLite databases, synthetic prospects."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury import experiments as ex
from mercury.state import StateManager
from tests.test_experiments import add_prospects, config, definition


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "_demo_config", lambda: config())
        sm = StateManager(db_path=str(db))
        asyncio.run(sm.init_db())
        with TestClient(dash.app) as c:
            c.sm = sm
            yield c


def test_api_create_preview_start_pause_hold_complete(client):
    asyncio.run(add_prospects(client.sm, 6))
    form = definition(allocation_a=50)
    draft_preview = client.post("/api/experiments/preview", json={"definition": form}).json()
    assert draft_preview["eligible"] == 6 and draft_preview["expected"] == {"A": 3, "B": 3}
    assert "Open with a question" in draft_preview["arms"][0]["voice_block"]

    created = client.post("/api/experiments", json=form).json()
    assert created["success"]
    exp = created["experiment"]
    assert exp["status_label"] == "Draft" and exp["controls"]["can_start"]
    assert exp["setup_line"] == ("Opening angle · 50/50 split · 14-day response window · "
                                 "3 mature per arm · Positive-reply rate")
    exp_id = exp["id"]
    preview = client.get(f"/api/experiments/{exp_id}/preview").json()
    assert sum(preview["expected"].values()) == 6
    assert {s["arm"] for s in preview["sample"]} <= {"A", "B"}

    assert client.post(f"/api/experiments/{exp_id}/pause").status_code == 409
    started_ = client.post(f"/api/experiments/{exp_id}/start").json()
    assert started_["experiment"]["status_label"] == "Running"
    assert client.post(f"/api/experiments/{exp_id}/pause").json()["experiment"]["status"] == "paused"
    assert client.post(f"/api/experiments/{exp_id}/resume").json()["experiment"]["status"] == "running"
    held = client.post(f"/api/experiments/{exp_id}/hold", json={"reason": "review"}).json()
    assert held["experiment"]["hold_mail"] and held["experiment"]["hold_reason"] == "review"
    assert client.post(f"/api/experiments/{exp_id}/release").json()["experiment"]["hold_mail"] is False
    refused = client.post(f"/api/experiments/{exp_id}/complete", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "confirmation_required"
    done = client.post(f"/api/experiments/{exp_id}/complete", json={"confirm": True}).json()
    assert done["experiment"]["status"] == "completed"

    listing = client.get("/api/experiments").json()
    row = listing["experiments"][0]
    assert row["result"] == {"code": "insufficient_data", "label": "Not enough data yet"}
    assert set(row) >= {"enrolled", "mature", "rate", "status_label", "variable_label"}
    assert {o["key"] for o in listing["options"]["variables"]} == set(ex.VARIABLES)
    detail = client.get(f"/api/experiments/{exp_id}").json()
    assert set(detail["results"]) >= {"arms", "comparison", "decision", "health", "definitions"}
    assert detail["results"]["comparison"]["method"].startswith("Newcombe")
    assert client.get(f"/api/experiments/{exp_id}?revision=9").status_code == 404
    assert client.get("/api/experiments/nope").status_code == 404


def test_api_rejects_a_bad_definition(client):
    bad = client.post("/api/experiments", json=definition(variable="tone"))
    assert bad.status_code == 400 and bad.json()["code"] == "invalid"
    same = client.post("/api/experiments", json=definition(
        arms=[{"instruction": "same"}, {"instruction": "same"}]))
    assert same.status_code == 400
    extra = client.post("/api/experiments", json=definition(colour="red"))
    assert extra.status_code == 400


def test_api_edits_in_place_or_as_a_new_revision(client):
    exp_id = client.post("/api/experiments", json=definition()).json()["experiment"]["id"]
    draft = client.patch(f"/api/experiments/{exp_id}", json={"min_per_arm": 40}).json()
    assert draft["experiment"]["revision"] == 1 and draft["experiment"]["min_per_arm"] == 40
    version = draft["experiment"]["version"]
    client.post(f"/api/experiments/{exp_id}/start")
    stale = client.patch(f"/api/experiments/{exp_id}",
                         json={"hypothesis": "x", "expected_version": version})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    # Only arm B changes; arm A keeps its instruction.
    changed = client.patch(f"/api/experiments/{exp_id}", json={
        "arms": [{"key": "B", "instruction": "Open with a two-line customer story."}]}).json()
    assert changed["new_revision"] and changed["experiment"]["revision"] == 2
    arms = {a["arm_key"]: a for a in changed["experiment"]["arms"]}
    assert arms["A"]["instruction"] == "Open with a question about their week."
    assert arms["B"]["instruction"] == "Open with a two-line customer story."
    assert arms["B"]["persona_version_id"] == "workspace-v1"
    assert [r["number"] for r in changed["experiment"]["revisions"]] == [1, 2]
    first = client.get(f"/api/experiments/{exp_id}?revision=1").json()
    assert first["experiment"]["arms"][1]["instruction"] == "Open with an observation from their site."
    unknown = client.patch(f"/api/experiments/{exp_id}", json={"status": "completed"})
    assert unknown.status_code == 400


def test_api_assignments_results_and_outcome_labels(client):
    exp_id = client.post("/api/experiments", json=definition()).json()["experiment"]["id"]
    client.post(f"/api/experiments/{exp_id}/start")
    pids = asyncio.run(add_prospects(client.sm, 4))
    prospects = [asyncio.run(client.sm.get_prospect(p)) for p in pids]
    asyncio.run(ex.enroll(client.sm, config(), prospects))
    listing = client.get(f"/api/experiments/{exp_id}/assignments?limit=2").json()
    assert listing["total"] == 4 and len(listing["assignments"]) == 2
    assert {a["arm_key"] for a in listing["assignments"]} <= {"A", "B"}
    results = client.get(f"/api/experiments/{exp_id}/results").json()
    assert sum(a["enrolled"] for a in results["arms"]) == 4
    assert sum(a["awaiting_first_touch"] for a in results["arms"]) == 4
    assert results["decision"]["code"] == "insufficient_data"
    assert results["decision"]["earliest_decision_at"] is None  # nothing sent yet

    row, _ = asyncio.run(client.sm.record_inbound(provider="fake", mailbox="sam@example.com",
                                                  external_id="m1", from_email=prospects[0].email,
                                                  body="Who handles this at your end?"))
    labelled = client.post(f"/api/experiments/outcomes/{row['id']}", json={"label": "neutral_question"})
    assert labelled.json()["outcome"]["source"] == "manual"
    assert client.post(f"/api/experiments/outcomes/{row['id']}", json={"label": "maybe"}).status_code == 400
    assert client.post("/api/experiments/outcomes/missing", json={"label": "other"}).status_code == 404


def test_api_preview_warns_about_overlap_and_empty_cohorts(client):
    first = client.post("/api/experiments", json=definition(name="First")).json()["experiment"]["id"]
    client.post(f"/api/experiments/{first}/start")
    asyncio.run(add_prospects(client.sm, 3))
    overlap = client.post("/api/experiments/preview", json=definition(name="Second")).json()
    assert [w["code"] for w in overlap["warnings"]] == ["overlap"]
    empty = client.post("/api/experiments/preview",
                        json=definition(cohort={"industries": ["aviation"]})).json()
    assert empty["eligible"] == 0 and [w["code"] for w in empty["warnings"]] == ["no_eligible"]


# ── CLI ──

def _cli(monkeypatch, capsys, db_path, *argv):
    import mercury.cli as cli
    import mercury.config as config_mod
    import mercury.state as state_mod

    monkeypatch.setattr(state_mod, "DB_PATH", db_path)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config())
    monkeypatch.setattr(sys, "argv", ["mercury", *argv])
    cli.main()
    return capsys.readouterr().out


def test_cli_create_preview_start_hold_and_complete(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        asyncio.run(StateManager(str(db)).init_db())
        asyncio.run(add_prospects(StateManager(str(db)), 4))
        spec = Path(tmp) / "test.yaml"
        spec.write_text(
            "name: Subject line test\n"
            "variable: subject_line\n"
            "arms:\n"
            "  - {name: variant A, instruction: Use a two-word subject.}\n"
            "  - {name: variant B, instruction: Use a question as the subject.}\n"
            "response_window_days: 10\n"
            "min_per_arm: 20\n")
        out = _cli(monkeypatch, capsys, db, "experiments", "preview", "--file", str(spec))
        assert "4 eligible now" in out and "Use a two-word subject." in out
        out = _cli(monkeypatch, capsys, db, "experiments", "create", "--file", str(spec),
                   "--split", "60", "--json")
        exp_id = json.loads(out)["experiment"]["id"]
        out = _cli(monkeypatch, capsys, db, "experiments", "start", "Subject line test")
        assert "[Running]" in out and "Subject line · 60/40 split" in out
        out = _cli(monkeypatch, capsys, db, "experiments", "hold", exp_id, "--reason", "review")
        assert "Stops every unsent email" in out and "unsent mail on hold" in out
        with pytest.raises(SystemExit):
            _cli(monkeypatch, capsys, db, "experiments", "complete", exp_id)
        assert "Confirm" in capsys.readouterr().out
        out = _cli(monkeypatch, capsys, db, "experiments", "complete", exp_id, "--confirm")
        assert "[Completed]" in out and "Not enough data yet" in out
        out = _cli(monkeypatch, capsys, db, "experiments")
        assert "Subject line test" in out and "Completed" in out
        with pytest.raises(SystemExit):
            _cli(monkeypatch, capsys, db, "experiments", "edit", exp_id, "--b-name", "variant B2")
        assert "cannot be edited" in capsys.readouterr().out


def test_api_listing_offers_personas_and_eligible_groups_for_the_form(client):
    asyncio.run(add_prospects(client.sm, 3, industry="segment_a"))
    asyncio.run(add_prospects(client.sm, 2, prefix="q", industry="segment_b"))
    options = client.get("/api/experiments").json()["options"]
    assert options["personas"][0] == {"id": "", "name": "Default voice", "revision": None}
    assert all(p["id"] and p["name"] for p in options["personas"][1:])
    cohorts = {c["key"]: c for c in options["cohorts"]}
    assert cohorts["all"]["cohort"] == {} and cohorts["all"]["label"].startswith("every new prospect")
    assert cohorts["industry:segment_a"]["count"] == 3
    assert cohorts["industry:segment_b"]["cohort"] == {"industries": ["segment_b"]}
    # Each offered group is a definition the create call accepts as is.
    created = client.post("/api/experiments", json=definition(
        cohort=cohorts["industry:segment_a"]["cohort"])).json()
    assert created["success"] and created["experiment"]["cohort"] == {"industries": ["segment_a"]}


def test_api_results_carry_the_series_and_weeks_the_charts_draw(client):
    created = client.post("/api/experiments", json=definition()).json()
    results = client.get(f"/api/experiments/{created['experiment']['id']}/results").json()
    assert results["series"] == [] and results["by_week"] == []


def test_api_detail_names_each_arms_pinned_persona_and_the_health_limits(client):
    created = client.post("/api/experiments", json=definition()).json()
    exp_id = created["experiment"]["id"]
    assert [a["persona_revision"] for a in created["experiment"]["arms"]] == [None, None]  # not frozen yet
    client.post(f"/api/experiments/{exp_id}/start")
    detail = client.get(f"/api/experiments/{exp_id}").json()
    assert [a["persona_revision"] for a in detail["experiment"]["arms"]] == [1, 1]
    assert {a["persona_name"] for a in detail["experiment"]["arms"]} == {"Workspace voice"}
    limits = detail["results"]["health"]["limits"]
    assert limits["low_reply_rate"] == 0.01 and limits["high_bounce_rate"] == 0.05
    assert limits["sending_stops_at"] == 0.9    # the test config's channels.email.max_bounce_rate
