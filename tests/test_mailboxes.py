"""Mailbox rotation: caps, warm-up, thread pinning, follow-up approval,
pacing, and reading replies from every inbox."""

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.sender import spread_budget
from harvey.config import EmailChannelConfig, EnvConfig, MailboxConfig, ProductConfig
from harvey.integrations.mail_provider import InboundMessage
from harvey.integrations.mailboxes import (
    Mailbox,
    MailboxPool,
    planned_daily_capacity,
    warmup_cap,
)
from harvey.integrations.smtp_mail import SmtpImapProvider
from harvey.state import StateManager
from tests.test_outbox_native import FakeProvider, StubBrain, seed_prospect


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def make_config(**email_overrides):
    email = dict(
        enabled=True, provider="smtp", max_daily_sends=100, send_to_risky=False,
        require_approval=True, max_bounce_rate=0.05, mailboxes=[],
        warmup_initial_cap=5, warmup_weekly_increase=5,
        auto_approve_followups=False, spread_sends=False,
    )
    email.update(email_overrides)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Carlos", email="carlos@main.co", company="EBSY",
                                role="BD", tone="direct"),
        channels=SimpleNamespace(email=SimpleNamespace(**email),
                                 linkedin=SimpleNamespace(enabled=False)),
        product=ProductConfig(name="P", description="d", pricing="$",
                              key_benefits=["b"], objection_responses={}),
        compliance=SimpleNamespace(postal_address="1 Main St",
                                   opt_out_line_en="Reply unsubscribe.",
                                   opt_out_line_es="Responde baja."),
        usage=SimpleNamespace(heartbeat_interval_minutes=15,
                              quiet_hours=SimpleNamespace(start="22:00", end="07:00",
                                                          timezone="UTC")),
    )


def make_pool(*specs, initial=5, weekly=5):
    """specs: (email, daily_cap, warmup_start) -> pool of FakeProviders."""
    boxes = [Mailbox(email=e, provider=FakeProvider(), daily_cap=c, warmup_start=w)
             for e, c, w in specs]
    return MailboxPool(boxes, warmup_initial_cap=initial, warmup_weekly_increase=weekly)


def make_sender(state, pool, **email_overrides):
    from harvey.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=make_config(**email_overrides),
                    env=SimpleNamespace(instantly_api_key=""))
    sender.mailboxes = pool
    sender.provider = pool.primary.provider
    sender.send_pacing = False
    return sender


async def queue(state, prospect_id, to, step=1, status="approved", campaign="c1",
                mailbox="", kind="sequence"):
    return await state.add_outbox_item(
        prospect_id=prospect_id, campaign_id=campaign if kind == "sequence" else "",
        step=step, to_email=to, subject=f"s{step}", body=f"Hi, step {step}.",
        send_at=_now_iso(), status=status, mailbox=mailbox, kind=kind,
    )


# ── caps and warm-up ──

def test_warmup_cap_ramps_weekly_and_stops_at_the_daily_cap():
    start = date(2026, 10, 7)
    assert warmup_cap(30, None, start, 5, 5) == 30                       # already warm
    assert warmup_cap(30, start, start - timedelta(days=1), 5, 5) == 0  # not started
    assert warmup_cap(30, start, start, 5, 5) == 5
    assert warmup_cap(30, start, start + timedelta(days=6), 5, 5) == 5
    assert warmup_cap(30, start, start + timedelta(days=7), 5, 5) == 10
    assert warmup_cap(30, start, start + timedelta(days=70), 5, 5) == 30


def test_planned_capacity_is_the_smaller_of_global_and_mailbox_caps():
    today = date(2026, 10, 7)
    mbs = [MailboxConfig(email="a@x.co", daily_cap=30, warmup_start=today),
           MailboxConfig(email="b@y.co", daily_cap=30)]
    assert planned_daily_capacity(make_config(mailboxes=mbs, max_daily_sends=100), today) == 35
    assert planned_daily_capacity(make_config(mailboxes=mbs, max_daily_sends=20), today) == 20
    assert planned_daily_capacity(make_config(max_daily_sends=15), today) == 15


def test_mailbox_config_normalises_and_rejects_duplicates():
    assert MailboxConfig(email=" Harvey@EbsyHQ.com ").email == "harvey@ebsyhq.com"
    with pytest.raises(ValueError):
        MailboxConfig(email="not-an-address")
    with pytest.raises(ValueError):
        EmailChannelConfig(mailboxes=[MailboxConfig(email="a@x.co"),
                                      MailboxConfig(email="A@x.co")])


def test_env_secret_reads_mailbox_vars_and_known_fields():
    env = EnvConfig(smtp_password="legacy", mailbox_secrets={"MAILBOX_PASSWORD": "pw"})
    assert env.secret("MAILBOX_PASSWORD") == "pw"
    assert env.secret("SMTP_PASSWORD") == "legacy"
    assert env.secret("MAILBOX_MISSING") == ""


def test_smtp_provider_sends_as_its_mailbox():
    env = EnvConfig(smtp_host="mail.example.com", smtp_port=465, smtp_username="old@main.co",
                    smtp_password="legacy", mailbox_secrets={"MAILBOX_PASSWORD": "pw"})
    mb = MailboxConfig(email="harvey@ebsyhq.com", name="Carlos | EBSY",
                       password_env="MAILBOX_PASSWORD")
    p = SmtpImapProvider(make_config(), env, mailbox=mb)
    assert (p.smtp_user, p.smtp_pass, p.imap_user, p.imap_pass) == (
        "harvey@ebsyhq.com", "pw", "harvey@ebsyhq.com", "pw")
    assert p.smtp_host == "mail.example.com" and p.smtp_port == 465
    assert p.sender_address == "harvey@ebsyhq.com"
    assert "pw" not in repr(p)
    missing = SmtpImapProvider(make_config(), env, mailbox=MailboxConfig(
        email="x@y.co", password_env="MAILBOX_NOPE"))
    assert not missing.is_configured()


def test_pool_from_config_marks_the_smtp_login_as_legacy():
    env = EnvConfig(smtp_host="h", smtp_username="old@main.co", smtp_password="p",
                    mailbox_secrets={"MAILBOX_PASSWORD": "pw"})
    cfg = make_config(mailboxes=[
        MailboxConfig(email="new@ebsyhq.com", password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="old@main.co"),
        MailboxConfig(email="off@x.co", enabled=False),
    ])
    pool = MailboxPool.from_config(cfg, env)
    assert [mb.email for mb in pool.mailboxes] == ["new@ebsyhq.com", "old@main.co"]
    assert pool.legacy.email == "old@main.co"
    assert pool.resolve("") is pool.legacy


# ── sending ──

@pytest.mark.asyncio
async def test_rotation_spreads_new_threads_and_respects_each_cap(state):
    pool = make_pool(("a@x.co", 2, None), ("b@y.co", 1, None))
    sender = make_sender(state, pool)
    for i in range(5):
        pid = await seed_prospect(state, email=f"p{i}@acme.com")
        await queue(state, pid, f"p{i}@acme.com", campaign=f"c{i}")

    await sender._drain_due()

    a, b = pool.mailboxes[0].provider, pool.mailboxes[1].provider
    assert (len(a.sent), len(b.sent)) == (2, 1)
    sent = await state.get_outbox(status="sent")
    assert sorted(r["mailbox"] for r in sent) == ["a@x.co", "a@x.co", "b@y.co"]
    assert len(await state.get_outbox(status="approved")) == 2   # held for tomorrow
    assert await state.count_outbox_sent_today_by_mailbox() == {"a@x.co": 2, "b@y.co": 1}


@pytest.mark.asyncio
async def test_warming_mailbox_sends_only_its_ramp(state):
    today = datetime.now(timezone.utc).date()
    pool = make_pool(("new@x.co", 30, today), initial=2)
    sender = make_sender(state, pool)
    for i in range(4):
        pid = await seed_prospect(state, email=f"w{i}@acme.com")
        await queue(state, pid, f"w{i}@acme.com", campaign=f"c{i}")
    await sender._drain_due()
    assert len(pool.primary.provider.sent) == 2


@pytest.mark.asyncio
async def test_follow_up_stays_on_its_openers_mailbox(state):
    pool = make_pool(("a@x.co", 5, None), ("b@y.co", 5, None))
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert len(pool.mailboxes[1].provider.sent) == 1
    assert pool.mailboxes[0].provider.sent == []


@pytest.mark.asyncio
async def test_follow_up_holds_when_its_mailbox_is_at_cap(state):
    pool = make_pool(("a@x.co", 5, None), ("b@y.co", 1, None))
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    s2 = await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert pool.mailboxes[0].provider.sent == [] and pool.mailboxes[1].provider.sent == []
    assert (await state.get_outbox_item(s2))["status"] == "approved"


@pytest.mark.asyncio
async def test_pre_tracking_threads_continue_from_the_legacy_mailbox(state):
    pool = make_pool(("new@x.co", 5, None), ("old@main.co", 5, None))
    pool.mailboxes[0].legacy, pool.mailboxes[1].legacy = False, True
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso())  # mailbox ''
    await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert len(pool.mailboxes[1].provider.sent) == 1


@pytest.mark.asyncio
async def test_thread_of_a_removed_mailbox_moves_to_another(state):
    pool = make_pool(("a@x.co", 5, None))
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="gone@z.co")
    await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert len(pool.primary.provider.sent) == 1


@pytest.mark.asyncio
async def test_replies_go_first_and_from_the_inbox_they_answer(state):
    pool = make_pool(("a@x.co", 1, None), ("b@y.co", 1, None))
    sender = make_sender(state, pool)
    for i in range(2):
        pid = await seed_prospect(state, email=f"n{i}@acme.com")
        await queue(state, pid, f"n{i}@acme.com", campaign=f"c{i}")
    rp = await seed_prospect(state, email="replier@acme.com", status="replied")
    await queue(state, rp, "replier@acme.com", kind="reply", mailbox="b@y.co")

    await sender._drain_due()

    b_sent = pool.mailboxes[1].provider.sent
    assert [m["to"] for m in b_sent] == ["replier@acme.com"]
    assert len(pool.mailboxes[0].provider.sent) == 1


@pytest.mark.asyncio
async def test_global_cap_still_limits_the_pool(state):
    pool = make_pool(("a@x.co", 10, None), ("b@y.co", 10, None))
    sender = make_sender(state, pool, max_daily_sends=3)
    for i in range(6):
        pid = await seed_prospect(state, email=f"g{i}@acme.com")
        await queue(state, pid, f"g{i}@acme.com", campaign=f"c{i}")
    await sender._drain_due()
    assert sum(len(mb.provider.sent) for mb in pool.mailboxes) == 3


# ── follow-up approval ──

@pytest.mark.asyncio
async def test_auto_approve_promotes_follow_ups_of_approved_openers_only(state):
    p1 = await seed_prospect(state, email="ok@acme.com")
    p2 = await seed_prospect(state, email="wait@acme.com")
    p3 = await seed_prospect(state, email="no@acme.com")
    await queue(state, p1, "ok@acme.com", step=1, status="approved", campaign="c1")
    ok2 = await queue(state, p1, "ok@acme.com", step=2, status="pending_review", campaign="c1")
    ok3 = await queue(state, p1, "ok@acme.com", step=3, status="pending_review", campaign="c1")
    await queue(state, p2, "wait@acme.com", step=1, status="pending_review", campaign="c2")
    wait2 = await queue(state, p2, "wait@acme.com", step=2, status="pending_review", campaign="c2")
    await queue(state, p3, "no@acme.com", step=1, status="rejected", campaign="c3")
    no2 = await queue(state, p3, "no@acme.com", step=2, status="pending_review", campaign="c3")

    pool = make_pool(("a@x.co", 0, None))  # cap 0: nothing sends, only promotion
    sender = make_sender(state, pool, auto_approve_followups=True)
    await sender._promote_followups()

    status = lambda i: state.get_outbox_item(i)
    assert (await status(ok2))["status"] == "approved"
    assert (await status(ok3))["status"] == "approved"
    assert (await status(wait2))["status"] == "pending_review"
    assert (await status(no2))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_follow_ups_wait_for_review_when_auto_approve_is_off(state):
    pid = await seed_prospect(state)
    await queue(state, pid, "jane@acme.com", step=1, status="approved")
    s2 = await queue(state, pid, "jane@acme.com", step=2, status="pending_review")
    sender = make_sender(state, make_pool(("a@x.co", 0, None)))
    await sender._run_native()
    assert (await state.get_outbox_item(s2))["status"] == "pending_review"


# ── pacing ──

def test_spread_budget_divides_the_rest_of_the_day():
    morning = datetime(2026, 10, 7, 7, 0)
    assert spread_budget(60, morning, "22:00", 15) == 1      # 60 cycles left
    assert spread_budget(90, morning, "22:00", 15) == 2
    assert spread_budget(10, datetime(2026, 10, 7, 21, 50), "22:00", 15) == 10
    assert spread_budget(0, morning, "22:00", 15) == 0


@pytest.mark.asyncio
async def test_spread_sends_limits_a_cycle(state, monkeypatch):
    pool = make_pool(("a@x.co", 50, None))
    sender = make_sender(state, pool, spread_sends=True)
    monkeypatch.setattr(sender, "_spread", lambda left: 2)
    for i in range(6):
        pid = await seed_prospect(state, email=f"s{i}@acme.com")
        await queue(state, pid, f"s{i}@acme.com", campaign=f"c{i}")
    await sender._drain_due()
    assert len(pool.primary.provider.sent) == 2


# ── replies from every inbox ──

class BrokenProvider(FakeProvider):
    async def get_replies(self, limit=50):
        raise RuntimeError("imap down")


@pytest.mark.asyncio
async def test_handler_reads_every_inbox_and_answers_from_the_receiving_one(state):
    from harvey.agents.handler import Handler

    pid = await seed_prospect(state, email="jane@acme.com")
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    await state.update_prospect_status(pid, "contacted")

    broken = Mailbox(email="a@x.co", provider=BrokenProvider(), daily_cap=5)
    good = FakeProvider()
    good.inbound = [InboundMessage(provider_id="<r1@acme>", from_email="jane@acme.com",
                                   subject="Re: s1", body="Tell me more please.",
                                   message_id="<r1@acme>")]
    pool = MailboxPool([broken, Mailbox(email="b@y.co", provider=good, daily_cap=5)])

    handler = Handler(brain=StubBrain(intent="question"), state=state,
                      config=make_config(), env=SimpleNamespace(instantly_api_key=""))
    handler.mailboxes = pool
    handler.provider = good
    await handler._run_native()

    replies = [r for r in await state.get_outbox(status="pending_review")
               if r["kind"] == "reply"]
    assert len(replies) == 1
    assert replies[0]["mailbox"] == "b@y.co"


# ── dashboard report ──

def test_mailbox_report_shows_caps_stages_and_missing_passwords():
    from harvey.integrations.mailboxes import mailbox_report

    today = date(2026, 10, 7)
    cfg = make_config(auto_approve_followups=True, max_daily_sends=100, mailboxes=[
        MailboxConfig(email="old@main.co", daily_cap=30, warmup_start=date(2026, 9, 16)),
        MailboxConfig(email="new@x.co", daily_cap=30, warmup_start=today,
                      password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="later@y.co", daily_cap=30, warmup_start=date(2026, 10, 9),
                      password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="warm@z.co", daily_cap=20, password_env="MAILBOX_NOPE"),
    ])
    rep = mailbox_report(cfg, {"": 4, "old@main.co": 3, "new@x.co": 1},
                         lambda name: name != "MAILBOX_NOPE",
                         smtp_username="old@main.co", day=today)
    rows = {r["email"]: r for r in rep["mailboxes"]}

    # 3 weeks in: 5 + 3 * 5 = 20; full 30/day after ceil(25 / 5) = 5 weeks.
    assert rows["old@main.co"]["cap_today"] == 20 and rows["old@main.co"]["sent_24h"] == 7
    assert rows["old@main.co"]["stage"] == "warming"
    assert rows["old@main.co"]["full_on"] == "2026-10-21"
    assert rows["new@x.co"]["cap_today"] == 5 and rows["new@x.co"]["remaining"] == 4
    assert rows["later@y.co"]["stage"] == "scheduled" and rows["later@y.co"]["cap_today"] == 0
    assert rows["warm@z.co"]["configured"] is False and rows["warm@z.co"]["stage"] == "warm"
    assert rep["capacity_today"] == 25          # the unconfigured mailbox doesn't count
    assert rep["sent_24h"] == 8 and rep["auto_approve_followups"] is True


def test_mailbox_report_without_rotation_is_one_mailbox():
    from harvey.integrations.mailboxes import mailbox_report

    rep = mailbox_report(make_config(max_daily_sends=15), {"": 6}, lambda n: True)
    assert rep["rotation"] is False
    assert [(r["email"], r["cap_today"], r["sent_24h"]) for r in rep["mailboxes"]] == [
        ("carlos@main.co", 15, 6)]


@pytest.mark.asyncio
async def test_outbox_api_shows_the_mailbox_a_follow_up_inherits(state, monkeypatch):
    from harvey import dashboard

    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    await queue(state, pid, "jane@acme.com", step=2)
    rows = await dashboard._with_from_mailbox(state, await state.get_outbox(status="approved"))
    assert [r["from_mailbox"] for r in rows] == ["b@y.co"]
