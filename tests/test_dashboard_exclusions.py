"""Dashboard flows for exclusions and company holds: add, search, remove,
import/export, hold, resume, and the reasons the Outbox shows."""

import asyncio
import base64
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.models.company import Company
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


def _queue(sm, email, company_id="", status="approved", step=1):
    pid = _run(sm.add_prospect(Prospect(first_name="Pat", email=email, email_status="verified",
                                        company_id=company_id)))
    return _run(sm.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b", step=step, status=status,
        campaign_id="c-" + email, company_id=company_id,
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))


def test_add_search_and_remove_a_rule(client):
    r = client.post("/api/exclusions", json={"kind": "domain", "value": "www.Acme.com",
                                             "reason": "existing customer",
                                             "include_subdomains": True})
    assert r.status_code == 200
    rule = r.json()
    assert rule["value"] == "acme.com" and rule["include_subdomains"] == 1
    assert rule["description"] == "acme.com and its subdomains (excluded by you)"

    found = client.get("/api/exclusions", params={"q": "acme"}).json()
    assert [x["id"] for x in found["rules"]] == [rule["id"]]
    assert client.get("/api/exclusions", params={"q": "zzz"}).json()["rules"] == []
    assert found["policy"]["daily_window"].startswith("rolling 24 hours")

    assert client.get("/api/exclusions/check", params={"email": "a@eu.acme.com"}).json()["excluded"]
    r = client.post(f"/api/exclusions/{rule['id']}/remove", json={"note": "not a customer"})
    assert r.status_code == 200
    assert client.get("/api/exclusions").json()["rules"] == []
    removed = client.get("/api/exclusions", params={"removed": True}).json()["rules"]
    assert removed[0]["removed_note"] == "not a customer"
    events = client.get(f"/api/exclusions/{rule['id']}").json()["events"]
    assert [(e["action"], e["actor"]) for e in events] == [("added", "dashboard"),
                                                          ("removed", "dashboard")]


def test_invalid_input_is_a_422_with_a_code(client):
    r = client.post("/api/exclusions", json={"kind": "email", "value": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid"


def test_opt_out_removal_needs_confirmation(client):
    rule, _ = _run(client.sm.add_suppression("email", "jane@acme.com", source="opt_out"))
    r = client.post(f"/api/exclusions/{rule['id']}/remove", json={"note": "cleanup"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "protected"
    r = client.post(f"/api/exclusions/{rule['id']}/remove",
                    json={"note": "she asked to hear from us", "confirm_opt_out": True})
    assert r.status_code == 200


def test_import_and_export(client):
    data = base64.b64encode(b"email\njane@acme.com\nbad\n").decode()
    r = client.post("/api/exclusions/import", json={"content_b64": data, "reason": "old list"})
    assert r.json()["added"] == 1 and r.json()["invalid_count"] == 1
    csv = client.get("/api/exclusions/export.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "jane@acme.com" in csv.text and "old list" in csv.text


def test_outbox_explains_blocked_and_held_mail(client):
    company = _run(client.sm.add_company(Company(name="Acme", domain="acme.com")))
    blocked_id = _queue(client.sm, "jane@acme.com", company)
    held_id = _queue(client.sm, "bob@acme.com", company)
    client.post("/api/exclusions", json={"kind": "email", "value": "jane@acme.com"})
    client.post("/api/company-holds", json={"company_id": company, "note": "in talks"})

    outbox = client.get("/api/outbox").json()
    (blocked,) = outbox["blocked"]
    assert blocked["id"] == blocked_id and blocked["policy"]["code"] == "excluded"
    assert blocked["policy"]["tone"] == "bad" and blocked["policy"]["action"]
    (held,) = outbox["approved"]
    assert held["id"] == held_id and held["policy"]["code"] == "company_hold"
    assert "in talks" in held["policy"]["reason"]

    today = {i["key"] for i in client.get("/api/today").json()["items"]}
    assert {"blocked", "company-holds"} <= today

    # Requeue is refused while the rule stands, and lands in review after.
    r = client.post(f"/api/outbox/{blocked_id}/requeue")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "still_excluded"
    rule_id = client.get("/api/exclusions").json()["rules"][0]["id"]
    client.post(f"/api/exclusions/{rule_id}/remove", json={"note": "mistake"})
    assert client.post(f"/api/outbox/{blocked_id}/requeue").json()["status"] == "pending_review"
    assert _run(client.sm.get_outbox_item(blocked_id))["status"] == "pending_review"

    (hold,) = client.get("/api/company-holds").json()["holds"]
    assert hold["company_name"] == "Acme" and hold["queued"] == 2
    r = client.post(f"/api/company-holds/{hold['id']}/release", json={"note": "done"})
    assert r.status_code == 200
    assert client.get("/api/company-holds").json()["holds"] == []
    approved = client.get("/api/outbox").json()["approved"]
    assert approved[0]["policy"]["code"] == "ok"


def test_hold_for_an_unknown_company_is_404(client):
    r = client.post("/api/company-holds", json={"company_id": "nope"})
    assert r.status_code == 404
