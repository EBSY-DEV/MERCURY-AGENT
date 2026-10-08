"""Dashboard side of out-of-office pauses: list them, correct a return date,
resume, and see them recorded in the activity log."""

import asyncio
from datetime import datetime, timedelta, timezone


import mercury.dashboard as dash
from mercury.models.prospect import Prospect

from tests.test_dashboard_pipeline import client  # noqa: F401  (fixture)


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _paused(c, email="jane@acme.com", state="needs_review", days=None, **kw):
    sm = c.sm
    pid = _run(sm.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme", email=email,
        email_status="verified", status="contacted",
    )))
    _run(sm.add_outbox_item(
        prospect_id=pid, to_email=email, subject="follow up", body="Body. Question?",
        send_at=(_now() + timedelta(days=1)).isoformat(), status="approved",
        campaign_id="c1", step=2,
    ))
    _run(sm.record_ooo_pause(
        pid, message_id="<m1>", message_at=_now(), state=state,
        resume_at=(_now() + timedelta(days=days)) if days is not None else None,
        return_text=kw.get("return_text", ""), review_reason=kw.get("review_reason", "none"),
        confidence=kw.get("confidence", 0.0),
    ))
    return pid


async def _rows(c, kind):
    async with c.sm._connect() as db:
        async with db.execute(
            "SELECT agent, details_json FROM actions WHERE action_type = ?", (kind,)
        ) as cur:
            return await cur.fetchall()


def test_pauses_listed_with_review_first_and_queue_counts(client):
    dated = _paused(client, "a@acme.com", state="paused", days=9, return_text="October 20",
                    confidence=0.95)
    review = _paused(client, "b@acme.com", state="needs_review", review_reason="ambiguous")
    data = client.get("/api/pauses").json()
    assert [p["prospect_id"] for p in data["pauses"]] == [review, dated]
    first, second = data["pauses"]
    assert first["state"] == "needs_review" and first["review_reason"] == "ambiguous"
    assert first["resume_at"] == "" and first["resume_local"] == ""
    assert second["return_text"] == "October 20" and second["resume_local"]
    assert second["queued_count"] == 1 and second["email"] == "a@acme.com"
    assert data["timezone"]


def test_ended_pauses_are_not_listed(client):
    pid = _paused(client)
    _run(client.sm.resume_pause(pid, "operator"))
    assert client.get("/api/pauses").json()["pauses"] == []


def test_set_return_date_resumes_on_that_day_and_is_logged(client):
    pid = _paused(client)
    day = (_now() + timedelta(days=14)).date().isoformat()
    res = client.post(f"/api/pauses/{pid}/return-date", json={"return_date": day})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] and body["state"] == "paused"
    pause = _run(client.sm.get_pause(pid))
    assert pause["state"] == "paused" and pause["manual_override"] == 1
    assert pause["resume_at"] == body["resume_at"]
    rows = _run(_rows(client, "pause_date_changed"))
    assert len(rows) == 1 and rows[0][0] == "dashboard"
    # The queued email kept its approval and is still held.
    items = _run(client.sm.get_outbox(status="approved"))
    assert len(items) == 1
    assert _run(client.sm.get_outbox(status="approved", exclude_paused=True)) == []
    # The list now shows the date for editing.
    shown = client.get("/api/pauses").json()["pauses"][0]
    assert shown["state"] == "paused" and shown["manual_override"] is True
    assert shown["resume_local"]


def test_return_date_is_validated(client):
    pid = _paused(client)
    assert client.post(f"/api/pauses/{pid}/return-date", json={"return_date": "soon"}).status_code == 400
    assert client.post(f"/api/pauses/{pid}/return-date", json={}).status_code == 400
    old = (_now() - timedelta(days=3)).date().isoformat()
    past = client.post(f"/api/pauses/{pid}/return-date", json={"return_date": old})
    assert past.status_code == 400 and "passed" in past.json()["error"]
    far = (_now() + timedelta(days=900)).date().isoformat()
    assert client.post(f"/api/pauses/{pid}/return-date", json={"return_date": far}).status_code == 400
    assert client.post("/api/pauses/nobody/return-date",
                       json={"return_date": (_now() + timedelta(days=3)).date().isoformat()}).status_code == 404
    assert _run(client.sm.get_pause(pid))["state"] == "needs_review"   # nothing changed


def test_resume_now_releases_the_contact_and_logs_it(client):
    pid = _paused(client, state="paused", days=20, return_text="Nov 1")
    res = client.post(f"/api/pauses/{pid}/resume")
    assert res.status_code == 200 and res.json()["success"]
    assert _run(client.sm.get_pause(pid))["state"] == "resumed"
    assert client.get("/api/pauses").json()["pauses"] == []
    rows = _run(_rows(client, "sequence_resumed"))
    assert len(rows) == 1 and rows[0][0] == "dashboard"
    # The held follow-up is released and due again, status untouched.
    item = _run(client.sm.get_outbox(status="approved", exclude_paused=True))[0]
    assert item["status"] == "approved"
    # Nothing left to resume.
    assert client.post(f"/api/pauses/{pid}/resume").status_code == 404


def test_activity_feed_names_the_new_events(client):
    pid = _paused(client)
    client.post(f"/api/pauses/{pid}/resume")
    kinds = {a["action_type"] for a in client.get("/api/activity").json()}
    assert "sequence_resumed" in kinds
    assert "sequence_resumed" in (dash.WEB_DIR / "app.js").read_text()
    assert "pause_date_changed" in (dash.WEB_DIR / "app.js").read_text()


def test_today_flags_pauses_that_need_a_return_date(client):
    quiet = client.get("/api/today").json()
    assert not any(i["key"] == "away-review" for i in quiet["items"])
    _paused(client, "a@acme.com", state="needs_review")
    _paused(client, "b@acme.com", state="paused", days=5)
    items = client.get("/api/today").json()["items"]
    item = next(i for i in items if i["key"] == "away-review")
    assert item["title"].startswith("1 contact is away")
    assert item["tab"] == "outbox"
