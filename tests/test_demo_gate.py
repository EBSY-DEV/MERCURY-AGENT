"""The demo gate (#62): an offer that promises a demo built for one business
holds its emails until that demo is ready, and fails closed."""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from fastapi.testclient import TestClient

import mercury.config as config_module
from mercury import cli
from mercury.config import DemosConfig, MercuryConfig, OfferDefinition
from mercury.control.demos import DemoError, DemoService
from mercury.demos import (
    annotate_outbox,
    check_campaign,
    check_outbox_item,
    retire_stale_demos,
    verdict,
    waiting_for_demo,
)
from mercury.models.campaign import Campaign, EmailStep
from mercury.state import MIGRATIONS, StateManager, _split_sql
from tests.test_outbox_native import Cfg, FakeProvider, seed_prospect

TEMPLATE = Path(__file__).resolve().parent.parent / "mercury.yaml"
OFFERS = [
    OfferDefinition(key="voice", requires_demo=True, demo_kind="voice"),
    OfferDefinition(key="website", requires_demo=True, demo_kind="website"),
    OfferDefinition(key="tools", requires_demo=False),
]


class DemoCfg(Cfg):
    offers = OFFERS
    demos = DemosConfig(retire_after_days=14)


def _iso(dt):
    return dt.replace(tzinfo=None).isoformat()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "mercury.db"))
    await sm.init_db()
    yield sm


def make_sender(state, provider, config=None):
    from mercury.agents.sender import Sender

    cfg = config or DemoCfg()
    cfg.channels.email.require_approval = False
    sender = Sender(brain=None, state=state, config=cfg, env=type("Env", (), {"instantly_api_key": ""})())
    sender.provider = provider
    sender.send_pacing = False
    return sender


async def seed_campaign(state, prospect_ids, offer_key="voice"):
    campaign = Campaign(
        id="", name="voice-campaign", channel="email", offer_key=offer_key,
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="We built a line for {{company}}.", delay_days=0),
            EmailStep(step=2, subject="follow", body="It is still ready, {{first_name}}.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── Config ──


def test_offer_config_validates_keys_and_kinds():
    data = yaml.safe_load(TEMPLATE.read_text())
    config = MercuryConfig(**data)
    assert config.offers == [] and config.demos.retire_after_days == 14
    data["offers"] = [{"key": " Voice ", "requires_demo": True, "demo_kind": "voice"}]
    assert MercuryConfig(**data).offers[0].key == "voice"
    for bad in ([{"key": "voice"}, {"key": "voice"}],
                [{"key": "voice", "demo_kind": "hologram"}],
                [{"key": "two words"}]):
        data["offers"] = bad
        with pytest.raises(Exception):
            MercuryConfig(**data)
    data["offers"], data["demos"] = [], {"retire_after_days": -1}
    with pytest.raises(Exception):
        MercuryConfig(**data)


# ── Migration ──


@pytest.mark.asyncio
async def test_migration_adds_demos_and_offer_keys_to_an_existing_db(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    for script in MIGRATIONS[:13]:
        for statement in _split_sql(script):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 13")
    conn.execute("INSERT INTO outbox (id, prospect_id, to_email) VALUES ('old1', 'p1', 'a@b.co')")
    conn.execute("INSERT INTO campaigns (id, name) VALUES ('c1', 'old')")
    conn.commit()
    conn.close()

    await StateManager(db).init_db()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert conn.execute("SELECT offer_key FROM outbox WHERE id = 'old1'").fetchone()[0] == ""
    assert conn.execute("SELECT offer_key FROM campaigns WHERE id = 'c1'").fetchone()[0] == ""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(demos)")}
    assert {"prospect_id", "offer_key", "kind", "status", "demo_url", "recording_path",
            "agent_id", "built_by", "created_at", "ready_at", "retired_at"} <= cols
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO demos (id, prospect_id, offer_key, status) VALUES ('d', 'p', 'voice', 'built')")
    conn.close()


@pytest.mark.asyncio
async def test_one_live_demo_per_contact_and_offer(state):
    first, created = await state.request_demo("p1", "voice", "voice")
    again, created_again = await state.request_demo("p1", "VOICE")
    assert created and not created_again and first == again
    assert await state.retire_demo(first, "test")
    newer, created = await state.request_demo("p1", "voice")
    assert created and newer != first
    assert await state.mark_demo_ready(newer, demo_url="https://demo.example/al-air")
    assert not await state.mark_demo_ready(first)  # retired stays retired
    demo = await state.get_demo(newer)
    assert demo["status"] == "ready" and demo["ready_at"] and demo["demo_url"].endswith("al-air")


@pytest.mark.asyncio
async def test_outbox_rows_inherit_the_campaign_offer(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    item_id = await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, to_email="jane@acme.com",
                                          subject="s", body="b", send_at=_iso(_now()))
    item = await state.get_outbox_item(item_id)
    assert item["offer_key"] == "voice"
    other = await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, step=2, offer_key="Tools",
                                        to_email="jane@acme.com", subject="s", body="b", send_at=_iso(_now()))
    assert (await state.get_outbox_item(other))["offer_key"] == "tools"


# ── The decision ──


def test_verdict_cases():
    offers = {o.key: o for o in OFFERS}
    assert not verdict("", offers, None, "p").held                    # no offer: no gate
    assert not verdict("tools", offers, None, "p").held               # offer without a demo
    assert not verdict("voice", offers, "ready", "p").held
    assert verdict("voice", offers, "requested", "p").code == "demo_requested"
    assert verdict("voice", offers, None, "p").code == "no_demo"
    assert verdict("voice", offers, "retired", "p").code == "no_demo"
    # Fail closed: anything the gate can't settle holds.
    assert verdict("hologram", offers, "ready", "p").code == "unknown_offer"
    assert verdict("voice", None, "ready", "p").code == "no_config"
    assert verdict("voice", offers, "ready", "").code == "no_prospect"
    assert verdict("voice", {}, None, "p").held


# ── The Sender ──


@pytest.mark.asyncio
async def test_voice_step_one_without_demo_never_sends_until_marked_ready(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider)

    await sender._run_native()
    await sender._run_native()
    assert provider.sent == []
    step1 = [r for r in await state.get_outbox(status="approved") if r["step"] == 1][0]
    assert step1["offer_key"] == "voice"
    demo = await state.find_live_demo(pid, "voice")
    assert demo["status"] == "requested" and demo["kind"] == "voice"

    waiting = await waiting_for_demo(state, DemoCfg())
    assert [(w["to_email"], w["step"], w["code"]) for w in waiting] == [("jane@acme.com", 1, "demo_requested")]

    service = await DemoService(state, DemoCfg()).ready()
    await service.mark_ready("jane@acme.com", recording_path="/demos/acme.mp3", built_by="Carlos")
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["jane@acme.com"]
    assert await waiting_for_demo(state, DemoCfg()) == []


@pytest.mark.asyncio
async def test_offer_without_demo_and_rows_without_offer_send(state):
    tools = await seed_prospect(state, email="tools@acme.com")
    plain = await seed_prospect(state, email="plain@acme.com")
    await seed_campaign(state, [tools], offer_key="tools")
    await seed_campaign(state, [plain], offer_key="")
    provider = FakeProvider()
    await make_sender(state, provider)._run_native()
    assert sorted(m["to"] for m in provider.sent) == ["plain@acme.com", "tools@acme.com"]
    assert await state.list_demos() == []


@pytest.mark.asyncio
async def test_unknown_offer_holds(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid], offer_key="hologram")
    provider = FakeProvider()
    await make_sender(state, provider)._run_native()
    assert provider.sent == []
    (waiting,) = await waiting_for_demo(state, DemoCfg())
    assert waiting["code"] == "unknown_offer"


@pytest.mark.asyncio
async def test_a_failing_lookup_holds_instead_of_sending(state, monkeypatch):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    demo_id, _ = await state.request_demo(pid, "voice")
    await state.mark_demo_ready(demo_id)

    async def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(state, "find_live_demo", broken)
    provider = FakeProvider()
    await make_sender(state, provider)._run_native()
    assert provider.sent == []
    item = [r for r in await state.get_outbox(status="approved") if r["step"] == 1][0]
    result = await check_outbox_item(state, DemoCfg(), item)
    assert result.held and result.code == "error"


@pytest.mark.asyncio
async def test_a_config_without_offers_holds_rows_that_carry_one(state):
    """The gate can't tell whether 'voice' needs a demo, so it holds."""
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    await make_sender(state, provider, config=Cfg())._run_native()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_follow_ups_wait_when_the_demo_is_retired_mid_sequence(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    demo_id, _ = await state.request_demo(pid, "voice")
    await state.mark_demo_ready(demo_id)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._run_native()
    assert len(provider.sent) == 1
    await state.retire_demo(demo_id, "test")
    step2 = (await state.get_outbox(status="approved"))[0]
    await state.update_outbox_item(step2["id"], send_at=_iso(_now() - timedelta(days=4)))
    async with state._connect() as db:  # step 1 went out long enough ago
        await db.execute("UPDATE outbox SET sent_at = ? WHERE step = 1", (_iso(_now() - timedelta(days=5)),))
        await db.commit()
    step2 = await state.get_outbox_item(step2["id"])
    assert (await check_outbox_item(state, DemoCfg(), step2)).code == "no_demo"
    await sender._run_native()
    assert len(provider.sent) == 1
    # A new ready demo releases it: the hold was the demo, not the schedule.
    await DemoService(state, DemoCfg()).mark_ready("jane@acme.com")
    await sender._run_native()
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_instantly_campaign_waits_for_every_demo(state):
    a = await seed_prospect(state, email="a@acme.com")
    b = await seed_prospect(state, email="b@acme.com")
    campaign = await seed_campaign(state, [a, b])
    assert (await check_campaign(state, DemoCfg(), campaign)).held
    assert {d["status"] for d in await state.list_demos()} == {"requested"}
    for d in await state.list_demos():
        await state.mark_demo_ready(d["id"])
    assert not (await check_campaign(state, DemoCfg(), campaign)).held


# ── Retirement ──


@pytest.mark.asyncio
async def test_ready_demos_retire_after_the_configured_days_without_a_reply(state):
    quiet = await seed_prospect(state, email="quiet@acme.com", status="contacted")
    talking = await seed_prospect(state, email="talk@acme.com", status="replied")
    pending = await seed_prospect(state, email="pending@acme.com", status="contacted")
    fresh = await seed_prospect(state, email="fresh@acme.com", status="contacted")
    old = _iso(_now() - timedelta(days=20))
    ids = {}
    for pid in (quiet, talking, pending, fresh):
        ids[pid], _ = await state.request_demo(pid, "voice")
        await state.mark_demo_ready(ids[pid])
    async with state._connect() as db:
        await db.execute("UPDATE demos SET ready_at = ?", (old,))
        await db.commit()
    for pid, sent_at in ((quiet, old), (talking, old), (pending, old), (fresh, _iso(_now() - timedelta(days=2)))):
        item = await state.add_outbox_item(prospect_id=pid, to_email="x@acme.com", subject="s", body="b",
                                           send_at=sent_at, status="sent", campaign_id="c", offer_key="voice")
        await state.update_outbox_item(item, sent_at=sent_at)
    await state.add_outbox_item(prospect_id=pending, to_email="x@acme.com", subject="s", body="b", step=2,
                                send_at=_iso(_now()), status="approved", campaign_id="c", offer_key="voice")

    class Never(DemoCfg):
        demos = DemosConfig(retire_after_days=0)

    assert await retire_stale_demos(state, Never()) == 0
    assert await retire_stale_demos(state, DemoCfg()) == 1
    statuses = {pid: (await state.get_demo(ids[pid]))["status"] for pid in ids}
    assert statuses == {quiet: "retired", talking: "ready", pending: "ready", fresh: "ready"}
    assert "no reply 14 days" in (await state.get_demo(ids[quiet]))["retire_reason"]


# ── Commands: the service, the CLI and the dashboard ──


@pytest.mark.asyncio
async def test_service_resolves_contacts_and_refuses_bad_input(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    item_id = await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, to_email="jane@acme.com",
                                          subject="s", body="b", send_at=_iso(_now()))
    service = await DemoService(state, DemoCfg()).ready()
    requested = await service.request(item_id)          # by outbox row
    assert requested["offer_key"] == "voice" and requested["status"] == "requested"
    with pytest.raises(DemoError) as bad_url:
        await service.mark_ready(pid, demo_url="ftp://nope")
    assert bad_url.value.code == "invalid"
    with pytest.raises(DemoError) as unknown:
        await service.mark_ready(pid, "hologram")
    assert unknown.value.code == "unknown_offer"
    ready = await service.mark_ready(requested["id"][:6], demo_url="https://demo.example/acme")
    assert ready["status"] == "ready"
    retired = await service.retire("jane@acme.com")
    assert retired["status"] == "retired"
    with pytest.raises(DemoError) as gone:
        await service.retire("jane@acme.com")
    assert gone.value.code == "not_found"
    with pytest.raises(DemoError):
        await DemoService(state, None).mark_ready(pid, "voice")   # no config: no changes


def _config_with_offers():
    data = yaml.safe_load(TEMPLATE.read_text())
    data["offers"] = [o.model_dump() for o in OFFERS]
    return MercuryConfig(**data)


def test_cli_lists_marks_ready_and_retires(tmp_path, monkeypatch, capsys):
    import asyncio

    config = _config_with_offers()
    db = tmp_path / "mercury.db"
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("mercury.state.DB_PATH", db)
    state = StateManager(str(db))

    async def seed():
        await state.init_db()
        pid = await seed_prospect(state)
        campaign = await seed_campaign(state, [pid])
        await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, to_email="jane@acme.com",
                                    subject="s", body="b", send_at=_iso(_now()), status="approved")
        return pid

    pid = asyncio.run(seed())

    def mercury(*argv):
        monkeypatch.setattr(sys, "argv", ["mercury", "demos", *argv])
        cli.main()
        return capsys.readouterr().out

    listed = json.loads(mercury("--json"))
    assert [w["to_email"] for w in listed["waiting"]] == ["jane@acme.com"]
    assert "Waiting for a demo: 1" in mercury()
    assert "is ready" in mercury("ready", "jane@acme.com", "--url", "https://demo.example/acme", "--by", "Carlos")
    demo = asyncio.run(state.find_live_demo(pid, "voice"))
    assert demo["status"] == "ready" and demo["built_by"] == "Carlos"
    assert json.loads(mercury("--json"))["waiting"] == []
    assert "retired" in mercury("retire", "jane@acme.com", "--reason", "closed the site")
    with pytest.raises(SystemExit):
        mercury("ready", "nobody@nowhere.example")
    assert "No demo, contact or outbox email" in capsys.readouterr().err


def test_dashboard_shows_the_hold_and_marks_ready(tmp_path, monkeypatch):
    import asyncio

    import mercury.dashboard as dash

    config = _config_with_offers()
    db = tmp_path / "mercury.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: config)
    state = StateManager(str(db))

    async def seed():
        await state.init_db()
        pid = await seed_prospect(state)
        campaign = await seed_campaign(state, [pid])
        return await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, to_email="jane@acme.com",
                                           subject="s", body="b", send_at=_iso(_now()))

    item_id = asyncio.run(seed())
    with TestClient(dash.app) as client:
        outbox = client.get("/api/outbox").json()
        (row,) = outbox["pending"]
        assert row["demo"]["held"] and row["demo"]["offer_key"] == "voice"
        assert [w["outbox_id"] for w in outbox["waiting_demo"]] == [item_id]
        today = client.get("/api/today").json()
        assert any(i["key"] == "demos" for i in today["items"])
        assert today["stats"]["waiting_demo"] == 1

        bad = client.post("/api/demos/ready", json={"target": item_id, "demo_url": "nope"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid"
        missing = client.post("/api/demos/ready", json={"target": "nobody"})
        assert missing.status_code == 404
        ok = client.post("/api/demos/ready", json={"target": item_id, "demo_url": "https://demo.example/acme"})
        assert ok.status_code == 200 and ok.json()["demo"]["status"] == "ready"

        outbox = client.get("/api/outbox").json()
        assert not outbox["pending"][0]["demo"]["held"] and outbox["waiting_demo"] == []
        assert not any(i["key"] == "demos" for i in client.get("/api/today").json()["items"])
        assert len(client.get("/api/demos").json()["demos"]) == 1


@pytest.mark.asyncio
async def test_annotation_fails_closed_without_config(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    item_id = await state.add_outbox_item(prospect_id=pid, campaign_id=campaign.id, to_email="jane@acme.com",
                                          subject="s", body="b", send_at=_iso(_now()))
    rows = await annotate_outbox(state, None, [await state.get_outbox_item(item_id)])
    assert rows[0]["demo"]["held"] and rows[0]["demo"]["code"] == "no_config"
