"""Deliverability health: the verdict ladder, per-domain aggregation, the
`mercury health` output and the /api/health endpoint."""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury import deliverability as dl
from mercury.config import MailboxConfig
from mercury.deliverability import BounceComposition, verdict
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from tests.test_mailboxes import make_config
from tests.test_outbox_native import FakeProvider


def _run(coro):
    return asyncio.run(coro)


NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
TODAY = NOW.date()


def _ago(days: float = 0, hours: float = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


# ── The ladder (pure) ─────────────────────────────────────────────────


def v(**kw):
    kw.setdefault("age_days", 60)
    kw.setdefault("sent", 0)
    kw.setdefault("replies", 0)
    return verdict(**kw)


def test_too_young_until_thirty_days():
    assert v(age_days=None)["verdict"] == "TOO_YOUNG"
    assert "hasn't sent" in v(age_days=None)["reason"]
    assert v(age_days=0)["verdict"] == "TOO_YOUNG"
    assert "today" in v(age_days=0)["reason"]
    assert v(age_days=29, sent=400, replies=0)["verdict"] == "TOO_YOUNG"
    assert "in 1 day" in v(age_days=29)["next"]
    assert v(age_days=30)["verdict"] == "INSUFFICIENT_DATA"
    assert "warm-up started" in v(age_days=5, age_source="warmup_start")["reason"]


def test_keep_needs_two_hundred_sends_and_one_percent():
    # A great reply rate on a small sample is still not evidence.
    assert v(sent=199, replies=20)["verdict"] == "INSUFFICIENT_DATA"
    keep = v(sent=200, replies=2)            # exactly 1%
    assert keep["verdict"] == "KEEP" and keep["tone"] == "good"
    assert keep["reply_rate"] == 0.01
    assert v(sent=200, replies=1)["verdict"] == "CANCEL_CANDIDATE"   # 0.5%
    assert "under the 1% line" in v(sent=200, replies=1)["reason"]
    assert v(sent=1000, replies=9)["verdict"] == "CANCEL_CANDIDATE"
    assert v(sent=1000, replies=10)["verdict"] == "KEEP"


def test_zero_replies_is_a_signal_only_from_150_sends():
    assert v(sent=149, replies=0)["verdict"] == "INSUFFICIENT_DATA"
    assert "not a signal yet" in v(sent=149, replies=0)["reason"]
    cancel = v(sent=150, replies=0)
    assert cancel["verdict"] == "CANCEL_CANDIDATE" and cancel["tone"] == "bad"
    assert "mercury mail placement" in cancel["next"]
    # One reply between 150 and 199: not enough sends to judge the rate.
    assert v(sent=180, replies=1)["verdict"] == "INSUFFICIENT_DATA"


def test_rates_stay_empty_until_the_sample_means_something():
    small = v(sent=49, replies=3, bounces=BounceComposition(total=10))
    assert small["bounce_rate"] is None and small["reply_rate"] is None
    assert "bounce rate needs 50" in small["reason"]
    mid = v(sent=50, replies=0, bounces=BounceComposition(total=1))
    assert mid["bounce_rate"] == 0.02 and mid["reply_rate"] is None
    assert v(sent=200, replies=4)["reply_rate"] == 0.02


def test_unclassified_bounces_flag_but_never_cancel():
    r = v(sent=300, replies=6, bounces=BounceComposition(total=30), max_bounce_rate=0.05)
    assert r["verdict"] == "KEEP"
    assert r["flags"] and "10.0%" in r["flags"][0]["text"] and r["flags"][0]["tone"] == "bad"
    assert v(sent=300, replies=6, bounces=BounceComposition(total=3))["flags"] == []
    # Under 50 sends a bounce rate isn't computed, so it can't flag either.
    assert v(sent=40, bounces=BounceComposition(total=20))["flags"] == []


def test_a_burned_bounce_code_cancels_even_a_young_domain():
    burned = BounceComposition(total=3, classified=True, buckets={"BURNED": 1, "LIST": 2})
    r = v(age_days=10, sent=40, replies=5, bounces=burned)
    assert r["verdict"] == "CANCEL_CANDIDATE" and "5.7.6xx" in r["reason"]
    # Classified list bounces alone are not a burned domain. The total comes
    # from the metrics counts, which already leave NOISE out.
    listy = BounceComposition(total=2, classified=True, buckets={"LIST": 2, "NOISE": 2})
    assert not listy.burned and listy.counted == 2
    assert v(sent=300, replies=6, bounces=listy)["verdict"] == "KEEP"


def test_bounce_composition_unclassified_without_bucketed_bounces(sm):
    # No bounces, or only ones logged before classification: never burned.
    pid = _send(sm, 1, "a@x.co")[0]
    _event(sm, "bounce", pid, days_ago=1, mailbox="a@x.co")
    comp = _run(dl.bounce_composition(sm.db_path, {"a@x.co"}, _ago(30), 1))
    assert comp.total == 1 and not comp.classified and not comp.burned and comp.counted == 1


def test_bounce_composition_reads_the_handlers_buckets_per_domain(sm):
    pids = _send(sm, 3, "a@x.co") + _send(sm, 1, "b@other.co")
    for pid, bucket in zip(pids, ["BURNED", "LIST", "NOISE", "BURNED"]):
        _run(sm.log_action("bounce", "handler", {"prospect_id": pid, "bucket": bucket},
                           created_at=_ago(1)))
    comp = _run(dl.bounce_composition(sm.db_path, {"a@x.co"}, _ago(30), 2))
    assert comp.classified and comp.burned
    assert comp.buckets == {"BURNED": 1, "LIST": 1, "NOISE": 1}
    assert comp.counted == 2
    # The other domain's burned bounce is not charged here, nor an old one.
    clean = _run(dl.bounce_composition(sm.db_path, {"a@x.co"}, _ago(0.5), 0))
    assert not clean.classified and not clean.burned


def test_copy_has_no_em_dashes():
    cases = [v(age_days=None), v(age_days=3), v(sent=10), v(sent=120), v(sent=120, replies=2),
             v(sent=160), v(sent=250, replies=1), v(sent=250, replies=9),
             v(sent=300, replies=6, bounces=BounceComposition(total=40))]
    for c in cases:
        text = " ".join([c["reason"], c["next"], c["label"]] + [f["text"] for f in c["flags"]])
        assert "—" not in text and "–" not in text


# ── Aggregation per domain ────────────────────────────────────────────


@pytest.fixture
def sm(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    _run(s.init_db())
    return s


_n = [0]


def _send(sm, n, mailbox, days_ago=0.0, kind="sequence"):
    """n sent outreach emails from ``mailbox`` ``days_ago`` days ago."""
    pids = []
    for _ in range(n):
        _n[0] += 1
        email = f"lead{_n[0]}@acme.co"
        pid = _run(sm.add_prospect(Prospect(first_name="Pat", last_name="Lee", email=email,
                                             email_status="verified", status="contacted")))
        when = _ago(days_ago)
        item = _run(sm.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s", body="b", send_at=when,
            status="approved", mailbox=mailbox, step=1, kind=kind,
            campaign_id="" if kind == "reply" else f"c-{pid}"))
        _run(sm.update_outbox_item(item, status="sent", sent_at=when))
        pids.append(pid)
    return pids


def _event(sm, kind, pid, days_ago=0.0, mailbox=None, intent="interested"):
    details = {"prospect_id": pid}
    if mailbox is not None:
        details["mailbox"] = mailbox
    if kind == "reply_received":
        details["intent"] = intent
    _run(sm.log_action(kind, "handler", details, created_at=_ago(days_ago)))


def _pool(*specs):
    return MailboxPool([Mailbox(email=e, provider=FakeProvider(), daily_cap=30, warmup_start=w)
                        for e, w in specs])


def _report(sm, pool, **cfg):
    return _run(dl.domain_report(sm, make_config(**cfg), pool, now=NOW))


def test_domains_add_up_their_mailboxes_over_each_window(sm):
    pool = _pool(("a@x.co", None), ("b@x.co", None), ("c@y.co", None))
    a = _send(sm, 3, "a@x.co", days_ago=2)
    _send(sm, 4, "b@x.co", days_ago=10)
    _send(sm, 5, "b@x.co", days_ago=20)
    _send(sm, 6, "c@y.co", days_ago=40)                    # outside every window
    _send(sm, 2, "a@x.co", days_ago=1, kind="reply")       # Mercury's replies aren't outreach
    _event(sm, "reply_received", a[0], days_ago=1)
    _event(sm, "reply_received", a[1], days_ago=1, intent="ooo")   # auto-reply: not a reply
    _event(sm, "bounce", a[2], days_ago=1)                  # no mailbox: traced through the outbox
    rep = _report(sm, pool)
    x = next(d for d in rep["domains"] if d["domain"] == "x.co")
    assert x["windows"]["7d"] == {"sent": 3, "bounces": 1, "replies": 1}
    assert x["windows"]["14d"] == {"sent": 7, "bounces": 1, "replies": 1}
    assert x["windows"]["30d"] == {"sent": 12, "bounces": 1, "replies": 1}
    assert x["mailboxes"] == ["a@x.co", "b@x.co"] and x["configured"]
    y = next(d for d in rep["domains"] if d["domain"] == "y.co")
    assert y["windows"]["30d"]["sent"] == 0
    assert y["age_days"] == 40 and y["age_source"] == "first_send"
    assert y["verdict"] == "INSUFFICIENT_DATA"
    assert x["age_days"] == 20 and x["verdict"] == "TOO_YOUNG"


def test_legacy_rows_belong_to_the_legacy_mailbox_domain(sm):
    pool = _pool(("a@x.co", None), ("b@y.co", None))        # a is legacy
    pids = _send(sm, 5, "", days_ago=3)
    _event(sm, "reply_received", pids[0], days_ago=2)
    rep = _report(sm, pool)
    x = next(d for d in rep["domains"] if d["domain"] == "x.co")
    assert x["windows"]["7d"] == {"sent": 5, "bounces": 0, "replies": 1}


def test_untraceable_events_are_not_charged_to_any_domain(sm):
    pool = _pool(("a@x.co", None))
    _send(sm, 2, "a@x.co", days_ago=1)
    _run(sm.log_action("bounce", "handler", {"prospect": "ghost@nowhere.co"}, created_at=_ago(1)))
    rep = _report(sm, pool)
    assert rep["unattributed"]["bounces"] == 1
    assert rep["domains"][0]["windows"]["7d"]["bounces"] == 0


def test_a_removed_mailbox_still_counts_toward_its_domain(sm):
    pool = _pool(("a@x.co", None))
    _send(sm, 3, "old@z.co", days_ago=35)
    _send(sm, 2, "old@z.co", days_ago=2)
    rep = _report(sm, pool)
    z = next(d for d in rep["domains"] if d["domain"] == "z.co")
    assert z["mailboxes"] == ["old@z.co"] and not z["configured"]
    assert z["windows"]["30d"]["sent"] == 2 and z["age_days"] == 35


def test_age_is_the_earlier_of_first_send_and_warmup_start(sm):
    pool = _pool(("a@x.co", TODAY - timedelta(days=45)),       # started before its first send
                 ("b@y.co", TODAY + timedelta(days=3)))         # scheduled: not started
    _send(sm, 1, "a@x.co", days_ago=10)
    rep = _report(sm, pool)
    by = {d["domain"]: d for d in rep["domains"]}
    assert by["x.co"]["age_days"] == 45 and by["x.co"]["age_source"] == "warmup_start"
    assert by["y.co"]["age_days"] is None and by["y.co"]["verdict"] == "TOO_YOUNG"


def test_verdict_uses_the_thirty_day_window_and_sorts_worst_first(sm):
    pool = _pool(("a@good.co", None), ("b@dead.co", None), ("c@new.co", TODAY - timedelta(days=5)))
    good = _send(sm, 200, "a@good.co", days_ago=5)
    _send(sm, 1, "a@good.co", days_ago=50)                      # makes it old enough
    for pid in good[:2]:
        _event(sm, "reply_received", pid, days_ago=3)
    _send(sm, 160, "b@dead.co", days_ago=5)
    _send(sm, 1, "b@dead.co", days_ago=60)
    rep = _report(sm, pool)
    assert [d["domain"] for d in rep["domains"]] == ["dead.co", "new.co", "good.co"]
    assert [d["verdict"] for d in rep["domains"]] == ["CANCEL_CANDIDATE", "TOO_YOUNG", "KEEP"]
    assert rep["thresholds"]["min_sends_reply"] == 200 and rep["window_days"] == 30
    assert rep["bounces_classified"] is False


def test_single_inbox_owns_every_row(sm):
    pool = MailboxPool.single(FakeProvider(), 50, "me@send.co")
    _send(sm, 2, "", days_ago=1)
    _send(sm, 3, "me@send.co", days_ago=1)
    rep = _report(sm, pool)
    assert [d["domain"] for d in rep["domains"]] == ["send.co"]
    assert rep["domains"][0]["windows"]["7d"]["sent"] == 5


def test_without_a_native_pool_the_outbox_still_reports(sm):
    _send(sm, 2, "", days_ago=1)
    rep = _run(dl.domain_report(sm, make_config(provider="instantly"), None, now=NOW))
    # '' rows fall back to persona.email (carlos@main.co in make_config).
    assert rep["domains"][0]["domain"] == "main.co" and rep["native"] is False


def test_max_bounce_rate_comes_from_config(sm):
    pool = _pool(("a@x.co", None))
    pids = _send(sm, 60, "a@x.co", days_ago=2)
    _send(sm, 1, "a@x.co", days_ago=45)
    for pid in pids[:2]:
        _event(sm, "bounce", pid, days_ago=1, mailbox="a@x.co")
    assert _report(sm, pool, max_bounce_rate=0.05)["domains"][0]["flags"] == []
    flagged = _report(sm, pool, max_bounce_rate=0.02)["domains"][0]["flags"]
    assert flagged and "over the 2.0% limit" in flagged[0]["text"]


# ── mercury health ────────────────────────────────────────────────────


def test_format_report_lists_counts_verdicts_and_thresholds(sm):
    pool = _pool(("a@x.co", None))
    pids = _send(sm, 160, "a@x.co", days_ago=3)
    _send(sm, 1, "a@x.co", days_ago=40)
    _event(sm, "bounce", pids[0], days_ago=1, mailbox="a@x.co")
    text = dl.format_report(_report(sm, pool))
    assert "x.co" in text and "Cancel candidate" in text
    assert "Last 7 days" in text and "Last 14 days" in text and "Last 30 days" in text
    assert "0 replies on 160 sends" in text
    assert "Keep: replies at 1% or more after 200 sends." in text
    assert "carries an SMTP code yet" in text
    assert "No placement test yet" in text
    assert "—" not in text


def test_format_report_without_domains():
    text = dl.format_report({"domains": []})
    assert "No sending domain yet" in text


def test_cli_health_prints_the_report(sm, monkeypatch, capsys):
    import mercury.cli as cli
    import mercury.config as config_mod
    import mercury.state as state_mod

    pool = _pool(("a@x.co", None))
    _send(sm, 3, "a@x.co", days_ago=1)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config())
    monkeypatch.setattr(config_mod, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(MailboxPool, "from_config", classmethod(lambda cls, c, e: pool))
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: sm)

    monkeypatch.setattr(sys, "argv", ["mercury", "health"])
    cli.main()
    out = capsys.readouterr().out
    assert "Deliverability health" in out and "x.co" in out and "Too young" in out

    monkeypatch.setattr(sys, "argv", ["mercury", "health", "--json"])
    cli.main()
    data = json.loads(capsys.readouterr().out)
    assert data["domains"][0]["domain"] == "x.co"
    assert data["domains"][0]["windows"]["7d"]["sent"] == 3
    assert data["placement"] is None


# ── /api/health ───────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, sm):
    cfg = make_config(provider="smtp", mailboxes=[MailboxConfig(email="a@x.co"),
                                                  MailboxConfig(email="b@y.co")])
    pool = _pool(("a@x.co", None), ("b@y.co", None))
    monkeypatch.setattr(dash, "_mail_context", lambda: (cfg, pool))
    monkeypatch.setattr(dash, "_state", lambda: sm)
    monkeypatch.setattr(dash, "DB_PATH", Path(sm.db_path))
    with TestClient(dash.app) as c:
        yield c


def test_health_endpoint(client, sm):
    _send(sm, 160, "b@y.co", days_ago=3)
    _send(sm, 1, "b@y.co", days_ago=45)
    _send(sm, 4, "a@x.co", days_ago=2)
    data = client.get("/api/health").json()
    assert [d["domain"] for d in data["domains"]] == ["y.co", "x.co"]
    y = data["domains"][0]
    assert y["verdict"] == "CANCEL_CANDIDATE" and y["tone"] == "bad" and y["label"] == "Cancel candidate"
    assert y["windows"]["14d"]["sent"] == 160 and y["reply_rate"] is None
    assert data["placement"] is None
    assert any("Keep:" in t for t in data["thresholds_text"])
    # Today raises the cancel candidate in Needs you.
    items = client.get("/api/today").json()["items"]
    item = next(i for i in items if i["key"] == "deliverability")
    assert "y.co" in item["title"] and item["tab"] == "mailboxes"


def test_health_endpoint_without_mail_config(client, monkeypatch, sm):
    def boom():
        raise RuntimeError("bad yaml")
    monkeypatch.setattr(dash, "_mail_context", boom)
    import mercury.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config(provider="instantly"))
    data = client.get("/api/health").json()
    assert data["domains"] == [] and data["native"] is False


def test_today_has_no_deliverability_item_without_evidence(client, sm):
    _send(sm, 20, "a@x.co", days_ago=1)
    items = client.get("/api/today").json()["items"]
    assert not any(i["key"] == "deliverability" for i in items)
