"""Registry on the contacts API, the review endpoints, the CLI and the Scout hook.

No network: providers are replaced with a SunbizProvider fed by a fake fetch.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
import mercury.registry_api as registry_api
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.signals import seed_signal_catalog
from mercury.state import StateManager
from tests import registry_fixtures as fx
from tests.registry_fixtures import FakeSunbiz, provider

ROOFING = ("EXAMPLE PALM ROOFING, LLC", "L15000000001", "Active")


def run(coro):
    return asyncio.run(coro)


def good_fake():
    return FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})


@pytest.fixture
def env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        import mercury.state as state_mod
        monkeypatch.setattr(state_mod, "DB_PATH", str(db))
        sm = StateManager(db_path=str(db))
        run(sm.init_db())
        run(seed_signal_catalog(sm))
        fake = good_fake()
        monkeypatch.setattr(registry_api, "default_providers", lambda: [provider(fake)])
        import mercury.registry.service as service_mod
        monkeypatch.setattr(service_mod, "default_providers", lambda: [provider(fake)])

        class Env:
            pass
        e = Env()
        e.sm, e.db, e.fake = sm, db, fake
        e.company = _company(sm, "Example Palm Roofing", "Orlando, FL")
        e.inbox = _prospect(sm, e.company, "info@example-palm.example.com",
                            first="Example Palm Roofing", last="Team", source="public_inbox")
        e.person = _prospect(sm, e.company, "pat@example-palm.example.com",
                             first="Pat", last="Lee", source="company_website")
        yield e


def _company(sm, name, location):
    c = Company(name=name, domain=f"{name.lower().replace(' ', '-')}.example.com", location=location)
    return run(sm.add_company(c))


def _prospect(sm, company_id, email, first, last, source):
    return run(sm.add_prospect(Prospect(
        company_id=company_id, first_name=first, last_name=last, email=email,
        title="Owner", source=source, email_status="verified")))


@pytest.fixture
def client(env):
    with TestClient(dash.app) as c:
        yield c


def contacts(client, company_id):
    rows = client.get(f"/api/companies/{company_id}/contacts").json()
    return {r["email"]: r for r in rows}


def test_contacts_carry_registry_status_before_any_lookup(client, env):
    rows = contacts(client, env.company)
    reg = rows["info@example-palm.example.com"]["registry"]
    assert reg["status"] == "not_looked_up" and reg["provider"] == "fl_sunbiz"
    assert reg["provider_label"] == "Florida Division of Corporations"
    assert reg["shared_inbox"] is True and reg["has_own_name"] is False
    assert reg["person"] is None and reg["source_url"] == "" and reg["looked_up_at"] == ""
    assert rows["pat@example-palm.example.com"]["registry"]["has_own_name"] is True
    assert rows["pat@example-palm.example.com"]["registry"]["shared_inbox"] is False
    # The list endpoint carries the same object.
    flat = {r["email"]: r for r in client.get("/api/prospects").json()}
    assert flat["info@example-palm.example.com"]["registry"]["status"] == "not_looked_up"


def test_non_florida_contact_is_not_eligible(client, env):
    other = _company(env.sm, "Mountain Example Roofing", "Denver, CO")
    _prospect(env.sm, other, "info@mountain.example.com", "Mountain Example Roofing", "Team", "public_inbox")
    reg = contacts(client, other)["info@mountain.example.com"]["registry"]
    assert reg["status"] == "not_eligible" and reg["provider"] == ""


def test_refresh_requires_the_signal_to_be_confirmed(client, env):
    r = client.post(f"/api/companies/{env.company}/registry/refresh")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "signal_not_confirmed"
    assert env.fake.requests == []


def test_refresh_then_review_flow(client, env):
    run(env.sm.set_signal_status("CONTACT_FOUND", "confirmed"))

    r = client.post(f"/api/companies/{env.company}/registry/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "matched" and body["source_url"].startswith("https://search.sunbiz.org/")
    assert body["people"][0]["name"] == "Jane Doe" and body["looked_up_at"]

    reg = contacts(client, env.company)["info@example-palm.example.com"]["registry"]
    assert reg["status"] == "matched" and reg["entity_name"] == "EXAMPLE PALM ROOFING, LLC"
    assert reg["document_number"] == "L15000000001" and reg["confidence"] == 0.95
    assert reg["person"] == {"name": "Jane Doe", "first_name": "Jane", "title": "Manager",
                             "basis": "sole_person"}
    assert reg["name_status"] == "registry_pending" and reg["review"] is None
    # A contact with their own name is untouched by the registry.
    assert contacts(client, env.company)["pat@example-palm.example.com"]["registry"]["name_status"] == "named"

    # GET returns the stored answer without fetching.
    seen = len(env.fake.requests)
    assert client.get(f"/api/companies/{env.company}/registry").json()["status"] == "matched"
    assert len(env.fake.requests) == seen

    r = client.post(f"/api/contacts/{env.inbox}/registry-name", json={"decision": "accepted"})
    assert r.status_code == 200
    assert r.json()["review"] == "accepted" and r.json()["name_status"] == "registry"
    r = client.post(f"/api/contacts/{env.inbox}/registry-name", json={"decision": "dismissed"})
    assert r.json()["review"] == "dismissed" and r.json()["name_status"] == "none"
    r = client.post(f"/api/contacts/{env.inbox}/registry-name", json={"decision": "clear"})
    assert r.json()["review"] is None and r.json()["name_status"] == "registry_pending"
    # The contact itself was never rewritten.
    assert run(env.sm.get_prospect(env.inbox)).first_name == "Example Palm Roofing"


def test_review_errors(client, env):
    assert client.post(f"/api/contacts/{env.inbox}/registry-name",
                       json={"decision": "accepted"}).status_code == 409   # nothing to accept yet
    assert client.post("/api/contacts/nope/registry-name",
                       json={"decision": "accepted"}).status_code == 404
    assert client.post(f"/api/contacts/{env.inbox}/registry-name",
                       json={"decision": "maybe"}).status_code == 422
    assert client.post("/api/companies/nope/registry/refresh").status_code == 404
    assert client.get("/api/companies/nope/registry").status_code == 404


def test_unavailable_registry_is_reported_not_hidden(client, env, monkeypatch):
    run(env.sm.set_signal_status("CONTACT_FOUND", "confirmed"))
    blocked = FakeSunbiz(search_html=fx.CHALLENGE, status=403)
    monkeypatch.setattr(registry_api, "default_providers", lambda: [provider(blocked)])
    body = client.post(f"/api/companies/{env.company}/registry/refresh").json()
    assert body["status"] == "unavailable"
    assert contacts(client, env.company)["info@example-palm.example.com"]["registry"]["status"] == "unavailable"


# ── CLI ──

def _cli(capsys, monkeypatch, *argv):
    import mercury.cli as cli
    monkeypatch.setattr(sys, "argv", ["mercury", *argv])
    cli.main()
    return capsys.readouterr().out


def test_cli_lookup_show_and_list(env, capsys, monkeypatch):
    run(env.sm.set_signal_status("CONTACT_FOUND", "confirmed"))
    out = _cli(capsys, monkeypatch, "registry", "show", "Example Palm")
    assert "not_looked_up" in out

    out = _cli(capsys, monkeypatch, "registry", "lookup", "Example Palm")
    assert "matched" in out and "L15000000001" in out and "Jane Doe" in out
    assert "https://search.sunbiz.org/" in out
    assert "info@example-palm.example.com" in out and "registry_pending" in out
    assert len(env.fake.requests) == 2

    out = _cli(capsys, monkeypatch, "registry", "lookup", "Example Palm")
    assert "[stored]" in out and len(env.fake.requests) == 2      # cached
    _cli(capsys, monkeypatch, "registry", "lookup", "Example Palm", "--force")
    assert len(env.fake.requests) == 4

    assert "matched" in _cli(capsys, monkeypatch, "registry", "list")
    import json
    data = json.loads(_cli(capsys, monkeypatch, "registry", "show", env.company, "--json"))
    assert data["status"] == "matched" and data["people"][0]["name"] == "Jane Doe"


def test_cli_refuses_when_signal_not_confirmed(env, capsys, monkeypatch):
    out = _cli(capsys, monkeypatch, "registry", "lookup", "Example Palm")
    assert "skipped" in out and "CONTACT_FOUND" in out
    assert env.fake.requests == []


# ── Scout hook ──

def test_scout_attaches_evidence_without_touching_the_prospect(env):
    from mercury.agents.scout import Scout

    run(env.sm.set_signal_status("CONTACT_FOUND", "confirmed"))
    scout = Scout.__new__(Scout)
    scout.state = env.sm
    scout._registry_lookups_this_cycle = 0
    before = run(env.sm.get_prospect(env.inbox))

    run(scout._attach_registry_evidence(env.company))

    assert run(env.sm.get_registry_lookup(env.company))["status"] == "matched"
    assert run(env.sm.get_prospect(env.inbox)) == before
    assert scout._registry_lookups_this_cycle == 1

    # Cached: no new request, and the per-cycle count does not move.
    run(scout._attach_registry_evidence(env.company))
    assert len(env.fake.requests) == 2 and scout._registry_lookups_this_cycle == 1


def test_scout_skips_ineligible_and_ungated_companies(env):
    from mercury.agents.scout import Scout

    scout = Scout.__new__(Scout)
    scout.state = env.sm
    scout._registry_lookups_this_cycle = 0
    run(scout._attach_registry_evidence(env.company))            # signal not confirmed
    other = _company(env.sm, "Mountain Example Roofing", "Denver, CO")
    run(env.sm.set_signal_status("CONTACT_FOUND", "confirmed"))
    run(scout._attach_registry_evidence(other))                  # not Florida
    assert env.fake.requests == []
