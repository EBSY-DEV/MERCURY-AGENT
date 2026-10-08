"""Tests for the native sending path: gate, outbox lifecycle, stage/drain,
stop-on-reply, bounces, and the kill switch."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from mercury.gate import pre_send_check
from mercury.models.campaign import Campaign, EmailStep
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from mercury.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []
        self.fail_next = False

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         references=""):
        if self.fail_next:
            self.fail_next = False
            return SendResult(ok=False, error="smtp boom")
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to,
                          "references": references})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


class Cfg:
    class persona:
        name = "Mercury"; email = "mercury@x.co"; company = "X"; role = "BD"; tone = "direct"

    class product:
        name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
        objection_responses = {}

    class channels:
        class email:
            enabled = True
            provider = "smtp"
            max_daily_sends = 50
            send_to_risky = False
            require_approval = True
            max_bounce_rate = 0.02

        class linkedin:
            enabled = False

    class compliance:
        # The sender holds the outbox without a postal address (CAN-SPAM).
        postal_address = "1 Main St, Springfield"
        opt_out_line_en = 'Reply "unsubscribe" to opt out.'
        opt_out_line_es = 'Responde "baja" para no recibir más.'


class Env:
    instantly_api_key = ""


def make_sender(state, provider, require_approval=True):
    from mercury.agents.sender import Sender
    cfg = Cfg()
    cfg.channels.email.require_approval = require_approval
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = provider  # override registry lookup
    sender.send_pacing = False  # no inter-send sleeps in tests
    return sender


class StubBrain:
    """Minimal brain: canned intent + reply text, no subprocess."""
    def __init__(self, intent="question", reply="Sure, happy to help."):
        self._intent = intent
        self._reply = reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self._intent if task == "classify_intent" or "category" in prompt.lower() else self._reply


def make_handler(state, provider, intent="question"):
    from mercury.agents.handler import Handler
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=Cfg(), env=Env())
    handler.provider = provider
    return handler


async def seed_prospect(state, email="jane@acme.com", status="new", estatus="verified"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status=estatus, email_verified=(estatus == "verified"),
        status=status,
    ))


async def seed_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="test-campaign", channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="Body for {{company}}. Question?", delay_days=0),
            EmailStep(step=2, subject="follow", body="Another angle {{first_name}}.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── gate ──


def test_gate_blocks_bad_content():
    p = Prospect(first_name="J", last_name="D", title="VP",
                 email="jane@acme.com", email_status="verified")
    ok = pre_send_check("jane@acme.com", "hello", "Short and fine. Question?", p)
    assert ok.ok

    bad = pre_send_check("jane@acme.com", "hello", "Hi {{first_name}}, act now!", p)
    assert not bad.ok
    assert any("merge" in r for r in bad.reasons)
    assert any("banned" in r for r in bad.reasons)

    guess = Prospect(first_name="J", last_name="D", title="VP",
                     email="jane@acme.com", email_status="guess")
    blocked = pre_send_check("jane@acme.com", "hello", "Fine body.", guess)
    assert not blocked.ok

    # Replies to someone who emailed us are exempt from the status gate
    reply_ok = pre_send_check("jane@acme.com", "Re: hi", "Thanks!", guess, kind="reply")
    assert reply_ok.ok


def test_gate_link_and_html_limits():
    r = pre_send_check("a@b.co", "s", "See https://a.com and https://b.com")
    assert any("links" in x for x in r.reasons)
    r2 = pre_send_check("a@b.co", "s", "<div>hello</div>")
    assert any("HTML" in x for x in r2.reasons)


def test_bounce_detection():
    assert looks_like_bounce("mailer-daemon@googlemail.com", "anything")
    assert looks_like_bounce("mx@x.com", "Delivery Status Notification (Failure)")
    assert not looks_like_bounce("jane@acme.com", "Re: your note")


# ── staging ──


@pytest.mark.asyncio
async def test_stage_campaign_renders_and_respects_approval(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider(), require_approval=True)

    await sender._run_native()

    pending = await state.get_outbox(status="pending_review")
    assert len(pending) == 2  # both steps staged
    step1 = next(i for i in pending if i["step"] == 1)
    assert step1["subject"] == "hi Jane"
    assert "Acme" in step1["body"]
    # Nothing sent while awaiting approval
    assert sender.provider.sent == []

    # Idempotent: re-running stages nothing new
    campaign2 = await state.get_campaigns_by_status("active")
    assert campaign2[0].id == campaign.id
    await sender._run_native()
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_stage_skips_unverified(state):
    pid = await seed_prospect(state, email="guess@acme.com", estatus="guess")
    await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []


# ── drain ──


@pytest.mark.asyncio
async def test_drain_sends_approved_due_items(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()  # stage as approved + drain in one pass

    sent = await state.get_outbox(status="sent")
    assert len(sent) == 1                     # step 1 due now; step 2 in 3 days
    assert provider.sent[0]["to"] == "jane@acme.com"
    assert provider.sent[0]["subject"] == "hi Jane"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "contacted"
    approved_left = await state.get_outbox(status="approved")
    assert len(approved_left) == 1 and approved_left[0]["step"] == 2


@pytest.mark.asyncio
async def test_drain_respects_kill_switch(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await state.set_setting("sending_paused", "test pause")

    await sender._run_native()
    assert provider.sent == []
    assert await state.get_outbox(status="sent") == []


@pytest.mark.asyncio
async def test_drain_gate_failure_marks_failed(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Unrendered {{first_name}} tag",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert "gate" in failed[0]["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_send_failure_recorded(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Fine body. Question?",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    provider.fail_next = True
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert failed and "smtp boom" in failed[0]["error"]


@pytest.mark.asyncio
async def test_sending_freezes_draft_and_persona_attribution(state):
    from mercury.personas import PersonaStore

    pid = await seed_prospect(state)
    store = PersonaStore(state)
    profile = await store.resolve(Cfg())
    generation_id = await store.record(profile, Cfg(), "Original prompt", {}, "personal_email")
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="original subject",
        body="Original body. Question?", send_at=_now_iso(), status="approved",
        generation_id=generation_id,
    )

    class EditingProvider(FakeProvider):
        async def send_email(self, *args, **kwargs):
            with pytest.raises(ValueError, match="no longer available"):
                await store.replace_draft(item_id, {
                    "subject": "replacement", "body": "New body", "generation_id": "",
                })
            assert not await state.edit_outbox_item(item_id, body="Late manual edit")
            return await super().send_email(*args, **kwargs)

    provider = EditingProvider()
    await make_sender(state, provider)._drain_due()
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "sent"
    assert item["generation_id"] == generation_id
    assert item["subject"] == provider.sent[0]["subject"] == "original subject"
    assert item["body"] == provider.sent[0]["body"]
    assert len(await store.history(item_id)) == 1


@pytest.mark.asyncio
async def test_send_claim_rejects_stale_and_duplicate_snapshots(state):
    pid = await seed_prospect(state)
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello", body="Fine body.",
        send_at=_now_iso(), status="approved",
    )
    stale = await state.get_outbox_item(item_id)
    assert await state.edit_outbox_item(item_id, subject="reviewed subject", manually_edited=1)
    assert not await state.claim_outbox_item(stale, "mercury@x.co")
    # The edit sent it back to review; only a new approval lets it go.
    current = await state.get_outbox_item(item_id)
    assert current["status"] == "pending_review" and current["revision"] == 2
    assert not await state.claim_outbox_item(current, "mercury@x.co")
    assert await state.approve_outbox(item_id, 2)
    current = await state.get_outbox_item(item_id)
    assert await state.claim_outbox_item(current, "mercury@x.co")
    assert not await state.claim_outbox_item(current, "mercury@x.co")
    assert not await state.edit_outbox_item(item_id, send_at=_now_iso())


# ── handler: stop-on-reply, bounces, kill switch ──


@pytest.mark.asyncio
async def test_reply_cancels_pending_outbox(state):
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=2,
    )
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="unsubscribe please",
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    cancelled = await state.get_outbox(status="cancelled")
    assert len(cancelled) == 1
    prospect = await state.get_prospect(pid)
    assert prospect.status == "opted_out"   # opt-out keywords honored


async def _sent_for_bounce(state, extra):
    pid = await seed_prospect(state, status="contacted")
    # A sent item whose message-id the bounce references
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    await state.update_outbox_item(
        item_id, status="sent", message_id="<m1@x>", sent_at=_now_iso()
    )
    # `extra` more sent items, for the kill-switch minimum-sample rule
    for i in range(extra):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=_now_iso(), status="approved", campaign_id=f"c{i+2}", step=1,
        )
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())
    return pid


def _dsn(provider_id="b1"):
    return InboundMessage(
        provider_id=provider_id, from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="couldn't be delivered", in_reply_to="<m1@x>", is_bounce=True,
    )


@pytest.mark.asyncio
async def test_bounce_marks_invalid_and_kill_switch(state):
    pid = await _sent_for_bounce(state, extra=49)
    provider = FakeProvider()
    provider.inbound = [_dsn("b1"), _dsn("b2")]
    handler = make_handler(state, provider)
    await handler._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "invalid"
    # 2 bounces / 50 sent = 4% > 2% threshold with >= 50 sends → kill switch
    assert await state.get_setting("sending_paused") != ""


@pytest.mark.asyncio
async def test_bounce_in_a_small_sample_marks_invalid_without_the_kill_switch(state):
    pid = await _sent_for_bounce(state, extra=10)
    provider = FakeProvider()
    provider.inbound = [_dsn()]
    handler = make_handler(state, provider)
    await handler._run_native()

    assert (await state.get_prospect(pid)).email_status == "invalid"
    # 1 bounce / 11 sent is 9%, but 11 sends is too few to judge the list
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_inbound_dedup(state):
    await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="we're all set thanks",
    )]
    handler = make_handler(state, provider, intent="not_interested")
    await handler._run_native()
    first = await state.get_conversations_by_status("closed")
    await handler._run_native()   # same message again → ignored via dedup
    second = await state.get_conversations_by_status("closed")
    assert len(first) == len(second) == 1


# ── stuck 'sending' rows, raising providers, disabled mailboxes ──


@pytest.mark.asyncio
async def test_recover_stale_sending_rows(state):
    pid = await seed_prospect(state)
    stale_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s", body="Fine body.",
        send_at=_now_iso(), status="approved",
    )
    fresh_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="Fine body.",
        send_at=_now_iso(), status="approved",
    )
    for item_id in (stale_id, fresh_id):
        assert await state.claim_outbox_item(await state.get_outbox_item(item_id), "m@x.co")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    async with state._connect() as db:
        await db.execute("UPDATE outbox SET updated_at = ? WHERE id = ?", (old, stale_id))
        await db.commit()

    assert await state.recover_stale_outbox(max_age_minutes=30) == 1
    stale = await state.get_outbox_item(stale_id)
    assert stale["status"] == "approved"
    assert stale["error"] == "recovered: send interrupted"
    assert stale["send_at"]  # kept
    fresh = await state.get_outbox_item(fresh_id)
    assert fresh["status"] == "sending"
    assert await state.recover_stale_outbox(max_age_minutes=30) == 0


@pytest.mark.asyncio
async def test_provider_exception_requeues_instead_of_sticking(state):
    pid = await seed_prospect(state)
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello",
        body="Fine body. Question?", send_at=_now_iso(), status="approved",
    )

    class RaisingProvider(FakeProvider):
        async def send_email(self, *a, **k):
            raise RuntimeError("socket exploded")

    await make_sender(state, RaisingProvider())._drain_due()
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "approved"
    assert item["error"].startswith("retry 1/3: exception: RuntimeError")
    assert item["send_at"] > _now_iso()  # pushed into the future
    assert await state.get_outbox(status="sending") == []


@pytest.mark.asyncio
async def test_disabled_mailbox_holds_new_threads_but_finishes_replies(state):
    from mercury.integrations.mailboxes import Mailbox, MailboxPool

    pid = await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    pool = MailboxPool(
        [Mailbox(email="old@x.co", provider=provider, daily_cap=50, accepts_new=False)],
        warmup_initial_cap=50, warmup_weekly_increase=50,
    )
    step1 = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello",
        body="Fine body. Question?", send_at=_now_iso(), status="approved",
        campaign_id="c1", step=1, mailbox="old@x.co",
    )
    reply = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="Re: hello",
        body="Thanks, here is the answer.", send_at=_now_iso(), status="approved",
        kind="reply", mailbox="old@x.co",
    )
    sender = make_sender(state, provider)
    sender.mailboxes = pool
    await sender._drain_due()

    assert (await state.get_outbox_item(step1))["status"] == "approved"
    assert (await state.get_outbox_item(reply))["status"] == "sent"
    assert [m["subject"] for m in provider.sent] == ["Re: hello"]
