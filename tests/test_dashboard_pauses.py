"""Dashboard flows for out-of-office pauses: list, correct the date, resume,
and how the Outbox and Today explain a held follow-up."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytz
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.models.prospect import Prospect
from mercury.state import StateManager


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        sm = StateManager(db_path=str(db))
        _run(sm.init_db())
        with TestClient(dash.app) as c:
            c.sm = sm
            yield c


def _away(sm, email="pat@example.com"):
    """A contact with an approved follow-up, paused by a reply with no date."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pid = _run(sm.add_prospect(Prospect(first_name="Pat", last_name="Lee", email=email,
                                        email_status="verified", status="contacted")))
    item = _run(sm.add_outbox_item(
        prospect_id=pid, to_email=email, subject="again", body="b", step=2,
        status="approved", campaign_id="c1", send_at=now.isoformat()))
    outcome, pause = _run(sm.record_auto_reply(
        message_key="<ooo@example.com>", kind="out_of_office", prospect_id=pid,
        received_at=(now - timedelta(hours=1)).isoformat(), excerpt="Out of the office.",
        parsed={"resume_at": None, "review_reason": "no_date"}))
    assert outcome == "paused"
    return pid, item, pause


def _local_today(client) -> str:
    tz = client.get("/api/pauses").json()["timezone"]
    return datetime.now(pytz.timezone(tz)).date().isoformat()


def _activity(client) -> list[str]:
    return [a["action_type"] for a in client.get("/api/activity").json()]


def test_paused_contacts_are_listed_with_their_review_state(client):
    _pid, _item, pause = _away(client.sm)
    data = client.get("/api/pauses").json()
    (row,) = data["pauses"]
    assert row["id"] == pause["id"] and row["prospect_email"] == "pat@example.com"
    assert row["review_state"] == "needs_review" and row["back_on"] is None
    assert row["review_text"] == "The reply gives no return date."
    assert row["queued"] == 1

    detail = client.get(f"/api/pauses/{pause['id']}").json()
    assert [m["kind"] for m in detail["messages"]] == ["out_of_office"]


def test_the_outbox_and_today_say_why_the_follow_up_waits(client):
    _away(client.sm)
    (approved,) = client.get("/api/outbox").json()["approved"]
    assert approved["policy"]["code"] == "ooo_pause"
    assert "no clear return date" in approved["policy"]["reason"]
    keys = [i["key"] for i in client.get("/api/today").json()["items"]]
    assert "away-review" in keys


def test_setting_a_return_date_is_validated_and_logged(client):
    _pid, _item, pause = _away(client.sm)
    url = f"/api/pauses/{pause['id']}/return-date"

    r = client.post(url, json={"date": "20-10-2026"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid"
    yesterday = (datetime.fromisoformat(_local_today(client)) - timedelta(days=1)).date()
    r = client.post(url, json={"date": yesterday.isoformat()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "past"

    day = (datetime.fromisoformat(_local_today(client)) + timedelta(days=3)).date().isoformat()
    r = client.post(url, json={"date": day, "note": "assistant confirmed"})
    assert r.status_code == 200
    assert r.json()["back_on"] == day and r.json()["review_state"] == "scheduled"
    assert r.json()["manual_override"] == 1
    assert "ooo_return_date_set" in _activity(client)
    (approved,) = client.get("/api/outbox").json()["approved"]
    assert approved["status"] == "approved"                 # approval untouched


def test_resuming_reschedules_without_approving_and_is_logged(client):
    _pid, item, pause = _away(client.sm)
    r = client.post(f"/api/pauses/{pause['id']}/resume", json={"note": "back early"})
    assert r.status_code == 200 and r.json()["status"] == "resumed"
    assert client.get("/api/pauses").json()["pauses"] == []
    (ended,) = client.get("/api/pauses", params={"ended": True}).json()["pauses"]
    assert ended["ended_reason"] == "back early" and ended["ended_by"] == "dashboard"
    assert "ooo_resumed" in _activity(client)
    assert _run(client.sm.get_outbox_item(item))["status"] == "approved"

    again = client.post(f"/api/pauses/{pause['id']}/resume", json={})
    assert again.status_code == 404


def test_cli_lists_sets_a_date_and_resumes(client, monkeypatch, capsys):
    import argparse

    import mercury.state as state_module
    from mercury.cli import cmd_paused

    monkeypatch.setattr(state_module, "DB_PATH", dash.DB_PATH)
    _pid, _item, pause = _away(client.sm)

    def run(action, target="", date="", ended=False):
        cmd_paused(argparse.Namespace(paused_action=action, target=target, date=date,
                                      note="", ended=ended, json=False))
        return capsys.readouterr().out

    assert "pat@example.com" in run("list") and "needs date" in run("list")
    day = (datetime.fromisoformat(_local_today(client)) + timedelta(days=2)).date().isoformat()
    assert day in run("set-date", pause["id"], day)
    assert "Resumed" in run("resume", pause["id"])
    assert "Nobody is paused" in run("list")
