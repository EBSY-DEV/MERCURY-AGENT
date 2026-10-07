"""Inbox warm-up: ramp plan, health gates, sender enforcement, endpoints, DNS."""

import asyncio
import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury import warmup
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from tests.test_outbox_native import Cfg, FakeProvider, make_sender


def _run(coro):
    return asyncio.run(coro)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _now_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + delta).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(warmup.SENDER_ENV, raising=False)
    warmup.clear_dns_cache()
    yield
    warmup.clear_dns_cache()


# ── Pure plan / caps / gates ──


def test_ramp_plan_shape_week1_and_target():
    plan = warmup.ramp_plan(50)
    assert len(plan) == 28
    assert plan[:7] == [5, 5, 6, 7, 8, 9, 10]
    assert plan[7] == 12 and plan[13] == 20
    assert plan[14] == 22 and plan[20] == 35
    assert plan[-1] == 50
    assert plan == sorted(plan)


@pytest.mark.parametrize("target", [1, 3, 8, 20, 30, 35, 50, 120, 200])
def test_ramp_plan_clipped_and_monotonic(target):
    plan = warmup.ramp_plan(target)
    assert all(c <= target for c in plan)
    assert all(b >= a for a, b in zip(plan, plan[1:]))
    assert plan[-1] == target


def test_plan_day_and_cap_after_day_28():
    start = date(2026, 1, 1)
    assert warmup.plan_day(start, start) == 1
    assert warmup.plan_day(start, start + timedelta(days=27)) == 28
    assert warmup.plan_cap(29, 40) == 40
    assert warmup.plan_cap(0, 40) == 5


def test_health_gate_thresholds():
    assert warmup.health_gate(19, 10)[0] == "ok"
    assert "not enough sends" in warmup.health_gate(19, 10)[1]
    assert warmup.health_gate(100, 3)[0] == "ok"      # exactly 3% → ok
    assert warmup.health_gate(100, 4)[0] == "hold"
    assert warmup.health_gate(100, 5)[0] == "hold"    # exactly 5% → hold
    assert warmup.health_gate(100, 6)[0] == "pause"
    assert warmup.health_gate(20, 2)[0] == "pause"


def test_effective_cap_by_status_and_gate():
    assert warmup.effective_cap("not_started", None, 30, "ok") is None
    assert warmup.effective_cap("paused", 10, 30, "ok") == 0
    assert warmup.effective_cap("complete", 28, 30, "ok") == 30
    assert warmup.effective_cap("warming", 9, 50, "ok") == warmup.ramp_plan(50)[8]
    # hold → yesterday's cap
    assert warmup.effective_cap("warming", 9, 50, "hold") == warmup.ramp_plan(50)[7]
    assert warmup.effective_cap("warming", 1, 50, "hold") == 5
    assert warmup.effective_cap("warming", 9, 50, "pause") == 0
    # scheduled in the future → never looser than day 1
    assert warmup.effective_cap("warming", -3, 50, "ok") == 5


def test_default_target_and_weeks():
    assert warmup.default_target(None) == 30
    assert warmup.default_target(25) == 25
    assert warmup.default_target(80) == 50
    weeks = warmup.build_weeks({"profile": True}, dns_ok=True, target=30)
    assert [w["week"] for w in weeks] == [0, 1, 2, 3, 4, 5]
    w0 = {t["key"]: t["done"] for t in weeks[0]["tasks"]}
    assert w0["dns"] is True and w0["profile"] is True and w0["newsletters"] is False
    assert weeks[1]["range"] == "5-10/day"
    assert weeks[5]["range"] == "30/day"
    assert warmup.week_for_day(None) == 0
    assert warmup.week_for_day(7) == 1 and warmup.week_for_day(8) == 2
    assert warmup.week_for_day(28) == 4 and warmup.week_for_day(29) == 5


def test_resolve_sender_email(monkeypatch):
    rows = [{"email": "a@new.co", "status": "not_started", "start_date": None},
            {"email": "b@new.co", "status": "warming", "start_date": "2026-01-01"}]
    assert warmup.resolve_sender_email("me@real.co", rows) == "me@real.co"
    assert warmup.resolve_sender_email("mercury@yourcompany.com", rows) == "b@new.co"
    assert warmup.resolve_sender_email("", []) is None
    monkeypatch.setenv(warmup.SENDER_ENV, "Env@Override.co")
    assert warmup.resolve_sender_email("me@real.co", rows) == "env@override.co"


# ── State-backed evaluation ──


@pytest.fixture
def sm(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    _run(s.init_db())
    return s


_n = [0]


def _prospect(sm, status="queued"):
    _n[0] += 1
    email = f"lead{_n[0]}@acme.co"
    pid = _run(sm.add_prospect(Prospect(
        first_name="Pat", last_name="Lee", title="Owner", email=email,
        email_status="verified", email_verified=True, status=status)))
    return pid, email


def _sent(sm, n, when=None, kind="sequence"):
    pids = []
    for i in range(n):
        pid, email = _prospect(sm, status="contacted")
        item = _run(sm.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s", body="b",
            send_at=when or _now_iso(), status="approved",
            campaign_id="" if kind == "reply" else "c-sent", step=1, kind=kind))
        _run(sm.update_outbox_item(item, status="sent", sent_at=when or _now_iso()))
        pids.append(pid)
    return pids


def _queue(sm, n):
    for _ in range(n):
        pid, email = _prospect(sm)
        _run(sm.add_outbox_item(
            prospect_id=pid, to_email=email, subject="hello",
            body="Fine body. Question?", send_at=_now_iso(timedelta(minutes=-5)),
            status="approved", campaign_id="c-q", step=1))


def _view(sm, email, is_sender=True, config_max=50):
    row = _run(sm.get_warmup_inbox(email))
    return _run(warmup.inbox_view(sm, row, is_sender=is_sender, config_max=config_max))


def test_day_28_rolls_over_to_complete(sm):
    start = (_today() - timedelta(days=30)).isoformat()
    _run(sm.add_warmup_inbox("me@send.co", status="warming", start_date=start, target_daily=40))
    v = _view(sm, "me@send.co")
    assert v["status"] == "complete"
    assert v["day"] == 28 and v["today_cap"] == 40 and v["current_week"] == 5
    assert _run(sm.get_warmup_inbox("me@send.co"))["status"] == "complete"
    assert all(p["sent"] is not None for p in v["plan"])


def test_plan_entries_and_sent_counts(sm):
    start = _today() - timedelta(days=2)
    _run(sm.add_warmup_inbox("me@send.co", status="warming", start_date=start.isoformat()))
    _sent(sm, 3)
    _sent(sm, 1, kind="reply")                    # volume includes Mercury's replies
    _sent(sm, 2, when=f"{(start).isoformat()} 10:00:00")
    v = _view(sm, "me@send.co")
    assert v["day"] == 3 and v["current_week"] == 1
    assert len(v["plan"]) == 28
    assert v["plan"][0] == {"day": 1, "date": start.isoformat(), "cap": 5, "sent": 2}
    assert v["plan"][2]["sent"] == 4 and v["sent_today"] == 4
    assert v["plan"][3]["sent"] is None
    assert v["today_cap"] == 6
    # A non-sender inbox has no send data.
    _run(sm.add_warmup_inbox("other@send.co", status="warming", start_date=start.isoformat()))
    o = _view(sm, "other@send.co", is_sender=False)
    assert o["sent_today"] == 0 and o["plan"][2]["sent"] == 0 and o["health"]["sent_7d"] == 0


def test_high_bounce_rate_auto_pauses(sm):
    start = (_today() - timedelta(days=10)).isoformat()
    _run(sm.add_warmup_inbox("me@send.co", status="warming", start_date=start))
    pids = _sent(sm, 20)
    for pid in pids[:2]:  # 10%
        _run(sm.log_action("bounce", "handler", {"prospect_id": pid}))
    v = _view(sm, "me@send.co")
    assert v["health"]["gate"] == "pause"
    assert v["health"]["bounce_rate"] == pytest.approx(0.1)
    assert v["status"] == "paused" and v["today_cap"] == 0
    row = _run(sm.get_warmup_inbox("me@send.co"))
    assert row["status"] == "paused" and row["paused_at"]


def test_hold_freezes_at_yesterdays_cap(sm):
    start = (_today() - timedelta(days=9)).isoformat()   # day 10
    _run(sm.add_warmup_inbox("me@send.co", status="warming", start_date=start, target_daily=50))
    pids = _sent(sm, 25)
    _run(sm.log_action("bounce", "handler", {"prospect_id": pids[0]}))  # 4%
    v = _view(sm, "me@send.co")
    assert v["health"]["gate"] == "hold"
    assert v["status"] == "warming"
    assert v["today_cap"] == warmup.ramp_plan(50)[8]  # day 9's cap


# ── Sender enforcement ──


def _drain(sm):
    provider = FakeProvider()
    sender = make_sender(sm, provider)
    _run(sender._drain_due())
    return len(provider.sent)


def test_sender_uses_warmup_cap(sm):
    _run(sm.add_warmup_inbox(Cfg.persona.email, status="warming",
                             start_date=_today().isoformat()))
    _queue(sm, 8)
    assert _drain(sm) == 5           # day-1 cap, below configured 50


def test_sender_paused_inbox_sends_nothing(sm):
    _run(sm.add_warmup_inbox(Cfg.persona.email, status="paused",
                             start_date=_today().isoformat()))
    _queue(sm, 3)
    assert _drain(sm) == 0


def test_warmup_never_raises_configured_cap(sm, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 2)
    start = (_today() - timedelta(days=20)).isoformat()   # plan cap ~33
    _run(sm.add_warmup_inbox(Cfg.persona.email, status="warming", start_date=start))
    _queue(sm, 6)
    assert _drain(sm) == 2


def test_sender_unaffected_without_warming_inbox(sm):
    _run(sm.add_warmup_inbox(Cfg.persona.email, status="not_started"))
    _run(sm.add_warmup_inbox("someone@else.co", status="paused",
                             start_date=_today().isoformat()))
    _queue(sm, 7)
    assert _drain(sm) == 7


def test_sender_counts_todays_sends_against_warmup_cap(sm):
    _run(sm.add_warmup_inbox(Cfg.persona.email, status="warming",
                             start_date=_today().isoformat()))
    _sent(sm, 4)
    _queue(sm, 5)
    assert _drain(sm) == 1


# ── Endpoints ──


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "_sender_config", lambda: ("me@send.co", 40))
        s = StateManager(db_path=str(db))
        _run(s.init_db())
        with TestClient(dash.app) as c:
            c.sm = s
            yield c


def _inboxes(client):
    data = client.get("/api/warmup").json()
    return data, {i["email"]: i for i in data["inboxes"]}


def test_virtual_sender_inbox(client):
    data, inboxes = _inboxes(client)
    assert data["active_email"] == "me@send.co"
    v = inboxes["me@send.co"]
    assert v["is_sender"] is True and v["status"] == "not_started"
    assert v["start_date"] is None and v["day"] is None and v["today_cap"] is None
    assert v["plan"] == [] and v["target_daily"] == 40 and v["current_week"] == 0
    assert len(v["weeks"]) == 6 and all("tasks" in w and "range" in w for w in v["weeks"])
    assert set(v["health"]) == {"sent_7d", "bounce_rate", "reply_rate", "gate", "reason"}
    assert _run(client.sm.get_warmup_inbox("me@send.co")) is None  # not persisted


def test_no_active_email_with_placeholder_config(client, monkeypatch):
    monkeypatch.setattr(dash, "_sender_config", lambda: ("mercury@yourcompany.com", 50))
    data, _ = _inboxes(client)
    assert data == {"active_email": None, "inboxes": []}
    client.post("/api/warmup/inboxes", json={"email": "demo@warm.co",
                                             "start_date": _today().isoformat()})
    data, inboxes = _inboxes(client)
    assert data["active_email"] == "demo@warm.co" and inboxes["demo@warm.co"]["is_sender"]


def test_add_inbox_validation_and_duplicates(client):
    assert client.post("/api/warmup/inboxes", json={"email": "nope"}).status_code == 400
    assert client.post("/api/warmup/inboxes", content="x").status_code == 400
    r = client.post("/api/warmup/inboxes", json={"email": "New@Box.co"})
    assert r.json() == {"success": True}
    dup = client.post("/api/warmup/inboxes", json={"email": "new@box.co"})
    assert dup.status_code == 400 and dup.json()["success"] is False
    assert client.post("/api/warmup/inboxes", json={"email": "b@box.co", "start_date": "06/10"}).status_code == 400
    assert client.post("/api/warmup/inboxes", json={"email": "b@box.co", "target_daily": 0}).status_code == 400
    assert client.post("/api/warmup/inboxes", json={"email": "b@box.co", "target_daily": True}).status_code == 400
    ok = client.post("/api/warmup/inboxes", json={
        "email": "b@box.co", "start_date": _today().isoformat(), "target_daily": 20})
    assert ok.status_code == 200
    _, inboxes = _inboxes(client)
    assert inboxes["new@box.co"]["status"] == "not_started"
    assert inboxes["new@box.co"]["is_sender"] is False
    b = inboxes["b@box.co"]
    assert b["status"] == "warming" and b["target_daily"] == 20 and b["day"] == 1
    assert b["plan"][-1]["cap"] == 20


def test_actions_lifecycle(client):
    url = "/api/warmup/inboxes/me@send.co/action"
    assert client.post(url, json={"action": "explode"}).status_code == 400
    assert client.post(url, json={"action": "pause"}).status_code == 404  # virtual, no row
    assert client.post(url, json={"action": "start"}).json() == {"success": True}
    _, inboxes = _inboxes(client)
    v = inboxes["me@send.co"]
    assert v["status"] == "warming" and v["start_date"] == _today().isoformat()
    assert v["day"] == 1 and v["today_cap"] == 5 and v["current_week"] == 1
    assert len(v["plan"]) == 28 and v["plan"][0]["sent"] == 0 and v["plan"][1]["sent"] is None

    assert client.post(url, json={"action": "start"}).status_code == 400
    assert client.post(url, json={"action": "resume"}).status_code == 400
    assert client.post(url, json={"action": "pause"}).status_code == 200
    _, inboxes = _inboxes(client)
    assert inboxes["me@send.co"]["status"] == "paused"
    assert inboxes["me@send.co"]["today_cap"] == 0
    assert "manually" in inboxes["me@send.co"]["health"]["reason"]
    assert client.post(url, json={"action": "resume"}).status_code == 200
    assert _inboxes(client)[1]["me@send.co"]["status"] == "warming"

    # reset restarts the plan today but keeps the checklist
    client.post("/api/warmup/inboxes/me@send.co", json={"start_date": "2026-01-01"})
    client.post("/api/warmup/inboxes/me@send.co/task", json={"key": "profile", "done": True})
    assert client.post(url, json={"action": "reset"}).status_code == 200
    v = _inboxes(client)[1]["me@send.co"]
    assert v["start_date"] == _today().isoformat() and v["day"] == 1
    assert {t["key"]: t["done"] for t in v["weeks"][0]["tasks"]}["profile"] is True

    assert client.post(url, json={"action": "remove"}).status_code == 200
    assert _run(client.sm.get_warmup_inbox("me@send.co")) is None
    assert _inboxes(client)[1]["me@send.co"]["status"] == "not_started"  # virtual again
    assert client.post("/api/warmup/inboxes/ghost@x.co/action",
                       json={"action": "remove"}).status_code == 404


def test_resume_restarts_health_window(client):
    sm = client.sm
    start = (_today() - timedelta(days=10)).isoformat()
    _run(sm.add_warmup_inbox("me@send.co", status="warming", start_date=start))
    pids = _sent(sm, 20, when=_now_iso(timedelta(hours=-2)))
    for pid in pids[:3]:
        _run(sm.log_action("bounce", "handler", {"prospect_id": pid},
                           created_at=_now_iso(timedelta(hours=-1))))
    assert _inboxes(client)[1]["me@send.co"]["status"] == "paused"
    client.post("/api/warmup/inboxes/me@send.co/action", json={"action": "resume"})
    v = _inboxes(client)[1]["me@send.co"]
    assert v["status"] == "warming"
    assert v["health"]["gate"] == "ok" and v["health"]["sent_7d"] == 0


def test_task_toggle(client):
    url = "/api/warmup/inboxes/me@send.co/task"
    assert client.post(url, json={"key": "nope", "done": True}).status_code == 400
    assert client.post(url, json={"key": "dns", "done": True}).status_code == 400
    assert client.post(url, json={"key": "profile", "done": "yes"}).status_code == 400
    assert client.post(url, json={"key": "profile", "done": True}).json() == {"success": True}
    assert client.post(url, json={"key": "seed_check", "done": True}).status_code == 200
    v = _inboxes(client)[1]["me@send.co"]
    assert v["status"] == "not_started"  # toggling a task doesn't start warm-up
    done = {t["key"]: t["done"] for w in v["weeks"] for t in w["tasks"]}
    assert done["profile"] and done["seed_check"] and not done["dns"]
    client.post(url, json={"key": "profile", "done": False})
    done = {t["key"]: t["done"] for w in _inboxes(client)[1]["me@send.co"]["weeks"] for t in w["tasks"]}
    assert not done["profile"]
    assert client.post("/api/warmup/inboxes/ghost@x.co/task",
                       json={"key": "profile", "done": True}).status_code == 404


def test_update_inbox(client):
    url = "/api/warmup/inboxes/me@send.co"
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"target_daily": 500}).status_code == 400
    assert client.post(url, json={"start_date": "soon"}).status_code == 400
    assert client.post(url, json={"notes": 5}).status_code == 400
    assert client.post(url, json={"target_daily": 25, "notes": "Workspace, bought 10/1"}).json() == {"success": True}
    v = _inboxes(client)[1]["me@send.co"]
    assert v["target_daily"] == 25 and v["notes"] == "Workspace, bought 10/1"
    assert v["status"] == "not_started"
    start = (_today() - timedelta(days=7)).isoformat()
    assert client.post(url, json={"start_date": start}).status_code == 200
    v = _inboxes(client)[1]["me@send.co"]
    assert v["status"] == "warming" and v["day"] == 8 and v["current_week"] == 2
    assert v["today_cap"] == 12
    assert client.post("/api/warmup/inboxes/ghost@x.co", json={"notes": "x"}).status_code == 404


# ── DNS checks (resolver mocked: no network) ──


class FakeDNS:
    def __init__(self, records: dict, errors: set | None = None):
        self.records = records
        self.errors = errors or set()
        self.calls = 0

    def __call__(self, name, rdtype):
        self.calls += 1
        if (name, rdtype) in self.errors or name in self.errors:
            raise warmup.DNSLookupError("timed out")
        return list(self.records.get((name, rdtype), []))


GOOD = {
    ("good.co", "MX"): ["1 aspmx.l.google.com"],
    ("good.co", "TXT"): ["google-site-verification=abc", "v=spf1 include:_spf.google.com ~all"],
    ("_dmarc.good.co", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@good.co"],
    ("google._domainkey.good.co", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
}


def _checks(result):
    return {c["key"]: c for c in result["checks"]}


def test_dns_all_pass():
    result = _run(warmup.check_dns("good.co", resolve=FakeDNS(GOOD)))
    c = _checks(result)
    assert [x["key"] for x in result["checks"]] == ["mx", "spf", "dkim", "dmarc"]
    assert all(x["status"] == "pass" for x in result["checks"])
    assert "Google Workspace" in c["spf"]["detail"]
    assert "google" in c["dkim"]["detail"] and c["dkim"]["record"].startswith("google:")
    assert c["spf"]["record"].startswith("v=spf1")
    assert result["domain"] == "good.co" and result["checked_at"]
    assert warmup.dns_all_pass(result)


def test_dns_missing_records():
    c = _checks(_run(warmup.check_dns("bare.co", resolve=FakeDNS({}))))
    assert c["mx"]["status"] == "fail"
    assert c["spf"]["status"] == "fail"
    assert c["dmarc"]["status"] == "fail"
    assert c["dkim"]["status"] == "unknown"
    assert "couldn't find a dkim key at common selectors" in c["dkim"]["detail"].lower()
    assert c["spf"]["record"] is None


def test_dns_warnings():
    recs = dict(GOOD)
    recs[("warn.co", "TXT")] = ["v=spf1 include:a.com ~all", "v=spf1 include:b.com -all"]
    recs[("_dmarc.warn.co", "TXT")] = ["v=DMARC1; p=none"]
    c = _checks(_run(warmup.check_dns("warn.co", resolve=FakeDNS(recs))))
    assert c["spf"]["status"] == "warn" and "multiple" in c["spf"]["detail"].lower()
    assert c["dmarc"]["status"] == "warn" and "p=none" in c["dmarc"]["detail"]
    recs[("plus.co", "TXT")] = ["v=spf1 +all"]
    recs[("q.co", "TXT")] = ["v=spf1 include:spf.protection.outlook.com ?all"]
    assert _checks(_run(warmup.check_dns("plus.co", resolve=FakeDNS(recs))))["spf"]["status"] == "warn"
    assert _checks(_run(warmup.check_dns("q.co", resolve=FakeDNS(recs))))["spf"]["status"] == "warn"


def test_dns_network_errors_are_unknown():
    fake = FakeDNS(GOOD, errors={"good.co", "_dmarc.good.co",
                                 *[f"{s}._domainkey.good.co" for s in warmup.DKIM_SELECTORS]})
    result = _run(warmup.check_dns("good.co", resolve=fake))
    assert {c["status"] for c in result["checks"]} == {"unknown"}
    assert not warmup.dns_all_pass(result)


def test_dns_cache():
    fake = FakeDNS(GOOD)
    _run(warmup.check_dns("good.co", resolve=fake))
    first = fake.calls
    _run(warmup.check_dns("good.co", resolve=fake))
    assert fake.calls == first
    _run(warmup.check_dns("good.co", resolve=fake, use_cache=False))
    assert fake.calls == first * 2


def test_dns_endpoint(client, monkeypatch):
    recs = {(k[0].replace("good.co", "send.co"), k[1]): v for k, v in GOOD.items()}
    fake = FakeDNS(recs)
    monkeypatch.setattr(warmup, "_default_resolve", fake)
    assert client.get("/api/warmup/dns?domain=not a domain").status_code == 400
    r = client.get("/api/warmup/dns")       # defaults to the active sender's domain
    assert r.status_code == 200
    data = r.json()
    assert data["domain"] == "send.co"
    assert {c["status"] for c in data["checks"]} == {"pass"}
    assert set(data["checks"][0]) == {"key", "label", "status", "detail", "record"}
    # The 'dns' checklist item is now auto-ticked for that inbox.
    v = _inboxes(client)[1]["me@send.co"]
    assert v["weeks"][0]["tasks"][0] == {"key": "dns", "label": v["weeks"][0]["tasks"][0]["label"],
                                         "done": True}
    other = client.get("/api/warmup/dns?domain=Other.CO").json()
    assert other["domain"] == "other.co"


def test_dns_endpoint_without_sender(client, monkeypatch):
    monkeypatch.setattr(dash, "_sender_config", lambda: (None, None))
    assert client.get("/api/warmup/dns").status_code == 400


def test_dns_result_json_roundtrip(sm):
    result = _run(warmup.check_dns("good.co", resolve=FakeDNS(GOOD)))
    _run(sm.set_setting(warmup.dns_setting_key("good.co"), json.dumps(result)))
    assert warmup.dns_all_pass(_run(warmup.load_dns_result(sm, "good.co")))
