"""Pausing a cold sequence for an out-of-office reply, and resuming it.

Real temporary SQLite databases, a fake mail provider, and a clock the sender
takes as ``sender.clock``. The message dates are real "now" (the handler
refuses a Date header from the future), so the return dates in the bodies are
built from today and every expectation is computed from the same helpers.
"""

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from mercury.agents.handler import Handler, received_at
from mercury.agents.sender import Sender
from mercury.integrations.mail_provider import InboundMessage
from mercury.models.campaign import Campaign, EmailStep
from mercury.ooo import resume_time
from mercury.state import StateManager

from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg,
    Env,
    FakeProvider,
    StubBrain,
    seed_prospect,
    state,
)

NY = "America/New_York"


class OooCfg(Cfg):
    """The test config plus the operator's clock (New York, quiet until 07:00)."""

    class usage:
        class quiet_hours:
            timezone = NY
            start = "22:00"
            end = "07:00"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def header_date(when: datetime) -> str:
    return format_datetime(when.replace(tzinfo=timezone.utc))


def local_day(when: datetime, plus: int = 0) -> date:
    import pytz

    return pytz.UTC.localize(when).astimezone(pytz.timezone(NY)).date() + timedelta(days=plus)


def phrase(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}"


def make_sender(state, provider, require_approval=False):
    class Isolated(OooCfg):
        # The shared test config is class-level; give each sender its own so
        # changing a setting here cannot leak into another test.
        class channels(OooCfg.channels):
            class email(OooCfg.channels.email):
                pass

    cfg = Isolated()
    cfg.channels.email.require_approval = require_approval
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = provider
    sender.send_pacing = False
    return sender


def make_handler(state, provider, intent="question", cfg=None):
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=cfg or OooCfg(), env=Env())
    handler.provider = provider
    return handler


def auto_reply(body, *, mid="<ooo1@acme>", at=None, subject="Automatic reply: Out of Office",
               sender="jane@acme.com", in_reply_to="", headers=None):
    return InboundMessage(
        provider_id=mid, from_email=sender, subject=subject, body=body, message_id=mid,
        in_reply_to=in_reply_to, date=header_date(at or utcnow()),
        headers=headers if headers is not None else {"Auto-Submitted": "auto-replied"},
    )


async def seed_sequence(state, prospect_id, delays=(0, 3), status="draft"):
    campaign = Campaign(
        id="", name="seq", channel="email",
        sequence=[
            EmailStep(step=i + 1, subject=f"hi {{{{first_name}}}} {i + 1}",
                      body=f"Body {i + 1} for {{{{company}}}}. Question?", delay_days=d)
            for i, d in enumerate(delays)
        ],
        prospect_ids=[prospect_id], status=status,
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


async def started(state, provider, delays=(0, 3), require_approval=False):
    """A prospect whose first email has gone out, follow-ups queued."""
    pid = await seed_prospect(state)
    campaign = await seed_sequence(state, pid, delays)
    sender = make_sender(state, provider, require_approval=require_approval)
    await sender._run_native()
    return pid, campaign, sender


async def steps(state, pid):
    rows = [r for r in await state.get_outbox(limit=100) if r["prospect_id"] == pid]
    return {r["step"]: r for r in rows}


# ── the happy path, end to end ──


@pytest.mark.asyncio
async def test_vacation_reply_pauses_followups_until_the_return_date_then_sends_one_step(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider, delays=(0, 3, 4))
    assert [m["subject"] for m in provider.sent] == ["hi Jane 1"]

    back = local_day(utcnow(), 13)
    handler = make_handler(state, provider)
    provider.inbound = [auto_reply(f"I am out of the office until {phrase(back)}.")]
    await handler._run_native()

    pause = await state.get_active_pause(pid)
    expected = resume_time(back, NY, "07:00")
    assert pause["state"] == "paused"
    assert datetime.fromisoformat(pause["resume_at"]) == expected
    assert pause["return_text"] == phrase(back)
    assert pause["confidence"] >= 0.9
    # The pause is not a sales outcome: stage and outreach status are untouched.
    assert (await state.get_prospect(pid)).status == "contacted"
    assert await state.get_conversations_by_status("open") == []

    # Day 4 is when step 2 was due; it does not go, nor does any later cycle
    # up to the return date.
    for days in (4, 8, 12):
        sender.clock = lambda d=days: utcnow() + timedelta(days=d)
        await sender._run_native()
    assert len(provider.sent) == 1

    # On the return date exactly one step goes, and step 3 keeps its own gap.
    sender.clock = lambda: expected + timedelta(hours=1)
    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["hi Jane 1", "hi Jane 2"]
    rows = await steps(state, pid)
    assert rows[2]["status"] == "sent"
    assert rows[3]["status"] == "approved"
    # Step 3 is delay_days (4) after step 2's new time, not firing alongside it.
    assert datetime.fromisoformat(rows[3]["send_at"]) == expected + timedelta(days=4)
    assert (await state.get_pause(pid))["state"] == "resumed"

    sender.clock = lambda: expected + timedelta(days=2)
    await sender._run_native()
    assert len(provider.sent) == 2
    sender.clock = lambda: expected + timedelta(days=4, hours=1)
    await sender._run_native()
    assert len(provider.sent) == 3


@pytest.mark.asyncio
async def test_resume_does_not_fire_every_overdue_step_at_once(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider, delays=(0, 1, 1, 1))
    back = local_day(utcnow(), 30)
    handler = make_handler(state, provider)
    provider.inbound = [auto_reply(f"Out of office until {phrase(back)}")]
    await handler._run_native()
    resume = resume_time(back, NY, "07:00")

    # Ten days past the return date every original send time is overdue.
    late = resume + timedelta(days=10)
    sender.clock = lambda: late
    await sender._run_native()
    assert len(provider.sent) == 2            # one more step, not three
    rows = await steps(state, pid)
    assert rows[2]["status"] == "sent"
    # Step 3 waits its own gap after step 2 really went out; step 4 behind it.
    assert datetime.fromisoformat(rows[3]["send_at"]) == late + timedelta(days=1)
    assert rows[3]["status"] == rows[4]["status"] == "approved"
    sender.clock = lambda: late + timedelta(hours=2)
    await sender._run_native()
    assert len(provider.sent) == 2
    sender.clock = lambda: late + timedelta(days=1, hours=1)
    await sender._run_native()
    assert len(provider.sent) == 3


@pytest.mark.asyncio
async def test_a_weekend_return_resumes_monday_morning(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    saturday = local_day(utcnow(), 20)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    provider.inbound = [auto_reply(f"I will be back on {phrase(saturday)}.")]
    await make_handler(state, provider)._run_native()
    pause = await state.get_active_pause(pid)
    monday = saturday + timedelta(days=2)
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(monday, NY, "07:00")
    assert datetime.fromisoformat(pause["resume_at"]) > resume_time(saturday - timedelta(days=1), NY, "07:00")


@pytest.mark.asyncio
async def test_resume_buffer_setting_adds_business_days_after_the_return_date(state):
    class BufferCfg(OooCfg):
        class channels(OooCfg.channels):
            class email(OooCfg.channels.email):
                ooo_resume_buffer_days = 2

    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = local_day(utcnow(), 20)
    while back.weekday() != 3:  # a Thursday, so the buffer crosses a weekend
        back += timedelta(days=1)
    provider.inbound = [auto_reply(f"I am out of the office until {phrase(back)}.")]
    await make_handler(state, provider, cfg=BufferCfg())._run_native()
    pause = await state.get_active_pause(pid)
    monday = back + timedelta(days=4)
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(monday, NY, "07:00")
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(back, NY, "07:00", 2)


@pytest.mark.asyncio
async def test_resume_time_uses_the_configured_timezone_and_quiet_hours_end(state):
    class DrCfg(OooCfg):
        class usage:
            class quiet_hours:
                timezone = "America/Santo_Domingo"
                start = "21:00"
                end = "08:30"

    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = utcnow().date() + timedelta(days=15)
    while back.weekday() >= 5:
        back += timedelta(days=1)
    provider.inbound = [auto_reply(f"Estaré fuera de la oficina hasta el {back.day} de "
                                   f"{['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][back.month - 1]}.")]
    await make_handler(state, provider, cfg=DrCfg())._run_native()
    pause = await state.get_active_pause(pid)
    # 08:30 in Santo Domingo (UTC-4 all year) is 12:30 UTC.
    assert datetime.fromisoformat(pause["resume_at"]) == datetime.combine(back, datetime.min.time()) + timedelta(hours=12, minutes=30)


# ── no usable date: paused for review, never guessed ──


@pytest.mark.parametrize("body,reason", [
    ("I am out of the office until further notice.", "none"),
    ("I will be back on 10/11.", "ambiguous"),
    ("Back on 31/02.", "invalid"),
    ("I am out of the office until September 1.", "past"),
    ("Out until 2031-01-01.", "too_far"),
])
@pytest.mark.asyncio
async def test_no_usable_date_pauses_for_review_and_never_resumes_by_itself(state, body, reason):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider)
    provider.inbound = [auto_reply(body)]
    await make_handler(state, provider)._run_native()

    pause = await state.get_active_pause(pid)
    assert pause["state"] == "needs_review"
    assert pause["review_reason"] == reason
    assert pause["resume_at"] is None

    # A year later it is still held: nothing resumes an undated pause silently.
    sender.clock = lambda: utcnow() + timedelta(days=400)
    await sender._run_native()
    assert len(provider.sent) == 1
    assert (await state.get_active_pause(pid))["state"] == "needs_review"
    assert (await steps(state, pid))[2]["status"] == "approved"


@pytest.mark.asyncio
async def test_operator_sets_a_date_and_the_pause_ends_on_it(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider)
    provider.inbound = [auto_reply("Out of the office, back soon.")]
    await make_handler(state, provider)._run_native()
    assert (await state.get_active_pause(pid))["state"] == "needs_review"

    when = resume_time(local_day(utcnow(), 9), NY, "07:00")
    pause = await state.override_pause(pid, when)
    assert pause["state"] == "paused" and pause["manual_override"] == 1
    assert datetime.fromisoformat(pause["resume_at"]) == when

    sender.clock = lambda: when - timedelta(minutes=1)
    await sender._run_native()
    assert len(provider.sent) == 1
    sender.clock = lambda: when + timedelta(minutes=1)
    await sender._run_native()
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_operator_resume_sends_the_next_step_on_normal_pacing(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider, delays=(0, 3, 3))
    provider.inbound = [auto_reply("Out of the office until further notice.")]
    await make_handler(state, provider)._run_native()

    now = utcnow() + timedelta(days=5)
    result = await state.resume_pause(pid, "operator", now=now)
    assert result["state"] == "resumed" and result["ended_reason"] == "operator"
    sender.clock = lambda: now + timedelta(minutes=5)
    await sender._run_native()
    assert len(provider.sent) == 2                       # step 2 only
    rows = await steps(state, pid)
    assert rows[3]["status"] == "approved"
    assert datetime.fromisoformat(rows[3]["send_at"]) >= now.replace(microsecond=0) + timedelta(days=3)
    assert await state.resume_pause(pid, "operator") is None     # nothing left to resume


@pytest.mark.asyncio
async def test_an_operator_date_in_the_past_resumes_now(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    provider.inbound = [auto_reply("Out of the office, no date.")]
    await make_handler(state, provider)._run_native()
    result = await state.override_pause(pid, utcnow() - timedelta(days=1))
    assert result["state"] == "resumed"
    assert await state.override_pause(pid, utcnow() + timedelta(days=3)) is None   # nothing active


# ── approvals, caps and the other gates stay intact ──


@pytest.mark.asyncio
async def test_pausing_and_resuming_never_changes_approval_status_or_text(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider, delays=(0, 3, 4), require_approval=True)
    # Opener approved and sent; follow-ups still waiting for review.
    first = (await steps(state, pid))[1]
    assert first["status"] == "pending_review"
    await state.approve_outbox(first["id"])
    await sender._run_native()
    assert len(provider.sent) == 1
    before = await steps(state, pid)
    assert before[2]["status"] == before[3]["status"] == "pending_review"

    back = local_day(utcnow(), 12)
    provider.inbound = [auto_reply(f"Out of office until {phrase(back)}")]
    await make_handler(state, provider)._run_native()
    resume = resume_time(back, NY, "07:00")
    await state.approve_outbox(before[2]["id"])          # reviewer approves step 2 mid-pause
    sender.clock = lambda: resume - timedelta(hours=1)
    await sender._run_native()
    assert len(provider.sent) == 1                       # approved, still held

    sender.clock = lambda: resume + timedelta(hours=1)
    await sender._run_native()
    assert len(provider.sent) == 2
    after = await steps(state, pid)
    assert after[3]["status"] == "pending_review"        # never promoted by the pause
    assert after[3]["subject"] == before[3]["subject"] and after[3]["body"] == before[3]["body"]
    assert after[2]["subject"] == before[2]["subject"]


@pytest.mark.asyncio
async def test_a_resumed_step_still_obeys_the_daily_cap_and_kill_switch(state, monkeypatch):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider)
    back = local_day(utcnow(), 6)
    provider.inbound = [auto_reply(f"Out of office until {phrase(back)}")]
    await make_handler(state, provider)._run_native()
    resume = resume_time(back, NY, "07:00")

    await state.set_setting("sending_paused", "test")
    sender.clock = lambda: resume + timedelta(hours=1)
    await sender._run_native()
    assert len(provider.sent) == 1
    await state.set_setting("sending_paused", "")
    # The opener already used today's cap. (The test config is class-level, so
    # this must go through monkeypatch to be undone.)
    monkeypatch.setattr(sender.config.channels.email, "max_daily_sends", 1)
    await sender._run_native()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_a_stale_due_scan_cannot_send_into_a_pause(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider)
    scanned = [r for r in await state.get_outbox(status="approved", limit=50)]
    assert scanned and scanned[0]["step"] == 2

    # The scan above was read, then a vacation notice landed.
    sender.clock = lambda: utcnow() + timedelta(days=10)
    provider.inbound = [auto_reply("I am out of the office until further notice.")]
    await make_handler(state, provider)._run_native()

    async def stale_get_outbox(*_a, **_k):
        return scanned
    state.get_outbox = stale_get_outbox
    await sender._drain_due()
    assert len(provider.sent) == 1
    # And the claim itself refuses a paused prospect.
    assert not await state.claim_outbox_item(scanned[0], "mercury@x.co")
    assert (await state.get_outbox_item(scanned[0]["id"]))["status"] == "approved"


@pytest.mark.asyncio
async def test_replies_to_a_person_are_not_held_by_a_pause(state):
    provider = FakeProvider()
    pid, _c, sender = await started(state, provider)
    provider.inbound = [auto_reply("Out of the office until further notice.")]
    await make_handler(state, provider)._run_native()
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="Re: hi", body="Thanks Jane.",
        send_at=utcnow().isoformat(), status="approved", kind="reply",
    )
    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["hi Jane 1", "Re: hi"]


# ── a person, an opt-out, a bounce or a closure supersede the pause ──


async def pause_for_review(state, provider):
    pid, _c, sender = await started(state, provider)
    provider.inbound = [auto_reply("Out of the office, no date.", mid="<a1@x>")]
    handler = make_handler(state, provider)
    await handler._run_native()
    assert (await state.get_active_pause(pid))["state"] == "needs_review"
    return pid, sender, handler


@pytest.mark.asyncio
async def test_a_human_reply_supersedes_the_pause_and_stops_the_sequence(state):
    provider = FakeProvider()
    pid, sender, _h = await pause_for_review(state, provider)
    provider.inbound = [InboundMessage(
        provider_id="h1", from_email="jane@acme.com", subject="Re: hi", message_id="<h1@x>",
        body="Interesting, can you send pricing?", date=header_date(utcnow()),
    )]
    await make_handler(state, provider, intent="question")._run_native()

    assert (await state.get_prospect(pid)).status == "replied"
    pause = await state.get_pause(pid)
    assert pause["state"] == "superseded" and pause["ended_reason"] == "human reply"
    assert (await steps(state, pid))[2]["status"] == "cancelled"
    # And an old pause cannot hold or revive anything: the sequence is over.
    sender.clock = lambda: utcnow() + timedelta(days=30)
    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["hi Jane 1"]


@pytest.mark.asyncio
async def test_a_human_message_that_mentions_vacation_is_still_a_human_reply(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    provider.inbound = [InboundMessage(
        provider_id="h1", from_email="jane@acme.com", subject="Re: hi", message_id="<h1@x>",
        body="I'm on vacation until Friday but yes, let's talk about pricing after.",
        date=header_date(utcnow()),
    )]
    await make_handler(state, provider, intent="interested")._run_native()
    assert (await state.get_prospect(pid)).status == "replied"
    assert await state.get_pause(pid) is None
    assert (await steps(state, pid))[2]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_opt_out_while_paused_supersedes(state):
    provider = FakeProvider()
    pid, _s, _h = await pause_for_review(state, provider)
    provider.inbound = [InboundMessage(
        provider_id="h2", from_email="jane@acme.com", subject="Re: hi", message_id="<h2@x>",
        body="Please unsubscribe me.", date=header_date(utcnow()),
    )]
    await make_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.get_pause(pid))["state"] == "superseded"


@pytest.mark.asyncio
async def test_bounce_while_paused_supersedes(state):
    provider = FakeProvider()
    pid, _s, _h = await pause_for_review(state, provider)
    sent = [r for r in await state.get_outbox(status="sent")][0]
    await state.update_outbox_item(sent["id"], message_id="<m1@x>")
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)", body="couldn't be delivered",
        in_reply_to="<m1@x>", is_bounce=True,
    )]
    await make_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert (await state.get_pause(pid))["state"] == "superseded"
    assert (await steps(state, pid))[2]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_closing_a_contact_supersedes_the_pause(state):
    provider = FakeProvider()
    pid, _s, _h = await pause_for_review(state, provider)
    await state.update_prospect_status(pid, "lost")
    assert (await state.get_pause(pid))["state"] == "superseded"
    assert await state.get_active_pause(pid) is None


@pytest.mark.asyncio
async def test_a_vacation_notice_from_someone_already_closed_pauses_nothing(state):
    provider = FakeProvider()
    pid = await seed_prospect(state, status="replied")
    provider.inbound = [auto_reply("Out of the office until further notice.")]
    await make_handler(state, provider)._run_native()
    assert await state.get_pause(pid) is None


# ── duplicates, replays and updates ──


@pytest.mark.asyncio
async def test_duplicate_ingestion_does_not_duplicate_or_move_the_pause(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = local_day(utcnow(), 10)
    msg = auto_reply(f"Out until {phrase(back)}", mid="<same@x>")
    provider.inbound = [msg]
    handler = make_handler(state, provider)
    await handler._run_native()
    first = await state.get_pause(pid)
    await handler._run_native()                      # polled again: handler dedup

    # Same Message-ID under a different provider id (e.g. another mailbox saw it).
    twin = auto_reply(f"Out until {phrase(local_day(utcnow(), 40))}", mid="<same@x>")
    twin.provider_id = "other-id"
    provider.inbound = [twin]
    await handler._run_native()

    again = await state.get_pause(pid)
    assert again == first
    async with state._connect() as db:
        async with db.execute("SELECT COUNT(*) FROM sequence_pauses WHERE prospect_id = ?", (pid,)) as c:
            assert (await c.fetchone())[0] == 1
        async with db.execute("SELECT COUNT(*) FROM actions WHERE action_type = 'sequence_paused'") as c:
            assert (await c.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_a_newer_vacation_notice_updates_the_pause(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    t0 = utcnow() - timedelta(hours=2)
    d1, d2 = local_day(utcnow(), 10), local_day(utcnow(), 20)
    provider.inbound = [auto_reply(f"Out until {phrase(d1)}", mid="<a@x>", at=t0)]
    handler = make_handler(state, provider)
    await handler._run_native()
    provider.inbound = [auto_reply(f"Extended: out until {phrase(d2)}", mid="<b@x>", at=t0 + timedelta(hours=1))]
    await handler._run_native()
    pause = await state.get_active_pause(pid)
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(d2, NY, "07:00")
    assert pause["trigger_message_id"] == "<b@x>"

    # An older notice replayed later does not move it back.
    provider.inbound = [auto_reply(f"Out until {phrase(d1)}", mid="<c@x>", at=t0 + timedelta(minutes=5))]
    await handler._run_native()
    assert (await state.get_active_pause(pid))["resume_at"] == pause["resume_at"]


@pytest.mark.asyncio
async def test_replaying_an_old_message_never_overwrites_an_operator_date(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    t0 = utcnow() - timedelta(days=1)
    d1 = local_day(utcnow(), 10)
    provider.inbound = [auto_reply(f"Out until {phrase(d1)}", mid="<a@x>", at=t0)]
    handler = make_handler(state, provider)
    await handler._run_native()

    operator_day = resume_time(local_day(utcnow(), 25), NY, "07:00")
    await state.override_pause(pid, operator_day)
    # A different, older notice from the same person shows up after the edit.
    provider.inbound = [auto_reply(f"Out until {phrase(local_day(utcnow(), 3))}", mid="<old@x>",
                                   at=t0 + timedelta(hours=1))]
    await handler._run_native()
    pause = await state.get_active_pause(pid)
    assert datetime.fromisoformat(pause["resume_at"]) == operator_day
    assert pause["manual_override"] == 1

    # A genuinely newer notice may replace it.
    d3 = local_day(utcnow(), 14)
    provider.inbound = [auto_reply(f"Out until {phrase(d3)}", mid="<new@x>",
                                   at=utcnow() + timedelta(minutes=1))]
    await handler._run_native()
    pause = await state.get_active_pause(pid)
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(d3, NY, "07:00")
    assert pause["manual_override"] == 0


@pytest.mark.asyncio
async def test_a_later_notice_without_a_date_keeps_the_date_already_held(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = local_day(utcnow(), 10)
    t0 = utcnow() - timedelta(hours=3)
    provider.inbound = [auto_reply(f"Out until {phrase(back)}", mid="<a@x>", at=t0)]
    handler = make_handler(state, provider)
    await handler._run_native()
    held = (await state.get_active_pause(pid))["resume_at"]
    provider.inbound = [auto_reply("I am out of the office.", mid="<b@x>", at=t0 + timedelta(hours=1))]
    await handler._run_native()
    pause = await state.get_active_pause(pid)
    assert pause["state"] == "paused" and pause["resume_at"] == held


@pytest.mark.asyncio
async def test_a_replayed_notice_after_the_pause_ended_does_not_re_pause(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    t0 = utcnow() - timedelta(hours=5)
    provider.inbound = [auto_reply("Out of the office, no date.", mid="<a@x>", at=t0)]
    handler = make_handler(state, provider)
    await handler._run_native()
    await state.resume_pause(pid, "operator")
    # The same person's *older* notice arrives again (different Message-ID).
    provider.inbound = [auto_reply("Out of the office, no date.", mid="<a2@x>", at=t0 + timedelta(hours=1))]
    await handler._run_native()
    assert await state.get_active_pause(pid) is None
    assert (await state.get_pause(pid))["state"] == "resumed"


# ── what is and is not a vacation reply ──


@pytest.mark.asyncio
async def test_acknowledgements_and_receipts_do_not_pause(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    provider.inbound = [
        auto_reply("Thank you for contacting Acme. Your ticket number is #4821.",
                   mid="<t1@x>", subject="Automatic reply: your message",
                   headers={"Auto-Submitted": "auto-generated"}),
        auto_reply("Your message was displayed.", mid="<r1@x>", subject="Read: hi", headers={}),
        auto_reply("Weekly digest", mid="<n1@x>", subject="Digest",
                   headers={"Precedence": "bulk"}),
    ]
    await make_handler(state, provider)._run_native()
    assert await state.get_pause(pid) is None
    assert (await steps(state, pid))[2]["status"] == "approved"
    async with state._connect() as db:
        async with db.execute(
            "SELECT json_extract(details_json, '$.kind') FROM actions "
            "WHERE action_type = 'auto_reply' ORDER BY rowid") as c:
            kinds = [r[0] for r in await c.fetchall()]
    assert kinds == ["acknowledgement", "receipt", "acknowledgement"]


@pytest.mark.asyncio
async def test_vacation_reply_is_an_audit_record_not_a_reply(state):
    from mercury import metrics

    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    provider.inbound = [auto_reply(f"Out until {phrase(local_day(utcnow(), 9))}")]
    await make_handler(state, provider)._run_native()

    assert await state.get_conversations_by_status("open") == []
    async with state._connect() as db:
        async with db.execute(
            "SELECT action_type FROM actions WHERE action_type IN ('reply_received', 'auto_reply', "
            "'sequence_paused') ORDER BY rowid") as c:
            logged = [r[0] for r in await c.fetchall()]
    assert logged == ["auto_reply", "sequence_paused"]
    today = utcnow().date()
    counts = await metrics.daily_counts(state.db_path, today - timedelta(days=1), today + timedelta(days=1))
    assert sum(d["replies"] for d in counts.values()) == 0
    assert sum(d["positive"] for d in counts.values()) == 0
    assert sum(d["sent"] for d in counts.values()) == 1


@pytest.mark.asyncio
async def test_a_vacation_notice_sent_from_another_address_matches_by_thread(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    sent = (await state.get_outbox(status="sent"))[0]
    await state.update_outbox_item(sent["id"], message_id="<m1@x>")
    back = local_day(utcnow(), 11)
    provider.inbound = [auto_reply(f"Jane is out of the office until {phrase(back)}.",
                                   sender="assistant@acme.com", in_reply_to="<m1@x>")]
    await make_handler(state, provider)._run_native()
    assert (await state.get_active_pause(pid))["state"] == "paused"


@pytest.mark.asyncio
async def test_quoted_text_does_not_supply_the_return_date(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    body = ("I am out of the office.\n\n"
            "On Mon, Oct 5, 2026 at 9:00 AM Mercury wrote:\n> Can we talk until October 30?\n")
    provider.inbound = [auto_reply(body)]
    await make_handler(state, provider)._run_native()
    assert (await state.get_active_pause(pid))["state"] == "needs_review"


@pytest.mark.asyncio
async def test_a_vacation_reply_the_classifier_finds_pauses_too(state):
    """No Auto-Submitted header, so the message goes to the intent classifier."""
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = local_day(utcnow(), 8)
    provider.inbound = [InboundMessage(
        provider_id="c1", from_email="jane@acme.com", subject="Re: hi", message_id="<c1@x>",
        body=f"Hi, I'm on vacation and will be back on {phrase(back)}.", date=header_date(utcnow()),
    )]
    await make_handler(state, provider, intent="ooo")._run_native()
    pause = await state.get_active_pause(pid)
    assert pause["state"] == "paused"
    assert datetime.fromisoformat(pause["resume_at"]) == resume_time(back, NY, "07:00")
    assert (await state.get_prospect(pid)).status == "contacted"
    assert await state.get_conversations_by_status("open") == []


@pytest.mark.asyncio
async def test_the_legacy_instantly_path_does_not_pretend_to_pause(state):
    class InstantlyCfg(OooCfg):
        class channels(OooCfg.channels):
            class email(OooCfg.channels.email):
                provider = "instantly"

    pid = await seed_prospect(state, status="contacted")
    handler = Handler(brain=StubBrain(intent="ooo"), state=state, config=InstantlyCfg(), env=Env())
    assert not handler.is_native
    await handler._process_reply("jane@acme.com", "I'm away until November 3.", "u1")
    assert await state.get_pause(pid) is None
    async with state._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM actions WHERE action_type = 'sequence_pause_unavailable'") as c:
            assert (await c.fetchone())[0] == 1


def test_received_at_reads_the_date_header_and_distrusts_the_future():
    assert received_at("Wed, 07 Oct 2026 15:00:00 +0000") == datetime(2026, 10, 7, 15, 0)
    assert received_at("Wed, 07 Oct 2026 11:00:00 -0400") == datetime(2026, 10, 7, 15, 0)
    assert abs(received_at("garbage") - utcnow()) < timedelta(seconds=5)
    assert abs(received_at("") - utcnow()) < timedelta(seconds=5)
    assert abs(received_at("Wed, 07 Oct 2099 15:00:00 +0000") - utcnow()) < timedelta(seconds=5)


# ── staging, durability, resuming twice ──


@pytest.mark.asyncio
async def test_staging_for_a_paused_prospect_starts_after_the_return_date(state):
    provider = FakeProvider()
    pid = await seed_prospect(state, status="new")
    back = local_day(utcnow(), 16)
    await state.record_ooo_pause(
        pid, message_id="<a@x>", message_at=utcnow(), state="paused",
        resume_at=resume_time(back, NY, "07:00"), return_text="x",
    )
    await seed_sequence(state, pid, (0, 3))
    sender = make_sender(state, provider, require_approval=True)
    await sender._run_native()
    rows = await steps(state, pid)
    base = resume_time(back, NY, "07:00")
    assert datetime.fromisoformat(rows[1]["send_at"]) == base
    assert datetime.fromisoformat(rows[2]["send_at"]) == base + timedelta(days=3)
    assert rows[1]["status"] == rows[2]["status"] == "pending_review"


@pytest.mark.asyncio
async def test_pauses_survive_a_restart(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    back = local_day(utcnow(), 12)
    provider.inbound = [auto_reply(f"Out until {phrase(back)}")]
    await make_handler(state, provider)._run_native()
    resume = resume_time(back, NY, "07:00")

    # A new process: new state manager over the same file, new sender.
    reborn = StateManager(state.db_path)
    await reborn.init_db()
    pause = await reborn.get_active_pause(pid)
    assert pause and datetime.fromisoformat(pause["resume_at"]) == resume
    sender = make_sender(reborn, provider)
    sender.clock = lambda: resume - timedelta(days=1)
    await sender._run_native()
    assert len(provider.sent) == 1
    sender.clock = lambda: resume + timedelta(hours=1)
    await sender._run_native()
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_resuming_twice_reschedules_once(state):
    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    provider.inbound = [auto_reply("Out of the office, no date.")]
    await make_handler(state, provider)._run_native()
    now = utcnow() + timedelta(days=9)
    first = await state.resume_pause(pid, "operator", now=now)
    assert first["rescheduled"] == 1
    assert await state.resume_pause(pid, "operator", now=now) is None
    assert await state.resume_due_pauses(now + timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_resume_due_only_releases_pauses_that_are_due(state):
    provider = FakeProvider()
    a = await seed_prospect(state, email="a@acme.com")
    b = await seed_prospect(state, email="b@acme.com")
    now = utcnow()
    for pid, days in ((a, 2), (b, 9)):
        await state.record_ooo_pause(pid, message_id=f"<{pid}>", message_at=now, state="paused",
                                     resume_at=now + timedelta(days=days), return_text="x")
    due = await state.resume_due_pauses(now + timedelta(days=3))
    assert [p["prospect_id"] for p in due] == [a]
    assert (await state.get_pause(b))["state"] == "paused"
    assert {p["prospect_id"] for p in await state.list_pauses()} == {b}


@pytest.mark.asyncio
async def test_paused_mail_does_not_count_as_ready_to_send(state):
    """main.decide_next_action counts approved mail; paused mail must not win the cycle."""
    import aiosqlite

    provider = FakeProvider()
    pid, _c, _s = await started(state, provider)
    sql = ("SELECT COUNT(*) FROM outbox o JOIN prospects p ON p.email = o.to_email "
           "WHERE o.status = 'approved' AND o.sent_at IS NULL "
           "AND NOT (o.kind = 'sequence' AND EXISTS (SELECT 1 FROM sequence_pauses sp "
           "WHERE sp.prospect_id = o.prospect_id AND sp.state IN ('paused', 'needs_review'))) "
           "AND p.email_status IN ('verified')")
    async with aiosqlite.connect(state.db_path) as db:
        assert (await (await db.execute(sql)).fetchone())[0] == 1
    provider.inbound = [auto_reply("Out of the office, no date.")]
    await make_handler(state, provider)._run_native()
    async with aiosqlite.connect(state.db_path) as db:
        assert (await (await db.execute(sql)).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_v14_migration_is_additive_on_an_existing_database():
    import aiosqlite

    from mercury.state import MIGRATIONS, _split_sql

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "old.db")
        async with aiosqlite.connect(path) as db:
            for script in MIGRATIONS[:13]:
                for stmt in _split_sql(script):
                    await db.execute(stmt)
            await db.execute("PRAGMA user_version = 13")
            await db.execute("INSERT INTO prospects (id, first_name, email) VALUES ('p1', 'A', 'a@x.co')")
            await db.commit()
        sm = StateManager(path)
        await sm.init_db()
        await sm.init_db()                 # idempotent
        assert (await sm.get_prospect("p1")).email == "a@x.co"
        assert await sm.get_pause("p1") is None
        result = await sm.record_ooo_pause("p1", message_id="<m>", message_at=utcnow(),
                                           state="needs_review", review_reason="none")
        assert result["action"] == "created"
