"""/api/trends: daily sent / replies / positive / bounces, totals, prior window.

Definitions under test (mercury/metrics.py): sent = sequence outbox rows by
date(sent_at); replies/positive = handler `reply_received` events (legacy
conversations as fallback), once per prospect per day, OOO excluded;
bounces = handler `bounce` events.
"""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.models.conversation import Conversation
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from tests.test_outbox_native import FakeProvider, make_handler  # noqa: F401
from mercury.integrations.mail_provider import InboundMessage


def _run(coro):
    return asyncio.run(coro)


def _today():
    return datetime.now(timezone.utc).date()


def _at(days_ago: int, hour: int = 12, sep: str = "T") -> str:
    d = _today() - timedelta(days=days_ago)
    return f"{d.isoformat()}{sep}{hour:02d}:15:00"


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


_n = [0]


def _prospect(sm, email=None):
    _n[0] += 1
    return _run(sm.add_prospect(Prospect(
        first_name="Pat", last_name="Lee", email=email or f"p{_n[0]}@acme.co",
        email_status="verified", status="contacted",
    )))


def _sent(sm, pid, when, kind="sequence", status="sent", step=1, campaign="c1"):
    item = _run(sm.add_outbox_item(
        prospect_id=pid, to_email="x@acme.co", subject="s", body="b",
        send_at=when, status="approved", campaign_id=campaign, step=step, kind=kind,
    ))
    _run(sm.update_outbox_item(item, status=status, sent_at=when))
    return item


def _reply(sm, pid, when, intent="question"):
    _run(sm.log_action("reply_received", "handler",
                       {"prospect_id": pid, "intent": intent}, created_at=when))


def _bounce(sm, pid, when):
    _run(sm.log_action("bounce", "handler",
                       {"prospect": "x@acme.co", "prospect_id": pid}, created_at=when))


def _by_date(data):
    return {row["date"]: row for row in data["series"]}


@pytest.mark.parametrize("days", [7, 30, 90])
def test_series_zero_filled_and_ends_today(client, days):
    data = client.get(f"/api/trends?days={days}").json()
    assert data["days"] == days
    series = data["series"]
    assert len(series) == days
    assert series[-1]["date"] == _today().isoformat()
    assert series[0]["date"] == (_today() - timedelta(days=days - 1)).isoformat()
    dates = [r["date"] for r in series]
    assert dates == sorted(dates)
    assert all(r["sent"] == r["replies"] == r["positive"] == r["bounces"] == 0 for r in series)


def test_default_is_30_and_bad_days_rejected(client):
    assert client.get("/api/trends").json()["days"] == 30
    for bad in ("14", "abc", "0", "-7"):
        r = client.get(f"/api/trends?days={bad}")
        assert r.status_code == 400
        assert r.json()["success"] is False


def test_rates_null_when_nothing_sent(client):
    pid = _prospect(client.sm)
    _reply(client.sm, pid, _at(1))  # a reply without sends still counts
    data = client.get("/api/trends?days=7").json()
    t = data["totals"]
    assert t["sent"] == 0 and t["replies"] == 1
    assert t["reply_rate"] is None and t["positive_rate"] is None and t["bounce_rate"] is None
    assert data["prior"]["reply_rate"] is None


def test_sent_bucketing_handles_both_timestamp_formats(client):
    sm = client.sm
    pid = _prospect(sm)
    _sent(sm, pid, _at(2, sep="T"), step=1)
    _sent(sm, pid, _at(2, hour=23, sep=" "), step=2)
    _sent(sm, pid, _at(0, hour=0, sep=" "), step=3)
    # Not outreach / not sent → excluded.
    _sent(sm, pid, _at(2), kind="reply", step=1, campaign="")
    _sent(sm, pid, _at(2), status="failed", step=4)
    rows = _by_date(client.get("/api/trends?days=7").json())
    assert rows[(_today() - timedelta(days=2)).isoformat()]["sent"] == 2
    assert rows[_today().isoformat()]["sent"] == 1


def test_replies_positive_bounces_counted_once_per_prospect_per_day(client):
    sm = client.sm
    a, b, c = _prospect(sm), _prospect(sm), _prospect(sm)
    for i in range(10):
        _sent(sm, a, _at(3), step=i + 1, campaign=f"k{i}")
    _reply(sm, a, _at(3, hour=9, sep=" "), "interested")
    _reply(sm, a, _at(3, hour=15, sep="T"), "interested")  # same prospect, same day
    _reply(sm, b, _at(3), "ooo")                             # auto-reply: not a reply
    _reply(sm, c, _at(3), "objection")
    _bounce(sm, b, _at(3, sep=" "))
    _bounce(sm, b, _at(3, hour=18))                          # duplicate bounce notice
    data = client.get("/api/trends?days=7").json()
    day = _by_date(data)[(_today() - timedelta(days=3)).isoformat()]
    assert day == {"date": day["date"], "sent": 10, "replies": 2, "positive": 1, "bounces": 1}
    t = data["totals"]
    assert t["reply_rate"] == pytest.approx(0.2)
    assert t["positive_rate"] == pytest.approx(0.1)
    assert t["bounce_rate"] == pytest.approx(0.1)


def test_legacy_conversations_fall_back_without_double_counting(client):
    sm = client.sm
    legacy, logged = _prospect(sm), _prospect(sm)
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    _run(sm.add_conversation(Conversation(
        id="", prospect_id=legacy, intent="interested", created_at=when, updated_at=when)))
    # This prospect has a reply_received event: its conversation is ignored.
    _run(sm.add_conversation(Conversation(
        id="", prospect_id=logged, intent="interested",
        created_at=when - timedelta(days=2), updated_at=when)))
    _reply(sm, logged, _at(1), "interested")
    data = client.get("/api/trends?days=7").json()
    assert data["totals"]["replies"] == 2
    assert data["totals"]["positive"] == 2
    assert _by_date(data)[(_today() - timedelta(days=1)).isoformat()]["replies"] == 2


def test_prior_window(client):
    sm = client.sm
    pid = _prospect(sm)
    _sent(sm, pid, _at(6), step=1)            # current 7d window (oldest day)
    _sent(sm, pid, _at(7), step=2)            # prior window (newest day)
    _sent(sm, pid, _at(13), step=3)           # prior window (oldest day)
    _sent(sm, pid, _at(14), step=4)           # outside both
    _reply(sm, pid, _at(10))
    data = client.get("/api/trends?days=7").json()
    assert data["totals"]["sent"] == 1
    assert data["prior"]["sent"] == 2
    assert data["prior"]["replies"] == 1
    assert data["prior"]["reply_rate"] == pytest.approx(0.5)
    assert set(data["prior"]) == set(data["totals"])


def test_handler_logs_reply_and_bounce_events(tmp_path):
    async def go():
        sm = StateManager(str(tmp_path / "h.db"))
        await sm.init_db()
        pid = await sm.add_prospect(Prospect(
            first_name="Jane", last_name="Doe", email="jane@acme.com",
            email_status="verified", status="contacted"))
        item = await sm.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=_at(0), status="approved", campaign_id="c1", step=1)
        await sm.update_outbox_item(item, status="sent", message_id="<m1@x>", sent_at=_at(0))
        bad = await sm.add_prospect(Prospect(
            first_name="Bo", last_name="B", email="bo@acme.com",
            email_status="verified", status="contacted"))
        item2 = await sm.add_outbox_item(
            prospect_id=bad, to_email="bo@acme.com", subject="s", body="b",
            send_at=_at(0), status="approved", campaign_id="c1", step=1)
        await sm.update_outbox_item(item2, status="sent", message_id="<m2@x>", sent_at=_at(0))

        provider = FakeProvider()
        provider.inbound = [
            InboundMessage(provider_id="r1", from_email="jane@acme.com",
                           subject="Re: s", body="Sounds good, tell me more"),
            InboundMessage(provider_id="b1", from_email="mailer-daemon@googlemail.com",
                           subject="Delivery Status Notification (Failure)",
                           body="nope", in_reply_to="<m2@x>", is_bounce=True),
        ]
        handler = make_handler(sm, provider, intent="interested")
        await handler._run_native()

        from mercury import metrics
        return await metrics.trends(sm.db_path, 7, _today()), pid, bad

    data, pid, bad = _run(go())
    t = data["totals"]
    assert (t["sent"], t["replies"], t["positive"], t["bounces"]) == (2, 1, 1, 1)
