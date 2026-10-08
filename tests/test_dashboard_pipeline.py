"""Pipeline board, manual moves, outbox calendar, and reschedule APIs.

The board is derived, not stored: a prospect's column comes from its status
plus its latest conversation stage. Humans own the right half of the board
(replied / meeting / won / lost); Mercury owns the left half.
"""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.models.company import Company
from mercury.models.conversation import Conversation
from mercury.models.prospect import Prospect
from mercury.state import StateManager


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def client(monkeypatch):
    """A dashboard bound to a throwaway database."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "mercury" / "web")
        sm = StateManager(db_path=str(db))
        _run(sm.init_db())
        with TestClient(dash.app) as c:
            c.db_path = db
            c.sm = sm
            yield c


def _prospect(sm, email, status="new", score=50, company_id="", first="Pat", last="Lee"):
    return _run(sm.add_prospect(Prospect(
        first_name=first, last_name=last, title="Owner", email=email,
        email_status="verified", status=status, score=score, company_id=company_id,
    )))


def _convo(sm, pid, stage="engaged", status="open"):
    return _run(sm.add_conversation(Conversation(
        id="", prospect_id=pid, stage=stage, status=status,
    )))


def _outbox(sm, pid, step=1, status="approved", send_at=None, kind="sequence",
            campaign_id="c1", to_email="x@y.co", sent_at=None):
    send_at = send_at or (_now() + timedelta(days=1)).isoformat()
    item = _run(sm.add_outbox_item(
        prospect_id=pid, to_email=to_email, subject=f"s{step}", body="b",
        send_at=send_at, status=status, campaign_id=campaign_id, step=step, kind=kind,
    ))
    if sent_at:
        _run(sm.update_outbox_item(item, sent_at=sent_at))
    return item


def _columns(client):
    data = client.get("/api/pipeline").json()
    return {c["key"]: c for c in data["columns"]}, data


def _column_of(client, pid):
    cols, _ = _columns(client)
    for key, col in cols.items():
        if any(card["id"] == pid for card in col["items"]):
            return key
    return None


# ── Board shape + derivation ──


def test_pipeline_shape_and_locks(client):
    cols, data = _columns(client)
    assert [c["key"] for c in data["columns"]] == [
        "new", "queued", "contacted", "replied", "meeting", "won", "lost"]
    for c in data["columns"]:
        assert c["hint"] and c["label"]
        assert c["locked"] == (c["key"] in ("new", "queued", "contacted"))
        assert c["count"] == 0 and c["items"] == []


def test_column_derivation(client):
    sm = client.sm
    co = _run(sm.add_company(Company(name="Peak Roofing", domain="peakroofing.com")))
    ids = {
        "new": _prospect(sm, "a@x.co", "new", company_id=co),
        "queued": _prospect(sm, "b@x.co", "queued"),
        "contacted": _prospect(sm, "c@x.co", "contacted"),
        "replied": _prospect(sm, "d@x.co", "replied"),
        "convo_only": _prospect(sm, "e@x.co", "contacted"),
        "meeting": _prospect(sm, "f@x.co", "meeting"),
        "won_status": _prospect(sm, "g@x.co", "closed"),
        "won_stage": _prospect(sm, "h@x.co", "replied"),
        "lost": _prospect(sm, "i@x.co", "lost"),
        "opted_out": _prospect(sm, "j@x.co", "opted_out"),
        "lost_stage": _prospect(sm, "k@x.co", "meeting"),
    }
    _convo(sm, ids["convo_only"], stage="engaged")
    _convo(sm, ids["won_stage"], stage="closed_won")
    _convo(sm, ids["lost_stage"], stage="closed_lost")

    expected = {
        "new": "new", "queued": "queued", "contacted": "contacted",
        "replied": "replied", "convo_only": "replied", "meeting": "meeting",
        "won_status": "won", "won_stage": "won", "lost": "lost",
        "opted_out": "lost", "lost_stage": "lost",
    }
    for name, col in expected.items():
        assert _column_of(client, ids[name]) == col, name

    cols, _ = _columns(client)
    assert cols["lost"]["count"] == 3
    card = next(c for c in cols["new"]["items"] if c["id"] == ids["new"])
    assert card["company"] == "Peak Roofing"
    assert card["name"] == "Pat Lee"
    conv_card = next(c for c in cols["replied"]["items"] if c["id"] == ids["convo_only"])
    assert conv_card["stage"] == "engaged" and conv_card["conversation_id"]


def test_card_outbox_rollups(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "contacted")
    soon = (_now() + timedelta(days=2)).replace(microsecond=0).isoformat()
    later = (_now() + timedelta(days=5)).replace(microsecond=0).isoformat()
    sent_at = (_now() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    _outbox(sm, pid, step=1, status="sent", send_at=sent_at, sent_at=sent_at)
    _outbox(sm, pid, step=2, status="approved", send_at=soon)
    _outbox(sm, pid, step=3, status="pending_review", send_at=later)
    _outbox(sm, pid, step=4, status="cancelled", send_at=(_now() + timedelta(hours=1)).isoformat())

    cols, _ = _columns(client)
    card = cols["contacted"]["items"][0]
    assert card["sent_count"] == 1
    assert card["pending_count"] == 2
    assert card["next_send_at"] == soon
    assert card["last_activity"]


def test_cards_sorted_by_activity_then_score(client):
    sm = client.sm
    a = _prospect(sm, "a@x.co", "new", score=10)
    b = _prospect(sm, "b@x.co", "new", score=90)
    c = _prospect(sm, "c@x.co", "new", score=50)
    _run(sm.update_prospect_status(c, "new"))  # most recent activity
    order = [card["id"] for card in _columns(client)[0]["new"]["items"]]
    assert order[0] == c
    assert set(order) == {a, b, c}


def test_name_falls_back_to_email(client):
    pid = _prospect(client.sm, "noname@x.co", "new", first="", last="")
    card = _columns(client)[0]["new"]["items"][0]
    assert card["id"] == pid and card["name"] == "noname@x.co"


# ── Moves ──


def test_move_to_meeting_cancels_and_advances_stage(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "replied")
    cid = _convo(sm, pid, stage="engaged")
    _outbox(sm, pid, step=2, status="approved")
    _outbox(sm, pid, step=3, status="pending_review")
    _outbox(sm, pid, step=1, status="sent")

    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "meeting"}).json()
    assert r == {"success": True, "column": "meeting", "cancelled": 2}
    assert _run(sm.get_prospect(pid)).status == "meeting"
    assert _run(sm.get_conversation(cid)).stage == "closing"
    cancelled = _run(sm.get_outbox(status="cancelled"))
    assert {o["error"] for o in cancelled} == {"moved_to_meeting"}
    assert _column_of(client, pid) == "meeting"


def test_move_to_won(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "meeting")
    cid = _convo(sm, pid, stage="closing")
    _outbox(sm, pid, step=2, status="approved")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "won"}).json()
    assert r["success"] and r["column"] == "won" and r["cancelled"] == 1
    assert _run(sm.get_prospect(pid)).status == "closed"
    convo = _run(sm.get_conversation(cid))
    assert convo.stage == "closed_won" and convo.status == "closed"
    assert _column_of(client, pid) == "won"


def test_move_to_lost_without_conversation(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "contacted")
    _outbox(sm, pid, step=2, status="approved")
    _outbox(sm, pid, step=3, status="approved")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "lost"}).json()
    assert r == {"success": True, "column": "lost", "cancelled": 2}
    assert _run(sm.get_prospect(pid)).status == "lost"
    assert _column_of(client, pid) == "lost"


def test_move_lost_back_to_replied_reopens_conversation(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "lost")
    cid = _convo(sm, pid, stage="closed_lost", status="closed")
    _outbox(sm, pid, step=2, status="approved")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "replied"}).json()
    assert r == {"success": True, "column": "replied", "cancelled": 0}
    assert _run(sm.get_prospect(pid)).status == "replied"
    assert _run(sm.get_conversation(cid)).stage == "engaged"
    assert _run(sm.get_outbox(status="approved"))  # replied does not cancel
    assert _column_of(client, pid) == "replied"


def test_move_won_back_to_meeting_lands_in_meeting(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "closed")
    cid = _convo(sm, pid, stage="closed_won", status="closed")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "meeting"}).json()
    assert r["success"]
    assert _run(sm.get_conversation(cid)).stage == "closing"
    assert _column_of(client, pid) == "meeting"


def test_move_logs_action(client):
    pid = _prospect(client.sm, "a@x.co", "replied")
    client.post(f"/api/pipeline/{pid}/move", json={"column": "won"})
    rows = _run(_actions(client.db_path))
    assert ("pipeline_move", "dashboard") in rows


async def _actions(db_path):
    import aiosqlite
    async with aiosqlite.connect(str(db_path)) as db:
        async with db.execute("SELECT action_type, agent FROM actions") as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.parametrize("column", ["new", "queued", "contacted"])
def test_move_to_locked_column_rejected(client, column):
    pid = _prospect(client.sm, "a@x.co", "replied")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": column})
    assert r.status_code == 400
    assert r.json()["success"] is False and r.json()["error"]
    assert _run(client.sm.get_prospect(pid)).status == "replied"


def test_move_unknown_column_rejected(client):
    pid = _prospect(client.sm, "a@x.co", "replied")
    r = client.post(f"/api/pipeline/{pid}/move", json={"column": "bogus"})
    assert r.status_code == 400 and r.json()["success"] is False


def test_move_unknown_prospect_404(client):
    r = client.post("/api/pipeline/nope/move", json={"column": "won"})
    assert r.status_code == 404 and r.json()["success"] is False


# ── Calendar ──


def test_calendar_range_uses_sent_at_for_sent(client):
    sm = client.sm
    co = _run(sm.add_company(Company(name="Front Range HVAC", domain="frhvac.com")))
    pid = _prospect(sm, "a@x.co", "contacted", company_id=co)
    # Sent: scheduled Oct 1 but actually went out Oct 3 → shows on Oct 3.
    sent = _outbox(sm, pid, step=1, status="sent", send_at="2026-10-01T09:00:00",
                   sent_at="2026-10-03T10:00:00")
    sched = _outbox(sm, pid, step=2, status="approved", send_at="2026-10-05T09:30:00")
    cancelled = _outbox(sm, pid, step=3, status="cancelled", send_at="2026-10-04T08:00:00")
    _run(sm.update_outbox_item(cancelled, error="moved_to_lost"))
    _outbox(sm, pid, step=4, status="approved", send_at="2026-10-10T00:00:00")  # end-exclusive
    _outbox(sm, pid, step=5, status="approved", send_at="2026-09-30 23:59:59")  # before start

    r = client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-10-10"})
    assert r.status_code == 200
    data = r.json()
    assert data["start"] == "2026-10-01" and data["end"] == "2026-10-10"
    ids = [e["id"] for e in data["items"]]
    assert ids == [sent, cancelled, sched]  # ordered by event time
    first = data["items"][0]
    assert first["at"] == "2026-10-03T10:00:00"
    assert first["company"] == "Front Range HVAC" and first["name"] == "Pat Lee"
    assert data["items"][1]["error"] == "moved_to_lost"
    assert set(first) >= {"id", "at", "kind", "step", "label", "status", "subject",
                          "to_email", "prospect_id", "name", "company",
                          "campaign_id", "error", "body"}

    # Window Oct 1-2 excludes the sent email (its time is sent_at, Oct 3).
    r = client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-10-03"})
    assert r.json()["items"] == []


def test_calendar_labels(client):
    sm = client.sm
    pid = _prospect(sm, "a@x.co", "replied")
    _outbox(sm, pid, step=1, send_at="2026-11-02T09:00:00")
    _outbox(sm, pid, step=3, send_at="2026-11-03T09:00:00")
    _outbox(sm, pid, step=1, kind="reply", campaign_id="", send_at="2026-11-04T09:00:00",
            to_email="")
    items = client.get("/api/calendar",
                       params={"start": "2026-11-01", "end": "2026-11-30"}).json()["items"]
    assert [i["label"] for i in items] == ["Email 1", "Follow-up 2", "Reply"]
    assert items[2]["kind"] == "reply" and items[2]["name"] == "Pat Lee"


@pytest.mark.parametrize("params", [
    {},
    {"start": "2026-10-01"},
    {"start": "2026/10/01", "end": "2026-10-05"},
    {"start": "2026-10-01", "end": "2026-13-01"},
    {"start": "2026-10-05", "end": "2026-10-01"},
    {"start": "2026-10-01", "end": "2026-12-15"},  # > 62 days
])
def test_calendar_validation(client, params):
    assert client.get("/api/calendar", params=params).status_code == 400


def test_calendar_max_span_ok(client):
    r = client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-12-02"})
    assert r.status_code == 200  # exactly 62 days


# ── Reschedule ──


def test_reschedule_happy_path(client):
    pid = _prospect(client.sm, "a@x.co", "queued")
    item = _outbox(client.sm, pid, step=2, status="pending_review")
    target = (_now() + timedelta(days=3)).replace(second=0, microsecond=0)
    r = client.post(f"/api/outbox/{item}/reschedule",
                    json={"send_at": target.strftime("%Y-%m-%dT%H:%M"), "revision": 1})
    assert r.status_code == 200
    assert r.json() == {"success": True, "send_at": target.isoformat(timespec="seconds"),
                        "revision": 2, "status": "pending_review", "approval_cleared": False}
    assert _run(client.sm.get_outbox_item(item))["send_at"] == target.isoformat(timespec="seconds")


def test_reschedule_converts_offsets_to_utc(client):
    pid = _prospect(client.sm, "a@x.co", "queued")
    item = _outbox(client.sm, pid, step=2, status="approved")
    year = _now().year + 1
    r = client.post(f"/api/outbox/{item}/reschedule",
                    json={"send_at": f"{year}-03-01T09:00:00-07:00", "revision": 1})
    assert r.json()["send_at"] == f"{year}-03-01T16:00:00"
    r = client.post(f"/api/outbox/{item}/reschedule",
                    json={"send_at": f"{year}-03-01T09:00:00.123Z", "revision": 2})
    assert r.json()["send_at"] == f"{year}-03-01T09:00:00"


def test_reschedule_rejects_past(client):
    pid = _prospect(client.sm, "a@x.co", "queued")
    item = _outbox(client.sm, pid, step=2, status="approved")
    past = (_now() - timedelta(hours=2)).isoformat()
    r = client.post(f"/api/outbox/{item}/reschedule", json={"send_at": past, "revision": 1})
    assert r.status_code == 400 and r.json()["success"] is False


def test_reschedule_rejects_bad_input(client):
    pid = _prospect(client.sm, "a@x.co", "queued")
    item = _outbox(client.sm, pid, step=2, status="approved")
    for body in ({}, {"send_at": "tomorrow", "revision": 1}, {"send_at": "", "revision": 1}):
        r = client.post(f"/api/outbox/{item}/reschedule", json=body)
        assert r.status_code == 400 and r.json()["success"] is False


@pytest.mark.parametrize("status", ["sent", "cancelled", "rejected", "failed"])
def test_reschedule_wrong_status(client, status):
    pid = _prospect(client.sm, "a@x.co", "contacted")
    item = _outbox(client.sm, pid, step=1, status=status)
    future = (_now() + timedelta(days=1)).isoformat()
    r = client.post(f"/api/outbox/{item}/reschedule", json={"send_at": future, "revision": 1})
    assert r.status_code == 400 and r.json()["success"] is False
    assert r.json()["code"] == "not_editable"


def test_reschedule_needs_the_revision_on_screen(client):
    pid = _prospect(client.sm, "a@x.co", "queued")
    item = _outbox(client.sm, pid, step=2, status="pending_review")
    future = (_now() + timedelta(days=1)).isoformat()
    r = client.post(f"/api/outbox/{item}/reschedule", json={"send_at": future})
    assert r.status_code == 400 and r.json()["code"] == "revision_required"
    r = client.post(f"/api/outbox/{item}/reschedule", json={"send_at": future, "revision": 7})
    assert r.status_code == 409 and r.json()["code"] == "stale_revision" and r.json()["revision"] == 1
    assert _run(client.sm.get_outbox_item(item))["revision"] == 1


def test_reschedule_unknown_item(client):
    future = (_now() + timedelta(days=1)).isoformat()
    r = client.post("/api/outbox/nope/reschedule", json={"send_at": future})
    assert r.status_code == 404 and r.json()["success"] is False


# ── Handler guard: a reply never downgrades a human-advanced deal ──


@pytest.mark.parametrize("status", ["meeting", "closed"])
def test_handler_reply_keeps_meeting_and_closed(status):
    from mercury.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, seed_prospect

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            sm = StateManager(str(Path(tmp) / "t.db"))
            await sm.init_db()
            pid = await seed_prospect(sm, status=status)
            await sm.add_outbox_item(
                prospect_id=pid, to_email="jane@acme.com", subject="s2", body="b",
                send_at=_now().isoformat(), status="approved", campaign_id="c1", step=2,
            )
            provider = FakeProvider()
            provider.inbound = [InboundMessage(
                provider_id="in-guard", from_email="jane@acme.com",
                subject="Re: hi", body="Sounds good, see you Thursday.",
            )]
            await make_handler(sm, provider)._run_native()
            prospect = await sm.get_prospect(pid)
            cancelled = await sm.get_outbox(status="cancelled")
            return prospect.status, len(cancelled)

    final_status, n_cancelled = _run(scenario())
    assert final_status == status
    assert n_cancelled == 1  # stop-on-reply still applies


def test_handler_reply_still_marks_contacted_as_replied():
    from mercury.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, seed_prospect

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            sm = StateManager(str(Path(tmp) / "t.db"))
            await sm.init_db()
            pid = await seed_prospect(sm, status="contacted")
            provider = FakeProvider()
            provider.inbound = [InboundMessage(
                provider_id="in-normal", from_email="jane@acme.com",
                subject="Re: hi", body="What does it cost?",
            )]
            await make_handler(sm, provider)._run_native()
            return (await sm.get_prospect(pid)).status

    assert _run(scenario()) == "replied"
