"""Mailbox rotation: caps, warm-up, thread pinning, follow-up approval,
pacing, and reading replies from every inbox."""

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mercury.agents.sender import spread_budget
from mercury.config import EmailChannelConfig, EnvConfig, MailboxConfig, ProductConfig
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import (
    Mailbox,
    MailboxPool,
    planned_daily_capacity,
    warmup_cap,
)
from mercury.integrations.smtp_mail import SmtpImapProvider
from mercury.state import StateManager
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
        persona=SimpleNamespace(name="Carlos", email="carlos@main.co", company="Northwind Outreach",
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
    from mercury.agents.sender import Sender
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
    env = EnvConfig(smtp_host="h", smtp_password="p")
    mbs = [MailboxConfig(email="a@x.co", daily_cap=30, warmup_start=today),
           MailboxConfig(email="b@y.co", daily_cap=30)]
    assert planned_daily_capacity(make_config(mailboxes=mbs, max_daily_sends=100), today, env) == 35
    assert planned_daily_capacity(make_config(mailboxes=mbs, max_daily_sends=20), today, env) == 20
    assert planned_daily_capacity(make_config(max_daily_sends=15), today, env) == 15


def test_mailbox_config_normalises_and_rejects_duplicates():
    assert MailboxConfig(email=" Mercury@NorthwindHQ.example ").email == "mercury@northwindhq.example"
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
    mb = MailboxConfig(email="mercury@northwindhq.example", name="Carlos | Northwind",
                       password_env="MAILBOX_PASSWORD")
    p = SmtpImapProvider(make_config(), env, mailbox=mb)
    assert (p.smtp_user, p.smtp_pass, p.imap_user, p.imap_pass) == (
        "mercury@northwindhq.example", "pw", "mercury@northwindhq.example", "pw")
    assert p.smtp_host == "mail.example.com" and p.smtp_port == 465
    assert p.sender_address == "mercury@northwindhq.example"
    assert "pw" not in repr(p)
    missing = SmtpImapProvider(make_config(), env, mailbox=MailboxConfig(
        email="x@y.co", password_env="MAILBOX_NOPE"))
    assert not missing.is_configured()


def test_pool_from_config_keeps_disabled_mailboxes_for_their_threads():
    env = EnvConfig(smtp_host="h", smtp_username="old@main.co", smtp_password="p",
                    mailbox_secrets={"MAILBOX_PASSWORD": "pw"})
    cfg = make_config(mailboxes=[
        MailboxConfig(email="new@northwindhq.example", password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="old@main.co"),
        MailboxConfig(email="off@x.co", enabled=False, password_env="MAILBOX_PASSWORD"),
    ])
    cfg.persona.email = "nobody@else.co"
    pool = MailboxPool.from_config(cfg, env)
    assert [mb.email for mb in pool.mailboxes] == ["new@northwindhq.example", "old@main.co", "off@x.co"]
    assert [mb.accepts_new for mb in pool.mailboxes] == [True, True, False]
    assert pool.legacy.email == "old@main.co"          # SMTP login, persona not listed
    assert pool.resolve("") is pool.legacy


def test_legacy_mailbox_is_the_old_from_address_first():
    # Old mail went out From persona.email, so that mailbox owns '' rows
    # even when the SMTP login is a different listed mailbox.
    env = EnvConfig(smtp_host="h", smtp_username="login@main.co", smtp_password="p")
    cfg = make_config(mailboxes=[MailboxConfig(email="login@main.co"),
                                 MailboxConfig(email="carlos@main.co")])
    assert MailboxPool.from_config(cfg, env).legacy.email == "carlos@main.co"


def test_secret_only_hands_out_mailbox_passwords():
    env = EnvConfig(tavily_api_key="tvly-secret", smtp_password="s",
                    mailbox_secrets={"MAILBOX_X": "x"})
    assert env.secret("TAVILY_API_KEY") == ""
    assert env.secret("SMTP_PASSWORD") == "s" and env.secret("MAILBOX_X") == "x"


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
async def test_thread_of_a_removed_mailbox_is_held_not_rerouted(state):
    # Nobody reads the removed inbox: continuing from another address would
    # keep mailing someone whose reply or opt-out went unseen. Held, not
    # cancelled, so a config typo is reversible.
    pool = make_pool(("a@x.co", 5, None))
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="gone@z.co")
    s2 = await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert pool.primary.provider.sent == []
    assert (await state.get_outbox_item(s2))["status"] == "approved"


@pytest.mark.asyncio
async def test_single_mailbox_continues_threads_whatever_address_they_recorded(state):
    # Without a rotation list there is one inbox; a changed persona.email
    # must not strand the threads it already started.
    pool = MailboxPool.single(FakeProvider(), 10, "new@main.co")
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="old@main.co")
    await queue(state, pid, "jane@acme.com", step=2)
    await sender._drain_due()
    assert len(pool.primary.provider.sent) == 1


@pytest.mark.asyncio
async def test_disabled_mailbox_finishes_its_threads_but_starts_none(state):
    pool = make_pool(("a@x.co", 5, None), ("off@y.co", 5, None))
    pool.mailboxes[1].accepts_new = False
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="off@y.co")
    await queue(state, pid, "jane@acme.com", step=2)
    for i in range(3):
        p = await seed_prospect(state, email=f"new{i}@acme.com")
        await queue(state, p, f"new{i}@acme.com", campaign=f"n{i}")

    await sender._drain_due()

    assert [m["to"] for m in pool.mailboxes[1].provider.sent] == ["jane@acme.com"]
    assert len(pool.mailboxes[0].provider.sent) == 3


@pytest.mark.asyncio
async def test_follow_up_without_credentials_is_held(state):
    class NoCreds(FakeProvider):
        def is_configured(self):
            return False
    pool = MailboxPool([Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=5),
                        Mailbox(email="b@y.co", provider=NoCreds(), daily_cap=5)])
    sender = make_sender(state, pool)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    s2 = await queue(state, pid, "jane@acme.com", step=2)

    await sender._drain_due()

    assert pool.mailboxes[0].provider.sent == []
    assert (await state.get_outbox_item(s2))["status"] == "approved"


@pytest.mark.asyncio
async def test_legacy_sends_count_against_the_legacy_mailbox(state):
    pool = make_pool(("old@main.co", 3, None), ("new@x.co", 3, None))
    sender = make_sender(state, pool)
    for i in range(3):   # three pre-tracking sends today, mailbox ''
        p = await seed_prospect(state, email=f"old{i}@acme.com")
        oid = await queue(state, p, f"old{i}@acme.com", campaign=f"o{i}")
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())
    for i in range(4):
        p = await seed_prospect(state, email=f"n{i}@acme.com")
        await queue(state, p, f"n{i}@acme.com", campaign=f"n{i}")

    await sender._drain_due()

    assert pool.mailboxes[0].provider.sent == []          # already at its 3
    assert len(pool.mailboxes[1].provider.sent) == 3


@pytest.mark.asyncio
async def test_capped_follow_ups_do_not_starve_other_mailboxes(state):
    # Many old follow-ups pinned to a capped mailbox sit ahead of newer
    # openers; the free mailbox must still send.
    pool = make_pool(("old@main.co", 1, None), ("new@x.co", 3, None))
    sender = make_sender(state, pool, spread_sends=True)
    sender._spread = lambda left: 1
    old = (datetime.now(timezone.utc) - timedelta(days=5)).replace(tzinfo=None).isoformat()
    for i in range(40):
        p = await seed_prospect(state, email=f"t{i}@acme.com")
        s1 = await queue(state, p, f"t{i}@acme.com", campaign=f"t{i}")
        await state.update_outbox_item(s1, status="sent", sent_at=old, mailbox="old@main.co")
        s2 = await queue(state, p, f"t{i}@acme.com", step=2, campaign=f"t{i}")
        await state.update_outbox_item(s2, send_at=old)
    p = await seed_prospect(state, email="fresh@acme.com")
    await queue(state, p, "fresh@acme.com", campaign="fresh")

    await sender._drain_due()          # old@main.co takes its 1; budget 1 spent
    await sender._drain_due()          # old is capped now: the opener must go

    assert [m["to"] for m in pool.mailboxes[1].provider.sent] == ["fresh@acme.com"]


@pytest.mark.asyncio
async def test_follow_up_waits_its_delay_after_the_opener_really_went_out(state):
    from mercury.models.campaign import Campaign, EmailStep

    camp = Campaign(id="", name="c", sequence=[
        EmailStep(step=1, subject="s1", body="b1", delay_days=0),
        EmailStep(step=2, subject="s2", body="b2", delay_days=3)])
    cid = await state.add_campaign(camp)
    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1, campaign=cid)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="a@x.co")
    s2 = await queue(state, pid, "jane@acme.com", step=2, campaign=cid)  # staged long ago

    pool = make_pool(("a@x.co", 5, None))
    sender = make_sender(state, pool)
    await sender._drain_due()

    assert pool.primary.provider.sent == []
    row = await state.get_outbox_item(s2)
    assert row["status"] == "approved"
    resched = datetime.fromisoformat(row["send_at"])
    assert timedelta(days=2, hours=23) < resched - datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_a_reply_may_pass_its_mailboxs_warmup_cap(state):
    pool = make_pool(("a@x.co", 1, None))
    sender = make_sender(state, pool)
    p = await seed_prospect(state, email="n@acme.com")
    oid = await queue(state, p, "n@acme.com", campaign="n")
    await state.update_outbox_item(oid, status="sent", sent_at=_now_iso(), mailbox="a@x.co")
    rp = await seed_prospect(state, email="replier@acme.com", status="replied")
    await queue(state, rp, "replier@acme.com", kind="reply", mailbox="a@x.co")

    await sender._drain_due()

    assert [m["to"] for m in pool.primary.provider.sent] == ["replier@acme.com"]


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
    from mercury.agents.handler import Handler

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

def _report_pool(env, cfg):
    return MailboxPool.from_config(cfg, env)


def test_mailbox_report_matches_what_the_sender_enforces():
    from mercury.integrations.mailboxes import mailbox_report

    today = date(2026, 10, 7)
    env = EnvConfig(smtp_host="mail.x", smtp_username="old@main.co", smtp_password="p",
                    mailbox_secrets={"MAILBOX_PASSWORD": "pw"})
    cfg = make_config(auto_approve_followups=True, max_daily_sends=100, mailboxes=[
        MailboxConfig(email="old@main.co", daily_cap=30, warmup_start=date(2026, 9, 16)),
        MailboxConfig(email="new@x.co", daily_cap=30, warmup_start=today,
                      password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="later@y.co", daily_cap=30, warmup_start=date(2026, 10, 9),
                      password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="warm@z.co", daily_cap=20, password_env="MAILBOX_NOPE"),
        MailboxConfig(email="flat@q.co", daily_cap=30, warmup_start=today,
                      password_env="MAILBOX_PASSWORD", enabled=False),
    ])
    cfg.persona.email = "old@main.co"
    pool = _report_pool(env, cfg)
    sent = {"": 4, "old@main.co": 3, "new@x.co": 1, "gone@r.co": 2}
    rep = mailbox_report(cfg, pool, sent, day=today)
    rows = {r["email"]: r for r in rep["mailboxes"]}

    # 3 weeks in: 5 + 3 * 5 = 20; full 30/day after ceil(25 / 5) = 5 weeks.
    assert rows["old@main.co"]["cap_today"] == 20 and rows["old@main.co"]["sent_24h"] == 7
    assert rows["old@main.co"]["stage"] == "warming" and rows["old@main.co"]["legacy"]
    assert rows["old@main.co"]["full_on"] == "2026-10-21"
    assert rows["later@y.co"]["stage"] == "scheduled" and rows["later@y.co"]["cap_today"] == 0
    assert rows["warm@z.co"]["configured"] is False and rows["warm@z.co"]["remaining"] == 0
    assert rows["flat@q.co"]["accepts_new"] is False
    assert rows[""]["stage"] == "removed" and rows[""]["sent_24h"] == 2
    # Same numbers as the pool the sender uses.
    remaining = pool.remaining(sent, today)
    for email, left in remaining.items():
        assert rows[email]["remaining"] == left
    assert rep["capacity_today"] == pool.capacity_on(today) == 20 + 5 + 0 + 5
    assert rep["legacy_email"] == "old@main.co" and rep["capped_by_global"] is False
    assert rep["sent_24h"] == 10 and rep["auto_approve_followups"] is True


def test_mailbox_report_stage_for_a_ramp_that_never_finishes():
    from mercury.integrations.mailboxes import mailbox_report

    today = date(2026, 10, 7)
    env = EnvConfig(smtp_host="h", mailbox_secrets={"MAILBOX_P": "x"})
    cfg = make_config(warmup_weekly_increase=0, mailboxes=[
        MailboxConfig(email="a@x.co", daily_cap=30, warmup_start=today, password_env="MAILBOX_P")])
    row = mailbox_report(cfg, _report_pool(env, cfg), {}, day=today)["mailboxes"][0]
    assert (row["stage"], row["cap_today"], row["full_on"]) == ("fixed", 5, None)


def test_mailbox_report_without_rotation_is_one_mailbox():
    from mercury.integrations.mailboxes import mailbox_report

    cfg = make_config(max_daily_sends=15)
    pool = MailboxPool.single(FakeProvider(), 15, "carlos@main.co")
    rep = mailbox_report(cfg, pool, {"": 6})
    assert rep["rotation"] is False
    assert [(r["email"], r["cap_today"], r["sent_24h"]) for r in rep["mailboxes"]] == [
        ("carlos@main.co", 15, 6)]
    assert mailbox_report(cfg, None, {})["mailboxes"] == []     # instantly


def test_planned_capacity_counts_only_usable_mailboxes_for_new_threads():
    today = date(2026, 10, 7)
    env = EnvConfig(smtp_host="h", smtp_password="p")
    cfg = make_config(mailboxes=[
        MailboxConfig(email="a@x.co", daily_cap=10),
        MailboxConfig(email="b@y.co", daily_cap=10, password_env="MAILBOX_NOPE"),
        MailboxConfig(email="c@z.co", daily_cap=10, enabled=False),
    ])
    assert planned_daily_capacity(cfg, today, env) == 10


def test_load_env_from_a_mapping_leaves_os_environ_alone(monkeypatch):
    from mercury.config import load_env

    monkeypatch.delenv("MAILBOX_ONLY_IN_FILE", raising=False)
    env = load_env({"SMTP_HOST": "h", "MAILBOX_ONLY_IN_FILE": "x"})
    assert env.smtp_host == "h" and env.secret("MAILBOX_ONLY_IN_FILE") == "x"
    assert "MAILBOX_ONLY_IN_FILE" not in os.environ


@pytest.mark.asyncio
async def test_promotion_can_be_limited_to_one_thread(state):
    a = await seed_prospect(state, email="a@acme.com")
    b = await seed_prospect(state, email="b@acme.com")
    await queue(state, a, "a@acme.com", step=1, campaign="ca")
    a2 = await queue(state, a, "a@acme.com", step=2, status="pending_review", campaign="ca")
    await queue(state, b, "b@acme.com", step=1, campaign="cb")
    b2 = await queue(state, b, "b@acme.com", step=2, status="pending_review", campaign="cb")
    assert await state.approve_ready_followups(campaign_id="ca", prospect_id=a) == 1
    assert (await state.get_outbox_item(a2))["status"] == "approved"
    assert (await state.get_outbox_item(b2))["status"] == "pending_review"


def test_has_credentials_ignores_an_empty_mailbox_secrets_dict(monkeypatch):
    from mercury import main as M

    monkeypatch.setattr(M, "load_env", lambda: EnvConfig())
    assert M._has_credentials() is False
    monkeypatch.setattr(M, "load_env", lambda: EnvConfig(mailbox_secrets={"MAILBOX_P": "x"}))
    assert M._has_credentials() is True


@pytest.mark.asyncio
async def test_outbox_api_shows_the_mailbox_a_follow_up_inherits(state, monkeypatch):
    from mercury import dashboard

    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    await queue(state, pid, "jane@acme.com", step=2)
    rows = await dashboard._with_from_mailbox(state, await state.get_outbox(status="approved"))
    assert [r["from_mailbox"] for r in rows] == ["b@y.co"]


@pytest.mark.asyncio
async def test_from_mailbox_resolves_legacy_threads_and_waiting_chains(state):
    from mercury import dashboard

    # Old thread: opener sent before tracking -> follow-up goes from legacy.
    p1 = await seed_prospect(state, email="old@acme.com")
    o1 = await queue(state, p1, "old@acme.com", step=1, campaign="c1")
    await state.update_outbox_item(o1, status="sent", sent_at=_now_iso())
    await queue(state, p1, "old@acme.com", step=2, campaign="c1")
    # Step 3 whose step 2 is approved but unsent: inherits step 1's mailbox.
    p2 = await seed_prospect(state, email="chain@acme.com")
    o2 = await queue(state, p2, "chain@acme.com", step=1, campaign="c2")
    await state.update_outbox_item(o2, status="sent", sent_at=_now_iso(), mailbox="b@y.co")
    await queue(state, p2, "chain@acme.com", step=2, campaign="c2")
    await queue(state, p2, "chain@acme.com", step=3, campaign="c2")
    # New thread, opener not sent: rotates.
    p3 = await seed_prospect(state, email="new@acme.com")
    await queue(state, p3, "new@acme.com", step=1, campaign="c3")

    rows = await dashboard._with_from_mailbox(
        state, await state.get_outbox(status="approved"), legacy_email="legacy@main.co")
    got = {(r["to_email"], r["step"]): r["from_mailbox"] for r in rows}
    assert got[("old@acme.com", 2)] == "legacy@main.co"
    assert got[("chain@acme.com", 2)] == "b@y.co"
    assert got[("chain@acme.com", 3)] == "b@y.co"
    assert got[("new@acme.com", 1)] == ""
    sent = await dashboard._with_from_mailbox(
        state, await state.get_outbox(status="sent"), legacy_email="legacy@main.co")
    assert {r["to_email"]: r["from_mailbox"] for r in sent}["old@acme.com"] == "legacy@main.co"


def test_mailboxes_endpoint_reports_presence_only(state, monkeypatch):
    from fastapi.testclient import TestClient
    from mercury import dashboard

    env = EnvConfig(smtp_host="mail.x", smtp_username="old@main.co",
                    smtp_password="TOPSECRET-1", mailbox_secrets={"MAILBOX_PASSWORD": "TOPSECRET-2"})
    cfg = make_config(mailboxes=[
        MailboxConfig(email="old@main.co"),
        MailboxConfig(email="new@x.co", password_env="MAILBOX_PASSWORD"),
        MailboxConfig(email="nopw@y.co", password_env="MAILBOX_MISSING"),
    ])
    cfg.persona.email = "old@main.co"
    monkeypatch.setattr(dashboard, "_mail_context",
                        lambda: (cfg, MailboxPool.from_config(cfg, env)))
    monkeypatch.setattr(dashboard, "_state", lambda: state)

    r = TestClient(dashboard.app).get("/api/mailboxes")
    body = r.text
    assert r.status_code == 200 and "TOPSECRET" not in body
    data = r.json()
    assert [m["configured"] for m in data["mailboxes"]] == [True, True, False]
    assert data["legacy_email"] == "old@main.co"


def test_mailboxes_endpoint_explains_a_broken_config(monkeypatch):
    from fastapi.testclient import TestClient
    from mercury import dashboard

    def boom():
        raise ValueError("channels.email.mailboxes.0.email: secret-looking input")
    monkeypatch.setattr(dashboard, "_mail_context", boom)
    data = TestClient(dashboard.app).get("/api/mailboxes").json()
    assert "error" in data and "ValueError" in data["error"]
    assert "secret-looking" not in data["error"]       # no config values echoed


@pytest.mark.asyncio
async def test_approving_an_opener_promotes_its_follow_ups_at_once(state, monkeypatch):
    from fastapi.testclient import TestClient
    from mercury import dashboard

    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1, status="pending_review")
    s2 = await queue(state, pid, "jane@acme.com", step=2, status="pending_review")
    s3 = await queue(state, pid, "jane@acme.com", step=3, status="pending_review")
    cfg = make_config(auto_approve_followups=True)
    monkeypatch.setattr(dashboard, "_state", lambda: state)
    monkeypatch.setattr("mercury.config.load_config", lambda *a, **k: cfg)

    data = TestClient(dashboard.app).post(f"/api/outbox/{s1}/approve", json={"revision": 1}).json()

    assert data == {"success": True, "followups_approved": 2, "revision": 1}
    for i in (s2, s3):
        assert (await state.get_outbox_item(i))["status"] == "approved"


@pytest.mark.asyncio
async def test_reply_from_a_mailbox_without_credentials_is_held(state):
    # Replies skip the warm-up cap, so only the credential check stops one
    # from going to an SMTP server with no password.
    class NoCreds(FakeProvider):
        def is_configured(self):
            return False
    nocreds = NoCreds()
    pool = MailboxPool([Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=5),
                        Mailbox(email="b@y.co", provider=nocreds, daily_cap=5)])
    sender = make_sender(state, pool)
    rp = await seed_prospect(state, email="replier@acme.com", status="replied")
    rid = await queue(state, rp, "replier@acme.com", kind="reply", mailbox="b@y.co")

    await sender._drain_due()

    assert nocreds.sent == [] and pool.mailboxes[0].provider.sent == []
    assert (await state.get_outbox_item(rid))["status"] == "approved"


@pytest.mark.asyncio
async def test_from_mailbox_flags_threads_of_removed_mailboxes(state):
    from mercury import dashboard

    pid = await seed_prospect(state)
    s1 = await queue(state, pid, "jane@acme.com", step=1)
    await state.update_outbox_item(s1, status="sent", sent_at=_now_iso(), mailbox="gone@z.co")
    await queue(state, pid, "jane@acme.com", step=2)
    rows = await dashboard._with_from_mailbox(
        state, await state.get_outbox(status="approved"), "a@x.co", known={"a@x.co"})
    assert rows[0]["from_mailbox"] == "gone@z.co" and rows[0]["from_removed"] is True
