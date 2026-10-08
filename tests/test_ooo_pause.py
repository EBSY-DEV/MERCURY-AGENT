"""Out-of-office pauses, end to end: a vacation reply holds a contact's cold
sequence until they are back, then the next step goes and later steps keep
their gaps.

Real temporary SQLite databases, a recording mail provider, and a clock the
tests move by hand. All people and companies are synthetic (example.com).
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from mercury import metrics
from mercury.agents.handler import Handler
from mercury.control.pauses import PauseError, PauseService
from mercury.integrations.mail_provider import InboundMessage
from mercury.models.campaign import Campaign, EmailStep
from mercury.state import StateManager
from tests.test_outbox_native import (
    Cfg, Env, FakeProvider, StubBrain, make_sender, seed_prospect,
)

NY = "America/New_York"
EMAIL = "jane@example.com"


class TzCfg(Cfg):
    class usage:
        class quiet_hours:
            timezone = NY
            start = "22:00"


class Clock:
    """Naive UTC, moved by the test."""

    def __init__(self, *args):
        self.now = datetime(*args)

    def __call__(self):
        return self.now

    def at(self, *args):
        self.now = datetime(*args)
        return self


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def make_sender_at(state, provider, clock, require_approval=False):
    sender = make_sender(state, provider, require_approval=require_approval)
    sender.config = TzCfg()
    sender.clock = clock
    return sender


def make_handler_at(state, provider, clock, intent="question"):
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=TzCfg(), env=Env())
    handler.provider = provider
    handler.clock = clock
    return handler


async def three_step_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="offer_a", channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="A note for {{company}}. Question?",
                      delay_days=0),
            EmailStep(step=2, subject="again", body="Another angle, {{first_name}}.", delay_days=3),
            EmailStep(step=3, subject="last", body="Closing the loop.", delay_days=4),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


def vacation(key="<ooo-1@example.com>", date="Tue, 06 Oct 2026 10:00:00 -0400",
             body="Thanks for your email. I'm out of the office until October 20 with "
                  "limited access to email.", sender=EMAIL, provider_id=None):
    return InboundMessage(
        provider_id=provider_id or key, message_id=key, from_email=sender,
        subject="Automatic reply: hi Jane", body=body, date=date,
        headers={"Auto-Submitted": "auto-replied"},
    )


async def sequence_rows(state):
    rows = await state.get_outbox(limit=50)
    return {r["step"]: r for r in rows if r["kind"] == "sequence"}


async def in_flight(state, provider, clock, require_approval=False):
    """Jane got step 1 on Monday 5 October; steps 2 and 3 are queued."""
    pid = await seed_prospect(state, email=EMAIL)
    await three_step_campaign(state, [pid])
    sender = make_sender_at(state, provider, clock.at(2026, 10, 5, 14, 0), require_approval)
    await sender._run_native()
    if require_approval:
        rows = await sequence_rows(state)
        await state.approve_outbox(rows[1]["id"])
        await sender._drain_due()
    assert [m["to"] for m in provider.sent] == [EMAIL]
    return pid, sender


async def actions(state, kind):
    async with state._connect() as db:
        async with db.execute("SELECT COUNT(*) FROM actions WHERE action_type = ?",
                              (kind,)) as cur:
            return (await cur.fetchone())[0]


# ── The whole path ──


@pytest.mark.asyncio
async def test_vacation_reply_defers_the_sequence_and_resumes_on_return(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, sender = await in_flight(state, provider, clock)
    handler = make_handler_at(state, provider, clock)

    provider.inbound = [vacation()]
    await handler._run_native()

    pause = await state.get_active_pause(pid)
    assert pause["review_state"] == "scheduled"
    assert pause["resume_at"] == "2026-10-20T13:00:00"      # 09:00 in New York
    assert pause["return_text"] == "October 20"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "contacted"                    # pipeline place untouched
    assert await state.get_conversations_by_status("open") == []
    assert await actions(state, "reply_received") == 0
    assert await actions(state, "ooo_paused") == 1

    # Step 2 was due on the 8th. It waits through the vacation.
    for moment in ((2026, 10, 8, 15, 0), (2026, 10, 12, 15, 0), (2026, 10, 20, 12, 59)):
        clock.at(*moment)
        await sender._run_native()
        assert len(provider.sent) == 1

    # Back on the 20th: only the next step goes, never the overdue backlog.
    clock.at(2026, 10, 20, 13, 5)
    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["hi Jane", "Re: hi Jane"]
    assert await state.get_active_pause(pid) is None
    assert await actions(state, "ooo_resumed") == 1
    rows = await sequence_rows(state)
    step2_sent = datetime.fromisoformat(rows[2]["sent_at"])
    assert datetime.fromisoformat(rows[3]["send_at"]) >= step2_sent + timedelta(days=4)

    clock.at(2026, 10, 21, 13, 5)
    await sender._run_native()
    assert len(provider.sent) == 2                           # the 4-day gap holds

    clock.at(2026, 10, 24, 13, 10)
    await sender._run_native()
    assert [m["subject"] for m in provider.sent][-1] == "Re: hi Jane"

    # The vacation reply never counted as a reply.
    counts = await metrics.window_counts(state.db_path, "2026-01-01T00:00:00")
    assert counts["replies"] == 0 and counts["positive"] == 0
    (record,) = await state.auto_replies_for(pid)
    assert record["kind"] == "out_of_office" and record["outcome"] == "paused"


@pytest.mark.asyncio
async def test_classifier_detected_vacation_reply_pauses_too(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 7, 14, 0)
    pid, _sender = await in_flight(state, provider, clock)
    provider.inbound = [InboundMessage(
        provider_id="m2", message_id="<m2@example.com>", from_email=EMAIL,
        subject="Re: hi Jane", body="Estoy de vacaciones, regreso el lunes.",
        date="Wed, 07 Oct 2026 09:00:00 -0400")]
    await make_handler_at(state, provider, clock.at(2026, 10, 7, 14, 0), intent="ooo")._run_native()

    pause = await state.get_active_pause(pid)
    assert pause["resume_at"] == "2026-10-12T13:00:00"
    (record,) = await state.auto_replies_for(pid)
    assert record["detected_by"] == "classifier"
    assert await state.get_conversations_by_status("open") == []


@pytest.mark.asyncio
async def test_receipts_and_acknowledgements_are_kept_and_change_nothing(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, sender = await in_flight(state, provider, clock)
    provider.inbound = [
        InboundMessage(provider_id="r1", message_id="<r1@example.com>", from_email=EMAIL,
                       subject="Read: hi Jane", body="Your message was read."),
        InboundMessage(provider_id="a1", message_id="<a1@example.com>", from_email=EMAIL,
                       subject="Re: hi Jane", body="We have received your message.",
                       headers={"Auto-Submitted": "auto-generated"}),
    ]
    await make_handler_at(state, provider, clock)._run_native()

    assert await state.get_active_pause(pid) is None
    kinds = sorted(r["kind"] for r in await state.auto_replies_for(pid))
    assert kinds == ["acknowledgement", "receipt"]
    clock.at(2026, 10, 8, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 2                           # the sequence goes on


# ── Review state ──


@pytest.mark.asyncio
async def test_no_date_waits_for_review_and_never_resumes_by_itself(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, sender = await in_flight(state, provider, clock)
    provider.inbound = [vacation(body="I am out of the office with limited access to email.")]
    await make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))._run_native()

    pause = await state.get_active_pause(pid)
    assert pause["review_state"] == "needs_review"
    assert pause["review_reason"] == "no_date" and pause["resume_at"] is None

    clock.at(2026, 12, 20, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1
    assert await state.get_active_pause(pid) is not None

    # A person sets the date; the sequence picks up that morning.
    svc = PauseService(state, TzCfg(), clock=clock)
    set_ = await svc.set_return_date(pause["id"], "2026-12-22", note="called the office",
                                     actor="test")
    assert set_["back_on"] == "2026-12-22" and set_["manual_override"] == 1
    clock.at(2026, 12, 22, 14, 1)                            # 09:01 EST
    await sender._run_native()
    assert len(provider.sent) == 2
    assert await actions(state, "ooo_return_date_set") == 1


@pytest.mark.asyncio
async def test_ambiguous_numeric_date_needs_review_and_keeps_the_text(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, _ = await in_flight(state, provider, clock)
    provider.inbound = [vacation(body="Out of the office. Back on 05/11.")]
    await make_handler_at(state, provider, clock)._run_native()
    pause = await state.get_active_pause(pid)
    assert (pause["review_state"], pause["review_reason"], pause["return_text"]) == (
        "needs_review", "ambiguous", "05/11")


@pytest.mark.asyncio
async def test_manual_resume_sends_only_the_next_step_and_keeps_approvals(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, sender = await in_flight(state, provider, clock, require_approval=True)
    rows = await sequence_rows(state)
    await state.approve_outbox(rows[2]["id"])                # step 3 is still a draft
    provider.inbound = [vacation()]
    await make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))._run_native()
    pause = await state.get_active_pause(pid)

    clock.at(2026, 10, 14, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1

    resumed = await PauseService(state, TzCfg(), clock=clock).resume(pause["id"], actor="test")
    assert resumed["rescheduled"] == 2
    rows = await sequence_rows(state)
    assert (rows[2]["status"], rows[3]["status"]) == ("approved", "pending_review")
    gap = (datetime.fromisoformat(rows[3]["send_at"]) - datetime.fromisoformat(rows[2]["send_at"]))
    assert gap == timedelta(days=4)

    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["hi Jane", "Re: hi Jane"]
    rows = await sequence_rows(state)
    assert rows[3]["status"] == "pending_review"             # a pause never approves


# ── Idempotency, restart, overrides ──


@pytest.mark.asyncio
async def test_duplicate_polling_neither_duplicates_nor_moves_the_pause(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, _ = await in_flight(state, provider, clock)
    handler = make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))
    provider.inbound = [vacation()]
    await handler._run_native()
    first = await state.get_active_pause(pid)

    # The same poll again, the same message seen through a second mailbox
    # (another provider id), and a lost processed_replies table.
    clock.at(2026, 10, 7, 15, 0)
    await handler._run_native()
    provider.inbound = [vacation(provider_id="gmail-xyz")]
    await handler._run_native()
    async with state._connect() as db:
        await db.execute("DELETE FROM processed_replies")
        await db.commit()
    provider.inbound = [vacation()]
    await handler._run_native()

    assert await state.get_active_pause(pid) == first
    assert len(await state.auto_replies_for(pid)) == 1
    assert await actions(state, "ooo_paused") == 1
    assert await actions(state, "ooo_pause_updated") == 0


@pytest.mark.asyncio
async def test_a_pause_survives_a_restart(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, _ = await in_flight(state, provider, clock)
    provider.inbound = [vacation()]
    await make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))._run_native()

    reopened = StateManager(state.db_path)
    await reopened.init_db()
    fresh = make_sender_at(reopened, provider, clock.at(2026, 10, 12, 15, 0))
    await fresh._run_native()
    assert len(provider.sent) == 1
    assert (await reopened.get_active_pause(pid))["resume_at"] == "2026-10-20T13:00:00"


@pytest.mark.asyncio
async def test_newer_reply_updates_but_a_replayed_old_one_never_beats_an_override(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, _ = await in_flight(state, provider, clock)
    handler = make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))
    provider.inbound = [vacation()]
    await handler._run_native()
    pause = await state.get_active_pause(pid)

    clock.at(2026, 10, 7, 15, 0)
    await PauseService(state, TzCfg(), clock=clock).set_return_date(pause["id"], "2026-10-22")
    assert (await state.get_active_pause(pid))["resume_at"] == "2026-10-22T13:00:00"

    # An older vacation reply turns up late: the operator's date stands.
    provider.inbound = [vacation(key="<ooo-0@example.com>", date="Mon, 05 Oct 2026 18:00:00 -0400",
                                 body="Out of the office until October 15.")]
    await handler._run_native()
    kept = await state.get_active_pause(pid)
    assert kept["resume_at"] == "2026-10-22T13:00:00" and kept["manual_override"] == 1

    # A genuinely newer one (sent after the correction) updates it.
    provider.inbound = [vacation(key="<ooo-2@example.com>", date="Fri, 09 Oct 2026 08:00:00 -0400",
                                 body="Change of plans: I am back on October 26.")]
    clock.at(2026, 10, 9, 15, 0)
    await handler._run_native()
    updated = await state.get_active_pause(pid)
    assert updated["resume_at"] == "2026-10-26T13:00:00" and updated["manual_override"] == 0
    assert updated["id"] == pause["id"]

    # A newer one with no date does not throw the known date away.
    provider.inbound = [vacation(key="<ooo-3@example.com>", date="Sat, 10 Oct 2026 08:00:00 -0400",
                                 body="I am out of the office.")]
    await handler._run_native()
    assert (await state.get_active_pause(pid))["resume_at"] == "2026-10-26T13:00:00"
    outcomes = {r["message_key"]: r["outcome"] for r in await state.auto_replies_for(pid)}
    assert outcomes == {"<ooo-1@example.com>": "paused", "<ooo-0@example.com>": "kept",
                        "<ooo-2@example.com>": "updated", "<ooo-3@example.com>": "kept"}


# ── What supersedes a pause ──


async def paused_contact(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, sender = await in_flight(state, provider, clock)
    provider.inbound = [vacation()]
    await make_handler_at(state, provider, clock.at(2026, 10, 6, 15, 0))._run_native()
    assert await state.get_active_pause(pid)
    return pid, sender, provider, clock


@pytest.mark.asyncio
async def test_a_human_reply_supersedes_the_pause(state):
    pid, sender, provider, clock = await paused_contact(state)
    provider.inbound = [InboundMessage(provider_id="h1", message_id="<h1@example.com>",
                                       from_email=EMAIL, subject="Re: hi Jane",
                                       body="Back early. Tell me more about this.")]
    await make_handler_at(state, provider, clock.at(2026, 10, 8, 15, 0),
                          intent="interested")._run_native()

    assert await state.get_active_pause(pid) is None
    (ended,) = await state.list_pauses(ended=True)
    assert ended["status"] == "superseded" and "replied" in ended["ended_reason"]
    assert (await state.get_prospect(pid)).status == "replied"

    clock.at(2026, 10, 21, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1                           # no cold step resumed

    # Replaying the old vacation reply must not pause (or resume) anything.
    provider.inbound = [vacation(key="<ooo-late@example.com>")]
    await make_handler_at(state, provider, clock)._run_native()
    assert await state.get_active_pause(pid) is None


@pytest.mark.asyncio
async def test_an_opt_out_while_paused_ends_it_for_good(state):
    pid, sender, provider, clock = await paused_contact(state)
    provider.inbound = [InboundMessage(provider_id="u1", message_id="<u1@example.com>",
                                       from_email=EMAIL, subject="Re: hi Jane",
                                       body="Please unsubscribe me.")]
    await make_handler_at(state, provider, clock.at(2026, 10, 8, 15, 0))._run_native()
    assert await state.get_active_pause(pid) is None
    assert (await state.get_prospect(pid)).status == "opted_out"
    clock.at(2026, 10, 21, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_a_bounce_while_paused_ends_it(state):
    pid, sender, provider, clock = await paused_contact(state)
    step1 = (await sequence_rows(state))[1]
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@example.net",
        subject="Delivery Status Notification (Failure)", body="address not found",
        in_reply_to=step1["message_id"], is_bounce=True)]
    await make_handler_at(state, provider, clock.at(2026, 10, 8, 15, 0))._run_native()
    assert await state.get_active_pause(pid) is None
    clock.at(2026, 10, 21, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_closing_or_excluding_a_paused_contact_stops_the_resume(state):
    pid, sender, provider, clock = await paused_contact(state)
    await state.update_prospect_status(pid, "lost")
    clock.at(2026, 10, 21, 15, 0)
    await sender._run_native()
    assert len(provider.sent) == 1
    (ended,) = await state.list_pauses(ended=True)
    assert ended["status"] == "superseded" and "lost" in ended["ended_reason"]

    # And the operator cannot resume someone whose sequence is over.
    other = await seed_prospect(state, email="sam@example.com", status="contacted")
    await state.record_auto_reply(message_key="<x@example.com>", kind="out_of_office",
                                  received_at="2026-10-21T10:00:00", prospect_id=other,
                                  parsed={"resume_at": None, "review_reason": "no_date"})
    await state.add_suppression("email", "sam@example.com", source="manual", actor="test")
    pause = await state.get_active_pause(other)
    with pytest.raises(PauseError) as err:
        await PauseService(state, TzCfg(), clock=clock).resume(pause["id"])
    assert err.value.code == "over"
    assert await state.get_active_pause(other) is None


# ── The sender's own checks ──


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", ["operator", "newer_reply"])
async def test_a_stale_pause_scan_respects_a_later_return_date(state, monkeypatch, correction):
    pid, sender, provider, clock = await paused_contact(state)
    clock.at(2026, 10, 20, 13, 5)
    original_due = state.due_pauses

    async def scan_then_extend(now):
        due = await original_due(now)
        assert len(due) == 1
        if correction == "operator":
            await PauseService(state, TzCfg(), clock=clock).set_return_date(
                due[0]["id"], "2026-10-26", actor="test")
        else:
            provider.inbound = [vacation(
                key="<ooo-extension@example.com>", date="Tue, 20 Oct 2026 09:01:00 -0400",
                body="I am still out of the office. Back October 26.")]
            await make_handler_at(state, provider, clock)._run_native()
        return due

    monkeypatch.setattr(state, "due_pauses", scan_then_extend)
    await sender._run_native()

    pause = await state.get_active_pause(pid)
    assert pause is not None
    assert pause["resume_at"] == "2026-10-26T13:00:00"
    assert len(provider.sent) == 1
    assert await actions(state, "ooo_resumed") == 0

    monkeypatch.setattr(state, "due_pauses", original_due)
    clock.at(2026, 10, 26, 13, 5)
    await sender._run_native()
    assert await state.get_active_pause(pid) is None
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_a_stale_due_scan_cannot_claim_a_contact_paused_since(state):
    provider = FakeProvider()
    clock = Clock(2026, 10, 5, 14, 0)
    pid, _ = await in_flight(state, provider, clock)
    due = [r for r in await state.get_outbox(status="approved", due_before="2026-10-09T00:00:00")
           if r["step"] == 2]
    assert due

    await state.record_auto_reply(
        message_key="<late@example.com>", kind="out_of_office", prospect_id=pid,
        received_at="2026-10-08T12:00:00",
        parsed={"resume_at": "2026-10-20T13:00:00", "text": "October 20", "confidence": 0.95})
    claim, detail = await state.claim_for_send(due[0], "mercury@x.co")
    assert claim == "ooo_pause" and detail["pause"]["prospect_id"] == pid
    assert (await state.get_outbox_item(due[0]["id"]))["status"] == "approved"


@pytest.mark.asyncio
async def test_staging_a_paused_contact_schedules_from_the_return_day(state):
    provider = FakeProvider()
    pid = await seed_prospect(state, email=EMAIL)
    await state.record_auto_reply(
        message_key="<o@example.com>", kind="out_of_office", prospect_id=pid,
        received_at="2026-10-05T12:00:00",
        parsed={"resume_at": "2026-10-20T13:00:00", "text": "October 20", "confidence": 0.95})
    await three_step_campaign(state, [pid])
    sender = make_sender_at(state, provider, Clock(2026, 10, 6, 14, 0))
    await sender._run_native()
    assert provider.sent == []
    rows = await sequence_rows(state)
    assert rows[1]["send_at"] == "2026-10-20T13:00:00"
    assert rows[1]["status"] == "approved"


@pytest.mark.asyncio
async def test_instantly_reports_that_it_cannot_pause(state):
    pid = await seed_prospect(state, email=EMAIL, status="contacted")
    handler = Handler(brain=StubBrain(intent="ooo"), state=state, config=TzCfg(), env=Env())
    handler.provider = None                                  # not a native provider
    await handler._process_reply(EMAIL, "I am out of the office until October 20.", "u-1")
    assert await state.get_active_pause(pid) is None
    assert await actions(state, "ooo_pause_unavailable") == 1
    assert await actions(state, "reply_received") == 0
