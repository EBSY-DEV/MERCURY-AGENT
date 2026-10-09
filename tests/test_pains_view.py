"""The Pains view on the Signals tab: its page wiring, the API calls it
makes, and the demo data that fills it.

Everything here is invented: codes are PAIN_A..PAIN_E, markets segment_a and
segment_b, offers offer_a and offer_b.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.config import MarketConfig, OfferDefinition
from mercury.state import StateManager

ROOT = Path(__file__).resolve().parent.parent


def _demo_pains():
    path = ROOT / "scripts" / "demo_pains.py"
    spec = importlib.util.spec_from_file_location("demo_pains_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(dash, "DB_PATH", Path(tmp) / "mercury.db")
        monkeypatch.setattr(dash, "WEB_DIR", ROOT / "mercury" / "web")
        with TestClient(dash.app) as c:
            yield c


# ── The page ──


def test_page_loads_the_pains_view(client):
    page = client.get("/").text
    assert "/static/pains.css" in page and "/static/pains.js" in page
    assert 'id="signals-view-pains"' in page and 'id="signals-view-signals"' in page
    assert client.get("/static/pains.js").status_code == 200
    assert client.get("/static/pains.css").status_code == 200


def test_the_signals_view_keeps_its_own_containers(client):
    page = client.get("/").text
    signals = page.split('id="signals-view-signals"', 1)[1].split('id="signals-view-pains"', 1)[0]
    for part in ("signals-summary", "signals-groups", "cohort-builder", "cohort-result"):
        assert f'id="{part}"' in signals


# ── What the view sends ──


def test_the_view_adds_edits_decides_and_restores(client):
    words = "Our phone rings out after five."
    made = client.post("/api/pains", json={
        "label": words, "owner_words": words, "scene": "Quotes sit unanswered for days.",
        "cost": "Callers hire the next number.", "market": "segment_a", "sector": "trade_a",
        "offer_key": "offer_a", "signal_codes": [], "evidence": ["example.com/contact"],
        "confirm": True}).json()
    code = made["pain"]["code"]
    assert made["pain"]["status"] == "confirmed" and made["pain"]["stats"]["sends"] == 0

    saved = client.post(f"/api/pains/{code}/save", json={
        "cost": "Callers hire the next number on the list.", "expected_revision": made["pain"]["revision"]}).json()
    assert saved["pain"]["cost"].endswith("on the list.")

    # The list the view reloads carries what the toolbar and drawer need.
    listed = client.get("/api/pains").json()
    assert {"markets", "offers", "signals", "summary"} <= set(listed)
    assert listed["summary"]["confirmed"] == 1

    rejected = client.post("/api/pains/status", json={
        "code": code, "status": "rejected", "note": "too generic",
        "revisions": {code: saved["pain"]["revision"]}}).json()
    assert rejected["changed"] == 1
    row = client.get(f"/api/pains/{code}").json()
    assert row["status"] == "rejected" and row["status_note"] == "too generic"

    # Restore reopens it; a decision made on an old revision is reported, not applied.
    stale = client.post("/api/pains/status", json={
        "code": code, "status": "proposed", "revisions": {code: row["revision"] - 1}}).json()
    assert stale["changed"] == 0 and stale["stale"] == [code]
    restored = client.post("/api/pains/status", json={
        "code": code, "status": "proposed", "revisions": {code: row["revision"]}}).json()
    assert restored["changed"] == 1
    assert client.get(f"/api/pains/{code}").json()["status"] == "proposed"


def test_the_view_gets_codes_it_can_show_for_refusals(client):
    first = client.post("/api/pains", json={"label": "Jobs slip through the cracks."}).json()
    again = client.post("/api/pains", json={"label": "Jobs slip through the cracks."})
    assert again.status_code == 409 and again.json()["code"] == "duplicate"
    assert again.json()["matched"] == first["pain"]["code"]

    client.post("/api/pains/status", json={"code": first["pain"]["code"], "status": "rejected"})
    repeat = client.post("/api/pains", json={"label": "Jobs slip through the cracks, again."})
    assert repeat.status_code == 409 and repeat.json()["code"] == "matches_rejected"
    assert repeat.json()["matched"] == first["pain"]["code"]


# ── The demo data ──


@pytest_asyncio.fixture
async def seeded(tmp_path):
    sm = StateManager(str(tmp_path / "demo.db"))
    await sm.init_db()
    return sm


@pytest.mark.asyncio
async def test_demo_pains_cover_every_status_and_show_results(seeded):
    demo = _demo_pains()
    # Two people were sent a pain-less sequence email; one of them replied.
    for n in range(3):
        await seeded.add_outbox_item(
            prospect_id=f"p{n}", to_email=f"p{n}@example.com", kind="sequence", step=1,
            subject="s", body="b", status="sent", send_at="2026-10-01T10:00:00")
        await seeded.update_outbox_item(
            (await seeded.get_outbox(status="sent"))[-1]["id"], sent_at="2026-10-01T10:00:00")

    counts = await demo.seed_pains(seeded)
    assert counts["pains"] == 5 and counts["pain_emails"] == 3

    pains = {p["code"]: p for p in await seeded.list_pains()}
    assert {c: p["status"] for c, p in pains.items()} == {
        "PAIN_A": "proposed", "PAIN_B": "confirmed", "PAIN_C": "confirmed",
        "PAIN_D": "proposed", "PAIN_E": "rejected"}
    assert pains["PAIN_E"]["status_note"] == "too generic"
    assert all(p["status_by"] for p in pains.values() if p["status"] != "proposed")

    stats = await seeded.pain_stats()
    assert sum(s["sends"] for s in stats.values()) == 3 and set(stats) <= {"PAIN_B", "PAIN_C"}

    # The editor can offer the signals the pains name.
    confirmed = {s["code"] for s in await seeded.get_signal_codes("confirmed")}
    assert {c for p in pains.values() for c in p["signal_codes"]} <= confirmed


def test_demo_config_names_the_markets_and_offers_the_pains_use():
    demo = _demo_pains()
    cfg = {"offers": [{"key": "voice"}]}
    demo.extend_config(cfg)
    markets = [MarketConfig(**m).name for m in cfg["icp"]["markets"]]
    offers = [OfferDefinition(**o).key for o in cfg["offers"]]
    assert markets == ["segment_a", "segment_b"]
    assert offers == ["voice", "offer_a", "offer_b"]
    used = {(p[4]["market"], p[4]["offer_key"]) for p in demo.PAINS}
    assert {m for m, _ in used} <= set(markets) and {o for _, o in used} <= set(offers)
