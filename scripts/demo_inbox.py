"""Inbox demo data for scripts/seed_demo.py: stored mail for the seeded threads.

The main seed records conversations the way Mercury did before inbound mail
was stored (text in ``thread_json`` only). This module gives every one of
them real stored messages, so the Inbox has something to triage:

* every reply stored in ``inbound_messages`` on the mailbox it reached, across
  all three demo mailboxes, answering the email it replied to;
* two Mercury drafts waiting for review, one reply approved for later today,
  one escalated thread, one opted-out contact (excluded), one snoozed
  thread, a reminder that is due, contact notes, and one reply Mercury could
  not process (it shows on Today).

All of it is synthetic. Nothing is ever sent.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from mercury.models.conversation import Message

MB_WARM = "jordan@northwind-outreach.com"
MB_ALEX = "alex@getnorthwind.com"
MB_SAM = "sam@trynorthwind.com"
OFFER = "offer_a"  # defined in the demo config by demo_outbox.py

# first name -> how their thread looks. ``at`` is how long ago they last
# wrote; ``earlier`` are older messages as (sender, text, hours before ``at``).
SCRIPT = {
    "Greg": dict(
        mailbox=MB_ALEX, intent="interested", stage="engaged", at=timedelta(minutes=35),
        steps=2, unread=True,
        text="Yeah we've noticed the drop. What would this look like for us?",
        draft=("Hi Greg,\n\nHappy to show you. It is a 15-minute call where I walk through the "
               "two listing changes I would make first, using your own page. Does Thursday at 10 "
               "or Friday at 2 work?\n\nAlex"),
        note="Prefers mornings. A second location opens in the spring.",
        signals=[("SERP_RANK", 14), ("NO_WEBSITE", None)],
    ),
    "Dana": dict(
        mailbox=MB_WARM, intent="objection", stage="engaged", at=timedelta(hours=2, minutes=10),
        steps=2, unread=True,
        text="We already have someone for this, but what do you charge?",
        draft=("Hi Dana,\n\nFair question. It is a flat monthly fee, and most shops keep their "
               "current agency for ads and use us only for the listing work. Want the one-page "
               "breakdown?\n\nJordan"),
    ),
    "Ryan": dict(
        mailbox=MB_ALEX, intent="question", stage="engaged", at=timedelta(days=1, hours=3),
        steps=1, unread=True,
        text="Do you work with companies our size? We are four people.",
    ),
    "Tina": dict(
        mailbox=MB_WARM, intent="interested", stage="closing", at=timedelta(days=2, hours=1),
        steps=1, unread=False,
        earlier=[("prospect", "Sounds useful. Could we talk this week?", 26),
                 ("mercury", "Glad to. Thursday at 10 or Friday at 2?", 24)],
        text="Thursday works.",
        reminder="Confirm Thursday's call and send the calendar invite.",
    ),
    "Erin": dict(
        mailbox=MB_ALEX, intent="interested", stage="qualifying", at=timedelta(days=2, hours=4),
        steps=2, unread=False,
        text="Can you send a bit more detail first?",
        approved=("Hi Erin,\n\nOf course. Here is what changes first: your hours, two service "
                  "photos and the booking link. Ten minutes on a call covers the rest if it "
                  "helps.\n\nAlex"),
    ),
    "Matt": dict(
        mailbox=MB_WARM, intent="question", stage="presenting", at=timedelta(days=3, hours=2),
        steps=2, unread=False,
        text="Let me talk to my partner and get back to you next week.",
        snooze=True,
    ),
    "Beth": dict(
        mailbox=MB_WARM, intent="not_interested", stage="closed_lost", status="closed",
        at=timedelta(days=3, hours=5), steps=1, unread=False,
        text="We're locked in with our agency through next year. Please take us off your list.",
        opted_out=True, note="Agency contract renews next October.",
    ),
    "Sam": dict(
        mailbox=MB_ALEX, intent="escalate", stage="closing", status="needs_human",
        at=timedelta(hours=5), steps=1, unread=True,
        text=("Before we go further I need our lawyer to look at the contract terms. Can someone "
              "call me directly this week?"),
    ),
    "Holly": dict(
        mailbox=MB_SAM, intent="interested", stage="closing", at=timedelta(days=4),
        steps=1, unread=False, direct=True,
        text=("Derek forwarded your note to me. We would like to see it. What does onboarding "
              "look like?"),
    ),
}


def _iso(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def _local_today_at(now_utc: datetime, hour: int) -> datetime:
    """``hour`` o'clock today on this machine's clock, as naive UTC."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone()
    at = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return at.astimezone(timezone.utc).replace(tzinfo=None)


async def _sql(sm, sql: str, params=()):
    async with sm._connect() as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await db.commit()
        return rows


async def _store(sm, *, cid, pid, email, mailbox, subject, body, at, intent, answers="",
                 status="processed", error="", attempts=1):
    """One received message, stored and handled the way the Handler does."""
    domain = email.split("@")[-1]
    key = uuid.uuid4().hex[:12]
    row, _ = await sm.record_inbound(
        provider="smtp", mailbox=mailbox, external_id=f"demo-{key}",
        rfc_message_id=f"<{key}@{domain}>", in_reply_to=answers, thread_references=answers,
        from_email=email, subject=subject, body=body,
        date_header=format_datetime(at.replace(tzinfo=timezone.utc)))
    await sm.link_inbound(row["id"], prospect_id=pid, conversation_id=cid, intent=intent)
    await sm.finish_inbound(row["id"], status, error)
    await _sql(sm, "UPDATE inbound_messages SET created_at = ?, processed_at = ?, attempts = ? "
                   "WHERE id = ?", (_iso(at), _iso(at), attempts, row["id"]))
    return row["id"]


async def seed_inbox(sm, now: datetime) -> dict:
    from mercury.signals import seed_signal_catalog

    await seed_signal_catalog(sm)
    convos = await _sql(sm, """
        SELECT c.id, c.prospect_id, c.thread_json, c.intent, c.created_at, p.first_name, p.email,
               p.company_id, COALESCE(co.name, p.company) AS company
        FROM conversations c JOIN prospects p ON p.id = c.prospect_id
        LEFT JOIN companies co ON co.id = p.company_id""")
    counts = {"inbound_messages": 0, "inbox_drafts": 0}
    # The main seed's replies name no conversation; file them under it.
    await _sql(sm, "UPDATE outbox SET conversation_id = (SELECT c.id FROM conversations c "
                   "WHERE c.prospect_id = outbox.prospect_id ORDER BY c.created_at DESC LIMIT 1) "
                   "WHERE kind = 'reply' AND COALESCE(conversation_id, '') = ''")
    for cid, pid, thread_json, intent, created, first, email, company_id, company in convos:
        plan = SCRIPT.get(first)
        sent = await _sql(sm, "SELECT id, step, subject, mailbox, sent_at FROM outbox "
                              "WHERE prospect_id = ? AND kind = 'sequence' ORDER BY step", (pid,))
        if plan is None:
            # Not scripted: store what the thread already says, on the
            # mailbox the opener went out from.
            opener = next((s for s in sent if s[4]), None)
            mailbox = (opener[3] if opener else "") or MB_WARM
            subject = "Re: " + (opener[2] if opener else f"{company} on page two")
            answers = await _message_id(sm, opener[0]) if opener else ""
            for n, m in enumerate(json.loads(thread_json or "[]")):
                at = datetime.fromisoformat(m["timestamp"]).replace(tzinfo=None)
                if m["sender"] == "prospect":
                    await _store(sm, cid=cid, pid=pid, email=email, mailbox=mailbox,
                                 subject=subject, body=m["content"], at=at, intent=intent)
                    counts["inbound_messages"] += 1
                else:
                    await _sent_reply(sm, pid, cid, email, mailbox, subject, m["content"], at)
                await sm.set_conversations_read([cid], True, now=_iso(at + timedelta(minutes=20)))
            continue
        await _script(sm, plan, now, cid, pid, email, company_id, company, sent, counts)

    # A reply Mercury could not process: kept, flagged on Today.
    derek = await _sql(sm, "SELECT c.id, p.id, p.email FROM conversations c JOIN prospects p "
                           "ON p.id = c.prospect_id WHERE p.first_name = 'Derek' LIMIT 1")
    if derek:
        cid, pid, email = derek[0]
        await _store(sm, cid=cid, pid=pid, email=email, mailbox=MB_WARM,
                     subject="Re: Summit Peak Roofing on page two",
                     body="Quick one: could you also look at our second location in Golden?",
                     at=now - timedelta(hours=7), intent="", status="failed",
                     error="the reply classifier timed out", attempts=5)
        counts["inbound_messages"] += 1
    return counts


async def _message_id(sm, outbox_id: str) -> str:
    """Give a sent email a Message-ID (the seed's rows have none)."""
    rows = await _sql(sm, "SELECT message_id FROM outbox WHERE id = ?", (outbox_id,))
    if rows and rows[0][0]:
        return rows[0][0]
    mid = f"<{outbox_id[:12]}@northwind.example>"
    await _sql(sm, "UPDATE outbox SET message_id = ? WHERE id = ?", (mid, outbox_id))
    return mid


async def _sent_reply(sm, pid, cid, email, mailbox, subject, body, at):
    item = await sm.add_outbox_item(
        prospect_id=pid, conversation_id=cid, kind="reply", to_email=email, subject=subject,
        body=body, send_at=_iso(at), status="approved", provider="smtp", mailbox=mailbox)
    await sm.update_outbox_item(item, status="sent", sent_at=_iso(at))
    await _message_id(sm, item)
    return item


async def _script(sm, plan, now, cid, pid, email, company_id, company, sent, counts):
    at = now - plan["at"]
    mailbox = plan["mailbox"]
    # Their sequence: step 1 (and step 2) sent before they wrote, from the
    # mailbox the thread runs through (Holly wrote to a mailbox directly).
    opener_subject = sent[0][2] if sent else f"{company} on page two"
    answers = ""
    for outbox_id, step, _subject, _mb, _sent_at in sent:
        if step > plan["steps"] or plan.get("direct"):
            continue
        when = at - timedelta(days=6 if step == 1 else 3, hours=-1)
        await sm.update_outbox_item(outbox_id, status="sent", sent_at=_iso(when),
                                    mailbox=mailbox)
        await _sql(sm, "UPDATE outbox SET error = '', offer_key = ? WHERE id = ?",
                   (OFFER, outbox_id))
        answers = await _message_id(sm, outbox_id)
    subject = "Re: " + opener_subject
    thread = []
    for sender, text, hours in plan.get("earlier", []):
        when = at - timedelta(hours=hours)
        if sender == "prospect":
            await _store(sm, cid=cid, pid=pid, email=email, mailbox=mailbox, subject=subject,
                         body=text, at=when, intent=plan["intent"], answers=answers)
            counts["inbound_messages"] += 1
        else:
            reply = await _sent_reply(sm, pid, cid, email, mailbox, subject, text, when)
            answers = await _message_id(sm, reply)
        thread.append(Message(sender=sender, content=text, timestamp=when))
    last = await _store(sm, cid=cid, pid=pid, email=email, mailbox=mailbox, subject=subject,
                        body=plan["text"], at=at, intent=plan["intent"], answers=answers)
    counts["inbound_messages"] += 1
    thread.append(Message(sender="prospect", content=plan["text"], timestamp=at))
    await sm.update_conversation(
        cid, intent=plan["intent"], stage=plan["stage"], status=plan.get("status", "open"),
        thread_json=json.dumps([m.model_dump(mode="json") for m in thread]))
    changed = at + timedelta(minutes=1)
    await _sql(sm, "UPDATE conversations SET created_at = ?, updated_at = ? WHERE id = ?",
               (_iso(at - timedelta(hours=1)), _iso(changed), cid))
    if plan["stage"] in ("closing",):
        await sm.update_prospect_status(pid, "meeting")
    elif plan["stage"] not in ("closed_won", "closed_lost"):
        await sm.update_prospect_status(pid, "replied")

    if plan.get("draft") or plan.get("approved"):
        await _sql(sm, "DELETE FROM outbox WHERE prospect_id = ? AND kind = 'reply' "
                       "AND status IN ('pending_review', 'approved')", (pid,))
    if not plan["unread"]:
        await sm.set_conversations_read([cid], True, now=_iso(at + timedelta(minutes=20)))
    if plan.get("draft"):
        await sm.add_outbox_item(
            prospect_id=pid, conversation_id=cid, kind="reply", to_email=email,
            subject=subject, body=plan["draft"], send_at=_iso(now), status="pending_review",
            provider="smtp", mailbox=mailbox, answers_inbound_id=last)
        counts["inbox_drafts"] += 1
    if plan.get("approved"):
        send_at = _local_today_at(now, 16)
        if send_at <= now:
            send_at += timedelta(days=1)
        await sm.add_outbox_item(
            prospect_id=pid, conversation_id=cid, kind="reply", to_email=email,
            subject=subject, body=plan["approved"], send_at=_iso(send_at), status="approved",
            provider="smtp", mailbox=mailbox, approved_by="demo", answers_inbound_id=last)
        counts["inbox_drafts"] += 1
    if plan.get("reminder"):
        due = _local_today_at(now, 9)
        if due > now:
            due = now - timedelta(minutes=30)
        await sm.add_reminder(cid, _iso(due), prospect_id=pid, note=plan["reminder"],
                              created_by="dashboard")
    if plan.get("snooze"):
        # Next Monday 9:00, the inbox's own snooze preset.
        local = now.replace(tzinfo=timezone.utc).astimezone()
        until = _local_today_at(now, 9) + timedelta(days=(7 - local.weekday()) % 7 or 7)
        await sm.snooze_conversation(cid, _iso(until), now=_iso(at + timedelta(hours=1)))
    if plan.get("note"):
        note = await sm.add_contact_note(pid, plan["note"], created_by="dashboard")
        await _sql(sm, "UPDATE contact_notes SET created_at = ?, updated_at = ? WHERE id = ?",
                   (_iso(at + timedelta(minutes=30)), _iso(at + timedelta(minutes=30)),
                    note["id"]))
    if plan.get("opted_out"):
        await sm.update_prospect_status(pid, "opted_out")
        rule, _ = await sm.add_suppression("email", email, source="opt_out",
                                           reason="asked to be taken off the list",
                                           prospect_id=pid, actor="handler")
        await _sql(sm, "UPDATE suppressions SET created_at = ? WHERE id = ?",
                   (_iso(at + timedelta(minutes=1)), rule["id"]))
    for code, value in plan.get("signals", []):
        if company_id:
            await sm.add_observation(code, company_id=company_id, collector="demo",
                                     value_num=value, observed_at=_iso(now - timedelta(days=9)))
