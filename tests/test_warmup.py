"""Inbox warm-up: health gates on top of the mailbox ramp, sender
enforcement through MailboxPool, the overlay endpoints, and DNS checks."""

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury import warmup
from mercury.config import MailboxConfig
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from tests.test_mailboxes import make_config
from tests.test_outbox_native import FakeProvider, make_handler


def _run(coro):
    return asyncio.run(coro)


def _today() -> date:
    # make_config's quiet-hours timezone is UTC, so local_today() == UTC today.
    return datetime.now(timezone.utc).date()


def _now_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + delta).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _clean_dns_cache():
    warmup.clear_dns_cache()
    yield
    warmup.clear_dns_cache()


def make_pool(*specs, initial=5, weekly=5):
    """specs: (email, daily_cap, warmup_start)."""
    return MailboxPool(
        [Mailbox(email=e, provider=FakeProvider(), daily_cap=c, warmup_start=w)
         for e, c, w in specs],
        warmup_initial_cap=initial, warmup_weekly_increase=weekly)


# ── Pure gate / ramp phrasing ──


def test_health_gate_thresholds():
    assert warmup.health_gate(19, 10)[0] == "ok"
    assert "not enough sends" in warmup.health_gate(19, 10)[1]
    assert warmup.health_gate(100, 3)[0] == "ok"      # exactly 3% → ok
    assert warmup.health_gate(100, 4)[0] == "hold"
    assert warmup.health_gate(100, 5)[0] == "hold"    # exactly 5% → hold
    assert warmup.health_gate(100, 6)[0] == "pause"
    assert warmup.health_gate(20, 2)[0] == "pause"


def test_week_ranges_follow_the_configured_ramp():
    # 5/day, +5 a week, up to 30: weeks 1-5 ramp, full volume from week 6.
    assert warmup.ramp_weeks(30, 5, 5) == 5
    assert [warmup.week_range(w, 30, 5, 5) for w in range(6)] == [
        "no cold email yet", "5/day", "10/day", "15/day", "20-25/day", "30/day"]
    # A short ramp clamps at the cap; a mailbox at/below the initial cap never ramps.
    assert warmup.week_range(3, 12, 5, 5) == "12/day"
    assert warmup.week_range(4, 12, 5, 5) == "12/day"
    assert warmup.ramp_weeks(5, 5, 5) == 0
    weeks = warmup.build_weeks({"profile": True}, True, 30, 5, 5)
    assert [w["week"] for w in weeks] == [0, 1, 2, 3, 4, 5]
    done = {t["key"]: t["done"] for w in weeks for t in w["tasks"]}
    assert done["dns"] and done["profile"] and not done["seed_check"]


# ── Gates folded into the pool's caps ──


def test_pool_cap_on_applies_gates():
    today = _today()
    pool = make_pool(("a@x.co", 30, today - timedelta(days=7)),   # week 2: 10/day
                     ("b@y.co", 30, today),                       # day 1: 5/day
                     ("c@z.co", 30, None))                        # warm
    a, b, c = pool.mailboxes
    assert [pool.cap_on(m, today) for m in (a, b, c)] == [10, 5, 30]
    pool.gates = {"a@x.co": "hold", "b@y.co": "hold", "c@z.co": "hold"}
    # hold = yesterday's ramp cap; never below day one; a warm inbox keeps its cap
    assert [pool.cap_on(m, today) for m in (a, b, c)] == [5, 5, 30]
    pool.gates = {"a@x.co": "paused"}
    assert pool.cap_on(a, today) == 0 and pool.base_cap_on(a, today) == 10
    assert pool.remaining({}, today) == {"a@x.co": 0, "b@y.co": 5, "c@z.co": 30}


# ── State-backed health ──


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


def _sent(sm, n, mailbox="", when=None, kind="sequence"):
    pids = []
    for _ in range(n):
        pid, email = _prospect(sm, status="contacted")
        item = _run(sm.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s", body="b",
            send_at=when or _now_iso(), status="approved", mailbox=mailbox,
            campaign_id="" if kind == "reply" else f"c-{pid}", step=1, kind=kind))
        _run(sm.update_outbox_item(item, status="sent", sent_at=when or _now_iso()))
        pids.append(pid)
    return pids


def _queue(sm, n, kind="sequence", mailbox=""):
    for _ in range(n):
        pid, email = _prospect(sm, status="replied" if kind == "reply" else "queued")
        _run(sm.add_outbox_item(
            prospect_id=pid, to_email=email, subject="hello",
            body="Fine body. Question?", send_at=_now_iso(timedelta(minutes=-5)),
            status="approved", campaign_id="" if kind == "reply" else f"q-{pid}",
            step=1, kind=kind, mailbox=mailbox))


def _bounce(sm, pid, mailbox=None, when=None):
    details = {"prospect_id": pid}
    if mailbox is not None:
        details["mailbox"] = mailbox
    _run(sm.log_action("bounce", "handler", details, created_at=when))


def test_bounces_pause_only_the_mailbox_they_came_from(sm):
    start = _today() - timedelta(days=30)
    pool = make_pool(("a@x.co", 30, start), ("b@y.co", 30, start))
    a_pids = _sent(sm, 20, mailbox="a@x.co")
    _sent(sm, 20, mailbox="b@y.co")
    _bounce(sm, a_pids[0], "a@x.co")
    _bounce(sm, a_pids[1])             # no mailbox on the event: found via the outbox
    health = _run(warmup.apply_health(sm, pool))
    assert health["a@x.co"]["gate"] == "pause" and health["a@x.co"]["status"] == "paused"
    assert health["a@x.co"]["bounces"] == 2 and health["a@x.co"]["sent_7d"] == 20
    assert health["b@y.co"]["gate"] == "ok" and health["b@y.co"]["status"] == "active"
    assert pool.gates == {"a@x.co": "paused"}
    row = _run(sm.get_warmup_inbox("a@x.co"))
    assert row["status"] == "paused" and row["paused_at"] and "bounce rate" in row["pause_reason"]
    # The pause sticks even after the bounces age out — it needs a human.
    _run(sm.update_warmup_inbox("a@x.co", pause_reason="kept"))
    assert _run(warmup.apply_health(sm, pool))["a@x.co"]["reason"] == "kept"


def test_pre_rotation_bounces_are_traced_by_address_not_blamed_on_legacy(sm):
    """Production bounces logged before rotation carry only {"prospect": email}.
    They must follow the email to the mailbox that sent it; an untraceable one
    must not be charged to whichever mailbox owns '' rows."""
    pool = make_pool(("a@x.co", 30, None), ("b@y.co", 30, None))   # a is legacy
    _sent(sm, 30, mailbox="a@x.co")
    b_pids = _sent(sm, 30, mailbox="b@y.co")
    b_emails = [_run(sm.get_prospect(p)).email for p in b_pids[:3]]
    for email in b_emails:                                         # 10% of b's sends
        _run(sm.log_action("bounce", "handler", {"prospect": email}))
    for i in range(3):                                             # matches no send at all
        _run(sm.log_action("bounce", "handler", {"prospect": f"ghost{i}@nowhere.co"}))
    health = _run(warmup.apply_health(sm, pool, persist=False))
    assert health["b@y.co"]["bounces"] == 3 and health["b@y.co"]["gate"] == "pause"
    assert health["a@x.co"]["bounces"] == 0 and health["a@x.co"]["gate"] == "ok"


def test_hold_between_three_and_five_percent(sm):
    pool = make_pool(("a@x.co", 30, _today() - timedelta(days=7)))
    pids = _sent(sm, 25, mailbox="a@x.co")
    _bounce(sm, pids[0], "a@x.co")                       # 4%
    health = _run(warmup.apply_health(sm, pool))
    assert health["a@x.co"]["gate"] == "hold" and health["a@x.co"]["status"] == "active"
    assert pool.cap_on(pool.mailboxes[0], _today()) == 5   # week 2 held at week 1


def test_legacy_mailbox_owns_untracked_rows_and_old_sends_age_out(sm):
    pool = make_pool(("a@x.co", 30, None), ("b@y.co", 30, None))   # a is legacy
    pids = _sent(sm, 20)                                           # mailbox ''
    for pid in pids[:2]:
        _bounce(sm, pid)
    old = _now_iso(timedelta(days=-8))
    _sent(sm, 30, mailbox="b@y.co", when=old)
    health = _run(warmup.apply_health(sm, pool, persist=False))
    assert health["a@x.co"]["sent_7d"] == 20 and health["a@x.co"]["gate"] == "pause"
    assert health["b@y.co"]["sent_7d"] == 0
    assert _run(sm.get_warmup_inbox("a@x.co")) is None            # persist=False


def test_resume_restarts_the_health_window(sm):
    pool = make_pool(("a@x.co", 30, None))
    pids = _sent(sm, 20, mailbox="a@x.co", when=_now_iso(timedelta(hours=-2)))
    for pid in pids[:3]:
        _bounce(sm, pid, "a@x.co", when=_now_iso(timedelta(hours=-1)))
    assert _run(warmup.apply_health(sm, pool))["a@x.co"]["status"] == "paused"
    _run(warmup.set_resumed(sm, "a@x.co"))
    h = _run(warmup.apply_health(sm, pool))["a@x.co"]
    assert h["status"] == "active" and h["gate"] == "ok" and h["sent_7d"] == 0
    assert pool.gates == {}


# ── Sender enforcement ──


def _sender(sm, pool, **cfg):
    from tests.test_mailboxes import make_sender
    return make_sender(sm, pool, **cfg)


def _sent_from(pool):
    return {mb.email: len(mb.provider.sent) for mb in pool.mailboxes}


def test_sender_skips_a_paused_mailbox_for_cold_mail(sm):
    pool = make_pool(("a@x.co", 30, None), ("b@y.co", 30, None))
    _run(warmup.set_paused(sm, "a@x.co", "paused manually"))
    _queue(sm, 6)
    _run(_sender(sm, pool)._drain_due())
    assert _sent_from(pool) == {"a@x.co": 0, "b@y.co": 6}


def test_paused_mailbox_holds_follow_ups_but_still_answers_replies(sm):
    pool = make_pool(("a@x.co", 30, None), ("b@y.co", 30, None))
    _run(warmup.set_paused(sm, "a@x.co", "paused manually"))
    _queue(sm, 2, mailbox="a@x.co")                  # pinned cold mail on a
    _queue(sm, 1, kind="reply", mailbox="a@x.co")    # someone wrote back to a
    _run(_sender(sm, pool)._drain_due())
    assert _sent_from(pool) == {"a@x.co": 1, "b@y.co": 0}
    held = _run(sm.get_outbox(status="approved"))
    assert len(held) == 2 and all(r["kind"] == "sequence" for r in held)   # held, not cancelled


def test_auto_pause_stops_a_single_inbox(sm):
    # Gmail / one SMTP mailbox: MailboxPool.single covers it.
    provider = FakeProvider()
    pool = MailboxPool.single(provider, 50, "me@send.co")
    pids = _sent(sm, 20, mailbox="me@send.co", when=_now_iso(timedelta(hours=-30)))
    for pid in pids[:2]:
        _bounce(sm, pid)
    _queue(sm, 3)
    _run(_sender(sm, pool)._drain_due())
    assert provider.sent == []
    assert _run(sm.get_warmup_inbox("me@send.co"))["status"] == "paused"


def test_ramp_still_caps_and_gate_only_lowers(sm):
    pool = make_pool(("a@x.co", 30, _today()))       # day 1: 5
    _queue(sm, 8)
    _run(_sender(sm, pool)._drain_due())
    assert _sent_from(pool) == {"a@x.co": 5}


def test_bounce_event_records_the_sending_mailbox(sm):
    pid, email = _prospect(sm, status="contacted")
    item = _run(sm.add_outbox_item(prospect_id=pid, to_email=email, subject="s", body="b",
                                   send_at=_now_iso(), status="approved", campaign_id="c1",
                                   step=1, mailbox="b@y.co"))
    _run(sm.update_outbox_item(item, status="sent", message_id="<m9@x>", sent_at=_now_iso()))
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="bx", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)", body="nope",
        in_reply_to="<m9@x>", is_bounce=True)]
    _run(make_handler(sm, provider)._run_native())
    rows = sqlite3.connect(sm.db_path).execute(
        "SELECT details_json FROM actions WHERE action_type = 'bounce'").fetchall()
    assert json.loads(rows[0][0])["mailbox"] == "b@y.co"


# ── Endpoints ──


@pytest.fixture
def client(monkeypatch, sm):
    today = _today()
    cfg = make_config(provider="smtp", max_daily_sends=100,
                      mailboxes=[MailboxConfig(email=e) for e in ("a@x.co", "b@y.co", "c@z.co")])
    pool = make_pool(("a@x.co", 30, today - timedelta(days=8)),    # week 2
                     ("b@y.co", 20, today + timedelta(days=3)),    # scheduled
                     ("c@z.co", 40, None))                         # warm
    monkeypatch.setattr(dash, "_mail_context", lambda: (cfg, pool))
    monkeypatch.setattr(dash, "_state", lambda: sm)
    monkeypatch.setattr(dash, "DB_PATH", Path(sm.db_path))
    with TestClient(dash.app) as c:
        c.sm, c.pool, c.cfg = sm, pool, cfg
        yield c


def _inboxes(client):
    data = client.get("/api/warmup").json()
    return data, {i["email"]: i for i in data["inboxes"]}


def test_warmup_overview_comes_from_the_mailbox_config(client):
    _sent(client.sm, 3, mailbox="a@x.co")
    _sent(client.sm, 2, mailbox="a@x.co", when=_now_iso(timedelta(days=-2)))
    data, ib = _inboxes(client)
    assert data["active_email"] == "a@x.co" and data["rotation"] is True
    assert list(ib) == ["a@x.co", "b@y.co", "c@z.co"]
    assert "mailboxes:" in data["config_hint"]
    a = ib["a@x.co"]
    assert a["primary"] and a["is_sender"] and a["stage"] == "warming"
    assert a["status"] == "active" and a["day"] == 9 and a["current_week"] == 2
    assert a["target_daily"] == 30 and a["today_cap"] == 10 and a["sent_today"] == 3
    assert a["remaining"] == 7
    assert a["full_on"] == (_today() - timedelta(days=8) + timedelta(days=35)).isoformat()
    assert len(a["plan"]) == 36 and a["plan"][0]["cap"] == 5 and a["plan"][-1]["cap"] == 30
    assert a["plan"][8]["sent"] == 3 and a["plan"][6]["sent"] == 2 and a["plan"][9]["sent"] is None
    assert [w["range"] for w in a["weeks"]][1:3] == ["5/day", "10/day"]
    assert set(a["health"]) == {"sent_7d", "bounce_rate", "reply_rate", "gate", "reason"}
    b = ib["b@y.co"]
    assert b["stage"] == "scheduled" and b["day"] is None and b["current_week"] == 0
    assert b["today_cap"] == 0 and b["plan"][0]["sent"] is None
    c = ib["c@z.co"]
    assert c["stage"] == "warm" and c["start_date"] is None and c["plan"] == []
    assert c["current_week"] == 5 and c["today_cap"] == 40


def test_single_inbox_without_rotation(client, monkeypatch):
    cfg = make_config(provider="gmail", max_daily_sends=25)
    pool = MailboxPool.single(FakeProvider(), 25, "me@send.co")
    monkeypatch.setattr(dash, "_mail_context", lambda: (cfg, pool))
    data, ib = _inboxes(client)
    assert data["rotation"] is False and data["single_inbox"] is True
    assert list(ib) == ["me@send.co"]
    assert ib["me@send.co"]["stage"] == "warm" and ib["me@send.co"]["today_cap"] == 25


def test_instantly_has_no_warmup_inboxes(client, monkeypatch):
    monkeypatch.setattr(dash, "_mail_context", lambda: (make_config(provider="instantly"), None))
    data = client.get("/api/warmup").json()
    assert data["inboxes"] == [] and "Instantly" in data["note"]


def test_pause_and_resume_actions(client):
    url = "/api/warmup/inboxes/a@x.co/action"
    assert client.post(url, json={"action": "explode"}).status_code == 400
    for gone in ("start", "reset", "remove"):
        assert client.post(url, json={"action": gone}).status_code == 400
    assert client.post(url, json={"action": "resume"}).status_code == 400
    assert client.post(url, json={"action": "pause"}).json() == {"success": True}
    assert client.post(url, json={"action": "pause"}).status_code == 400
    a = _inboxes(client)[1]["a@x.co"]
    assert a["status"] == "paused" and a["stage"] == "paused" and a["today_cap"] == 0
    assert "manually" in a["health"]["reason"]
    # The Outbox capacity card agrees with the sender.
    mb = {m["email"]: m for m in client.get("/api/mailboxes").json()["mailboxes"]}
    assert mb["a@x.co"]["cap_today"] == 0 and mb["a@x.co"]["gate"] == "paused"
    assert client.post(url, json={"action": "resume"}).status_code == 200
    a = _inboxes(client)[1]["a@x.co"]
    assert a["status"] == "active" and a["today_cap"] == 10
    assert client.post("/api/warmup/inboxes/ghost@x.co/action",
                       json={"action": "pause"}).status_code == 404


def test_config_owned_fields_are_not_editable(client):
    assert client.post("/api/warmup/inboxes", json={"email": "new@box.co"}).status_code in (404, 405)
    url = "/api/warmup/inboxes/a@x.co"
    assert client.post(url, json={"target_daily": 25}).status_code == 400
    assert client.post(url, json={"start_date": "2026-01-01"}).status_code == 400
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"notes": "Workspace, bought 10/1"}).json() == {"success": True}
    assert _inboxes(client)[1]["a@x.co"]["notes"] == "Workspace, bought 10/1"
    assert client.post("/api/warmup/inboxes/ghost@x.co", json={"notes": "x"}).status_code == 404


def test_task_toggle(client):
    url = "/api/warmup/inboxes/b@y.co/task"
    assert client.post(url, json={"key": "nope", "done": True}).status_code == 400
    assert client.post(url, json={"key": "dns", "done": True}).status_code == 400
    assert client.post(url, json={"key": "profile", "done": "yes"}).status_code == 400
    assert client.post(url, json={"key": "profile", "done": True}).json() == {"success": True}
    b = _inboxes(client)[1]["b@y.co"]
    assert b["status"] == "active" and b["stage"] == "scheduled"
    done = {t["key"]: t["done"] for w in b["weeks"] for t in w["tasks"]}
    assert done["profile"] and not done["dns"]
    client.post(url, json={"key": "profile", "done": False})
    done = {t["key"]: t["done"] for w in _inboxes(client)[1]["b@y.co"]["weeks"] for t in w["tasks"]}
    assert not done["profile"]


def test_calendar_items_carry_the_from_mailbox(client):
    pid, email = _prospect(client.sm, status="contacted")
    item = _run(client.sm.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b", send_at=_now_iso(),
        status="approved", campaign_id="cal", step=1))
    _run(client.sm.update_outbox_item(item, status="sent", sent_at=_now_iso(), mailbox="c@z.co"))
    start, end = _today().isoformat(), (_today() + timedelta(days=1)).isoformat()
    items = client.get(f"/api/calendar?start={start}&end={end}").json()["items"]
    assert [i["mailbox"] for i in items] == ["c@z.co"]


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


def test_dns_result_json_roundtrip(sm):
    result = _run(warmup.check_dns("good.co", resolve=FakeDNS(GOOD)))
    _run(sm.set_setting(warmup.dns_setting_key("good.co"), json.dumps(result)))
    assert warmup.dns_all_pass(_run(warmup.load_dns_result(sm, "good.co")))


def test_dns_endpoint(client, monkeypatch):
    recs = {(k[0].replace("good.co", "x.co"), k[1]): v for k, v in GOOD.items()}
    monkeypatch.setattr(warmup, "_default_resolve", FakeDNS(recs))
    assert client.get("/api/warmup/dns?domain=not a domain").status_code == 400
    data = client.get("/api/warmup/dns").json()     # defaults to the primary mailbox's domain
    assert data["domain"] == "x.co"
    assert {c["status"] for c in data["checks"]} == {"pass"}
    assert set(data["checks"][0]) == {"key", "label", "status", "detail", "record"}
    a = _inboxes(client)[1]["a@x.co"]
    assert a["weeks"][0]["tasks"][0]["key"] == "dns" and a["weeks"][0]["tasks"][0]["done"] is True
    assert client.get("/api/warmup/dns?domain=Other.CO").json()["domain"] == "other.co"


def test_dns_endpoint_without_mail_config(client, monkeypatch):
    monkeypatch.setattr(dash, "_mail_context", lambda: (make_config(provider="instantly"), None))
    assert client.get("/api/warmup/dns").status_code == 400
