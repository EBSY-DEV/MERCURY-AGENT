"""The unified inbox API (issue #7): list, search and facets in the database,
thread assembly, local triage state, bulk actions and composing through the
outbox. Real SQLite databases, fake mail providers, synthetic people."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import mercury.dashboard as dash
from mercury.config import ProductConfig
from mercury.control.context import OperatorContext
from mercury.control.inbox import InboxService
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.models.conversation import Conversation, Message
from mercury.models.prospect import Prospect
from mercury.state import StateManager
from tests.test_outbox_native import FakeProvider, StubBrain, make_sender


def run(coro):
    return asyncio.run(coro)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(when):
    return when.replace(microsecond=0).isoformat()


def config(require_approval=True):
    return SimpleNamespace(
        persona=SimpleNamespace(name="Sam Rivera", email="sam@example.com",
                                company="Example Co", role="BD", tone="direct"),
        channels=SimpleNamespace(
            email=SimpleNamespace(enabled=True, provider="smtp", max_daily_sends=50,
                                  send_to_risky=False, require_approval=require_approval,
                                  max_bounce_rate=0.05, mailboxes=[], thread_followups=True,
                                  auto_approve_followups=False, spread_sends=False),
            linkedin=SimpleNamespace(enabled=False)),
        product=ProductConfig(name="offer_a", description="d", pricing="$",
                              key_benefits=["b"], objection_responses={}),
        compliance=SimpleNamespace(postal_address="1 Example St",
                                   opt_out_line_en="Reply unsubscribe.",
                                   opt_out_line_es="Responde baja."),
        usage=SimpleNamespace(heartbeat_interval_minutes=15,
                              quiet_hours=SimpleNamespace(start="22:00", end="07:00",
                                                          timezone="UTC")),
    )


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mercury.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "_demo_config", lambda: config())

        def no_mail():
            raise RuntimeError("no mail config in tests")

        monkeypatch.setattr(dash, "_mail_context", no_mail)
        sm = StateManager(db_path=str(db))
        run(sm.init_db())
        with TestClient(dash.app) as c:
            c.sm = sm
            yield c


async def converse(sm, email, *, mailbox="a@example.com", intent="interested", stage="engaged",
                   body="Sounds good, tell me more.", subject="Re: hello", first="Jane",
                   last="Doe", company="Acme Example", status="open", sent_body=None):
    """A contact who got our opener from ``mailbox`` and answered it, stored
    the way the Handler stores it."""
    pid = await sm.add_prospect(Prospect(first_name=first, last_name=last, title="Owner",
                                         company=company, email=email,
                                         email_status="verified", status="replied"))
    opener = await sm.add_outbox_item(
        prospect_id=pid, to_email=email, subject="hello",
        body=sent_body or f"Opening line for {company}.", send_at=iso(now()),
        status="approved", campaign_id=f"c-{email}", step=1)
    await sm.update_outbox_item(opener, status="sent", sent_at=iso(now() - timedelta(days=1)),
                                message_id=f"<out-{email}>", mailbox=mailbox)
    row, _ = await sm.record_inbound(
        provider="fake", mailbox=mailbox, external_id=f"in-{email}",
        rfc_message_id=f"<in-{email}>", in_reply_to=f"<out-{email}>", from_email=email,
        subject=subject, body=body,
        date_header=format_datetime((now() - timedelta(hours=12)).replace(tzinfo=timezone.utc)))
    convo, _ = await sm.attach_inbound_to_conversation(
        row["id"], prospect_id=pid, campaign_id=f"c-{email}",
        message=Message(sender="prospect", content=body), intent=intent)
    await sm.update_conversation(convo.id, stage=stage, status=status)
    await sm.finish_inbound(row["id"], "processed")
    return pid, convo.id, row["id"]


def listing(client, **params):
    response = client.get("/api/inbox/conversations", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def facet(data, name):
    return {f["value"]: f["count"] for f in data["facets"][name]}


# ── List, filters, search, facets ──


def test_pages_go_past_the_old_hundred_conversation_limit(client):
    async def seed():
        for n in range(130):
            await converse(client.sm, f"lead{n:03d}@example.org",
                           mailbox="a@example.com" if n % 2 else "b@example.net")
    run(seed())
    seen, offset = [], 0
    while offset is not None:
        page = listing(client, limit=50, offset=offset)
        assert page["total"] == 130
        seen += [item["id"] for item in page["items"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 130
    assert facet(listing(client, limit=1), "mailbox") == {"a@example.com": 65,
                                                          "b@example.net": 65}
    # The old endpoint keeps working, capped as before.
    assert len(client.get("/api/conversations").json()) == 100


def test_filters_and_their_facets(client):
    sm = client.sm

    async def seed():
        a = await converse(sm, "ann@example.org", intent="interested", stage="engaged")
        b = await converse(sm, "bo@example.org", mailbox="b@example.net", intent="question",
                           stage="qualifying")
        c = await converse(sm, "cy@example.org", intent="escalate", status="needs_human")
        await sm.add_outbox_item(prospect_id=b[0], conversation_id=b[1], kind="reply",
                                 to_email="bo@example.org", subject="Re: hello", body="Answer.",
                                 send_at=iso(now()), mailbox="b@example.net")
        await sm.set_conversations_read([a[1]], True)
        return a, b, c
    a, b, c = run(seed())

    assert [i["id"] for i in listing(client, mailbox="b@example.net")["items"]] == [b[1]]
    assert [i["id"] for i in listing(client, intent="interested,question")["items"]] \
        and listing(client, intent="interested,question")["total"] == 2
    assert [i["id"] for i in listing(client, stage="qualifying")["items"]] == [b[1]]
    assert {i["id"] for i in listing(client, read="unread")["items"]} == {b[1], c[1]}
    assert [i["id"] for i in listing(client, attention="true")["items"]] == [c[1]]
    drafts = listing(client, response="draft")
    assert [i["id"] for i in drafts["items"]] == [b[1]]
    assert drafts["items"][0]["draft"]["status"] == "pending_review"
    assert {i["id"] for i in listing(client, response="awaiting")["items"]} == {a[1], c[1]}

    data = listing(client, mailbox="a@example.com")
    assert data["total"] == 2
    # A facet ignores its own filter, so the other mailbox still shows.
    assert facet(data, "mailbox") == {"a@example.com": 2, "b@example.net": 1}
    assert facet(data, "read") == {"unread": 1, "read": 1}
    assert facet(data, "attention") == {"needs_human": 1, "none": 1}
    assert client.get("/api/inbox/conversations", params={"read": "maybe"}).status_code == 400


def test_search_covers_contact_company_subject_and_content(client):
    sm = client.sm

    async def seed():
        await converse(sm, "jane@example.org", first="Jane", last="Quill", company="Bluebird Example",
                       subject="Re: pricing question", body="What does the starter plan include?",
                       sent_body="We help clinics answer calls.")
        await converse(sm, "omar@example.net", first="Omar", last="Reyes", company="Other Example")
        legacy = await sm.add_prospect(Prospect(first_name="Lia", last_name="Park", email="lia@example.com",
                                                company="Legacy Example", status="replied"))
        await sm.add_conversation(Conversation(
            id="", prospect_id=legacy, thread=[Message(sender="prospect", content="Old note about turnips")],
            intent="question"))
    run(seed())

    def hits(q):
        return [i["prospect"]["email"] for i in listing(client, q=q)["items"]]

    assert hits("jane@example") == ["jane@example.org"]
    assert hits("Jane Quill") == ["jane@example.org"]
    assert hits("bluebird") == ["jane@example.org"]
    assert hits("pricing question") == ["jane@example.org"]
    assert hits("starter plan") == ["jane@example.org"]
    assert hits("answer calls") == ["jane@example.org"]      # what we sent them
    assert hits("turnips") == ["lia@example.com"]            # legacy thread text
    assert hits("sender") == []                               # not the JSON around it
    assert hits("100%") == []                                 # LIKE wildcards are literal


# ── Threads ──


def test_thread_merges_sent_and_received_once_in_order_with_drafts_apart(client):
    sm = client.sm

    async def seed():
        pid, cid, first = await converse(sm, "jane@example.org", mailbox="a@example.com")
        # The same reply also landed in the second inbox.
        dup, _ = await sm.record_inbound(
            provider="fake", mailbox="b@example.net", external_id="other-1",
            rfc_message_id="<in-jane@example.org>", from_email="jane@example.org",
            subject="Re: hello", body="Sounds good, tell me more.")
        # Our answer went out, then she wrote again.
        sent = await sm.add_outbox_item(
            prospect_id=pid, conversation_id=cid, kind="reply", to_email="jane@example.org",
            subject="Re: hello", body="Great. Does Tuesday work?", send_at=iso(now()),
            status="approved", mailbox="a@example.com", answers_inbound_id=first)
        await sm.update_outbox_item(sent, status="sent", sent_at=iso(now() - timedelta(hours=2)),
                                    message_id="<ans-1@example.com>")
        convo = await sm.get_conversation(cid)
        convo.thread.append(Message(sender="mercury", content="Great. Does Tuesday work?"))
        await sm.update_conversation(cid, thread_json=convo.thread_json())
        second, _ = await sm.record_inbound(
            provider="fake", mailbox="a@example.com", external_id="in-2",
            rfc_message_id="<in2@example.org>", in_reply_to="<ans-1@example.com>",
            from_email="jane@example.org", subject="Re: hello", body="Tuesday is fine.")
        await sm.attach_inbound_to_conversation(
            second["id"], prospect_id=pid, campaign_id="", intent="interested",
            message=Message(sender="prospect", content="Tuesday is fine."))
        await sm.finish_inbound(second["id"], "processed")
        draft = await sm.add_outbox_item(
            prospect_id=pid, conversation_id=cid, kind="reply", to_email="jane@example.org",
            subject="Re: hello", body="Booked.", send_at=iso(now()), mailbox="a@example.com")
        failed = await sm.add_outbox_item(
            prospect_id=pid, conversation_id=cid, kind="reply", to_email="jane@example.org",
            subject="Re: hello", body="Rejected text.", send_at=iso(now()), mailbox="a@example.com")
        await sm.reject_outbox_item(failed)
        return cid, first, dup, sent, second, draft, failed
    cid, first, dup, sent, second, draft, failed = run(seed())

    data = client.get(f"/api/inbox/conversations/{cid}").json()
    shape = [(m["direction"], m["source"], m["delivery"], m["body"]) for m in data["messages"]]
    assert shape == [
        ("outbound", "outbox", "sent", "Opening line for Acme Example."),
        ("inbound", "inbound", "received", "Sounds good, tell me more."),
        ("outbound", "outbox", "sent", "Great. Does Tuesday work?"),
        ("inbound", "inbound", "received", "Tuesday is fine."),
    ]
    opener, reply1, answer, reply2 = data["messages"]
    assert opener["mailbox"] == "a@example.com" and opener["kind"] == "sequence"
    assert reply1["also_received_in"] == ["b@example.net"]
    assert reply1["answers_outbox_id"] and reply2["answers_outbox_id"] == sent
    assert answer["answers_inbound_id"] == first and reply1["intent"] == "interested"
    assert data["partial_history"] is False
    assert [d["id"] for d in data["drafts"]] == [draft]
    assert data["drafts"][0]["revision"] == 1 and data["drafts"][0]["approved"] is False
    assert [u["id"] for u in data["unsent"]] == [failed]
    compose = data["compose"]
    assert compose["allowed"] and compose["reply_to"]["id"] == second["id"]
    assert compose["mailbox"] == "a@example.com" and compose["to_email"] == "jane@example.org"


def test_legacy_conversations_are_partial_history_without_invented_sends(client):
    sm = client.sm

    async def seed():
        pid = await sm.add_prospect(Prospect(first_name="Lia", last_name="Park",
                                             email="lia@example.com", status="replied"))
        cid = await sm.add_conversation(Conversation(
            id="", prospect_id=pid, intent="question", thread=[
                Message(sender="prospect", content="How does it work?"),
                Message(sender="harvey", content="Short answer: it reads your inbox."),
            ]))
        return cid
    cid = run(seed())
    data = client.get(f"/api/inbox/conversations/{cid}").json()
    assert data["partial_history"] is True
    assert [(m["direction"], m["source"], m["delivery"], m["mailbox"])
            for m in data["messages"]] == [("inbound", "legacy", "recorded", ""),
                                           ("outbound", "legacy", "recorded", "")]
    assert all(m["time_source"] == "recorded" for m in data["messages"])
    item = listing(client)["items"][0]
    assert item["partial_history"] is True and item["response"] == "none"


# ── Compose ──


def test_a_draft_is_reviewed_approved_and_sent_from_the_original_mailbox(client):
    sm = client.sm
    pid, cid, inbound = run(converse(sm, "jane@example.org", mailbox="b@example.net"))
    run(sm.update_prospect_status(pid, "replied"))

    created = client.post(f"/api/inbox/conversations/{cid}/drafts",
                          json={"body": "Happy to. Does Tuesday at 10 work?"}).json()
    assert created["success"] and created["status"] == "pending_review"
    assert created["mailbox"] == "b@example.net" and created["to_email"] == "jane@example.org"
    assert created["in_reply_to"] == "<in-jane@example.org>"
    assert created["thread_references"] == "<in-jane@example.org>"
    assert created["answers_inbound_id"] == inbound and created["subject"] == "Re: hello"
    assert created["revision"] == 1

    # Creating a draft never sends, whatever the approval policy.
    a, b = FakeProvider(), FakeProvider()
    sender = make_sender(sm, a)
    sender.mailboxes = MailboxPool([Mailbox(email="a@example.com", provider=a, daily_cap=20),
                                    Mailbox(email="b@example.net", provider=b, daily_cap=20)])
    run(sender._drain_due())
    assert a.sent == b.sent == []

    second = client.post(f"/api/inbox/conversations/{cid}/drafts", json={"body": "Another"})
    assert second.status_code == 409 and second.json()["code"] == "draft_exists"

    approved = client.post(f"/api/inbox/conversations/{cid}/drafts/{created['id']}/approve",
                           json={"revision": 1}).json()
    assert approved["success"] and approved["status"] == "approved" and approved["approved"]
    run(sender._drain_due())
    assert a.sent == []
    (out,) = b.sent
    assert out["to"] == "jane@example.org" and out["subject"] == "Re: hello"
    assert out["in_reply_to"] == "<in-jane@example.org>"

    data = client.get(f"/api/inbox/conversations/{cid}").json()
    assert data["drafts"] == []
    last = data["messages"][-1]
    assert (last["source"], last["delivery"], last["mailbox"]) == ("outbox", "sent",
                                                                   "b@example.net")
    # The sender's own thread_json entry is the same message, shown once.
    assert sum(m["body"] == "Happy to. Does Tuesday at 10 work?" for m in data["messages"]) == 1

    edit_sent = client.put(f"/api/inbox/conversations/{cid}/drafts/{created['id']}",
                           json={"subject": "Re: hello", "body": "Overwrite", "revision": 2})
    assert edit_sent.status_code == 409 and edit_sent.json()["code"] == "not_editable"


def test_stale_saves_are_rejected_and_edits_send_an_approved_reply_back(client, monkeypatch):
    sm = client.sm
    _pid, cid, _ = run(converse(sm, "jane@example.org"))
    draft = client.post(f"/api/inbox/conversations/{cid}/drafts", json={"body": "First"}).json()
    url = f"/api/inbox/conversations/{cid}/drafts/{draft['id']}"

    saved = client.put(url, json={"subject": "Re: hello", "body": "Second", "revision": 1}).json()
    assert saved["revision"] == 2
    stale = client.put(url, json={"subject": "Re: hello", "body": "Old tab", "revision": 1})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision" and stale.json()["revision"] == 2
    assert run(sm.get_outbox_item(draft["id"]))["body"] == "Second"
    missing = client.put(url, json={"subject": "Re: hello", "body": "No revision"})
    assert missing.status_code == 400 and missing.json()["code"] == "revision_required"

    client.post(f"{url}/approve", json={"revision": 2})
    edited = client.put(url, json={"subject": "Re: hello", "body": "Third", "revision": 2}).json()
    assert edited["status"] == "pending_review" and edited["approval_cleared"] is True
    assert edited["approved"] is False and edited["revision"] == 3

    from mercury.agents.handler import Handler

    async def fake_generate(self, intent, reply_text, prospect, convo, instruction="",
                            profile=None):
        self._response_generation_id = ""
        return f"Regenerated ({instruction})"

    monkeypatch.setattr(Handler, "_generate_response", fake_generate)
    client.post(f"{url}/approve", json={"revision": 3})
    regen = client.post(f"{url}/regenerate",
                        json={"instruction": "shorter", "revision": 3}).json()
    assert regen["success"] and regen["body"] == "Regenerated (shorter)"
    assert regen["status"] == "pending_review" and regen["approval_cleared"] is True

    later = iso(now() + timedelta(days=1))
    scheduled = client.post(f"{url}/schedule",
                            json={"send_at": later, "revision": regen["revision"]},
                            headers={"Idempotency-Key": "sched-1"}).json()
    assert scheduled["status"] == "approved" and scheduled["send_at"].startswith(later[:16])
    provider = FakeProvider()
    run(make_sender(sm, provider)._drain_due())
    assert provider.sent == []        # not before its time

    discarded = client.post(f"{url}/discard", json={"revision": scheduled["revision"]}).json()
    assert discarded["success"] and run(sm.get_outbox_item(draft["id"]))["status"] == "rejected"


def test_mercury_can_write_the_draft_with_an_instruction(client, monkeypatch):
    from mercury.agents.handler import Handler

    seen = {}

    async def fake_generate(self, intent, reply_text, prospect, convo, instruction="",
                            profile=None):
        seen.update(intent=intent, text=reply_text, instruction=instruction)
        self._response_generation_id = ""
        return "Generated answer."

    monkeypatch.setattr(Handler, "_generate_response", fake_generate)
    _pid, cid, _ = run(converse(client.sm, "jane@example.org", body="Is there a trial?",
                                intent="question"))
    draft = client.post(f"/api/inbox/conversations/{cid}/drafts",
                        json={"generate": True, "instruction": "mention offer_a"}).json()
    assert draft["body"] == "Generated answer." and draft["status"] == "pending_review"
    assert seen == {"intent": "question", "text": "Is there a trial?",
                    "instruction": "mention offer_a"}


def test_opted_out_and_escalated_conversations_stay_visible_but_refuse_compose(client):
    sm = client.sm
    provider = FakeProvider()

    async def seed():
        await converse(sm, "keen@example.org")
        for email in ("stop@example.org", "angry@example.org"):
            await sm.add_prospect(Prospect(first_name="Pat", last_name="Lane", email=email,
                                           email_status="verified", status="contacted"))
        provider.inbound = [
            InboundMessage(provider_id="s1", from_email="stop@example.org", subject="Re: hello",
                           body="Please unsubscribe me.", message_id="<s1@example.org>"),
            InboundMessage(provider_id="s2", from_email="angry@example.org", subject="Re: hello",
                           body="This is harassment, my lawyer will hear about it.",
                           message_id="<s2@example.org>"),
        ]
        from mercury.agents.handler import Handler
        handler = Handler(brain=StubBrain(), state=sm, config=config(),
                          env=SimpleNamespace(instantly_api_key=""))
        handler.provider = provider
        await handler._run_native()
    run(seed())

    data = listing(client)
    by_email = {i["prospect"]["email"]: i for i in data["items"]}
    assert set(by_email) == {"keen@example.org", "stop@example.org", "angry@example.org"}
    assert by_email["stop@example.org"]["opted_out"] is True
    assert by_email["angry@example.org"]["needs_human"] is True
    for email, reason in (("stop@example.org", "opted_out"), ("angry@example.org", "escalated")):
        cid = by_email[email]["id"]
        thread = client.get(f"/api/inbox/conversations/{cid}").json()
        assert thread["compose"]["allowed"] is False and thread["compose"]["code"] == reason
        refused = client.post(f"/api/inbox/conversations/{cid}/drafts", json={"body": "Hi"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "compose_refused" and refused.json()["reason"] == reason
    stop = client.get(f"/api/inbox/conversations/{by_email['stop@example.org']['id']}").json()
    assert stop["restrictions"]["exclusion"]["source"] == "opt_out"
    assert [m["intent"] for m in stop["messages"] if m["direction"] == "inbound"] == ["unsubscribe"]


# ── Local state ──


def test_read_notes_snooze_and_reminders_survive_a_restart(client):
    sm = client.sm
    pid, cid, _ = run(converse(sm, "jane@example.org"))
    assert client.post(f"/api/inbox/conversations/{cid}/read").json()["unread"] is False
    note = client.post(f"/api/inbox/contacts/{pid}/notes",
                       json={"body": "Prefers mornings."}).json()
    until = iso(now() + timedelta(days=2))
    client.post(f"/api/inbox/conversations/{cid}/snooze", json={"until": until})
    due = iso(now() - timedelta(minutes=5))
    reminder = client.post(f"/api/inbox/conversations/{cid}/reminders",
                           json={"due_at": due, "note": "Check back"}).json()

    # A new process: a fresh StateManager and service on the same file.
    fresh = InboxService(OperatorContext.local("cli"), StateManager(sm.db_path))
    run(fresh.ready())
    assert run(fresh.list())["total"] == 0                     # hidden while snoozed
    (item,) = run(fresh.list({"snoozed": "only"}))["items"]
    assert item["unread"] is False and item["snoozed_until"] == until
    assert item["reminder"] == "due"
    thread = run(fresh.thread(cid))
    assert [n["body"] for n in thread["notes"]] == ["Prefers mornings."]
    assert thread["reminders"][0]["id"] == reminder["id"]
    assert thread["local"]["snoozed"] is True and "do not" in thread["local"]["note"]

    edited = client.patch(f"/api/inbox/notes/{note['id']}", json={"body": "Prefers 9am."}).json()
    assert edited["body"] == "Prefers 9am."
    assert client.delete(f"/api/inbox/notes/{note['id']}").json()["deleted"] is True
    assert client.get(f"/api/inbox/contacts/{pid}/notes").json() == []


def test_due_reminders_show_on_today_and_never_queue_mail(client):
    sm = client.sm
    _pid, cid, _ = run(converse(sm, "jane@example.org"))
    before = len(run(sm.get_outbox()))
    client.post(f"/api/inbox/conversations/{cid}/reminders",
                json={"due_at": iso(now() + timedelta(days=3)), "note": "later"})
    assert "reminders" not in [i["key"] for i in client.get("/api/today").json()["items"]]
    client.post(f"/api/inbox/conversations/{cid}/reminders",
                json={"due_at": iso(now() - timedelta(hours=1)), "note": "now"})
    today = client.get("/api/today").json()
    (item,) = [i for i in today["items"] if i["key"] == "reminders"]
    assert item["conversation_ids"] == [cid] and today["stats"]["reminders_due"] == 1
    (due,) = client.get("/api/inbox/reminders", params={"due": "true"}).json()
    assert due["note"] == "now" and due["due"] is True
    assert [i["id"] for i in listing(client, reminder="due")["items"]] == [cid]
    assert len(run(sm.get_outbox())) == before
    done = client.post(f"/api/inbox/reminders/{due['id']}/done").json()
    assert done["done_at"]
    assert client.get("/api/inbox/reminders", params={"due": "true"}).json() == []


def test_a_new_message_makes_a_read_conversation_unread_and_ends_its_snooze(client):
    sm = client.sm
    pid, cid, _ = run(converse(sm, "jane@example.org"))
    client.post(f"/api/inbox/conversations/{cid}/read")
    client.post(f"/api/inbox/conversations/{cid}/snooze",
                json={"until": iso(now() + timedelta(days=1))})
    assert listing(client)["total"] == 0

    async def another():
        import time
        time.sleep(1.1)   # stored timestamps are to the second
        row, _ = await sm.record_inbound(provider="fake", mailbox="a@example.com",
                                         external_id="in-2", from_email="jane@example.org",
                                         subject="Re: hello", body="One more thing.")
        await sm.attach_inbound_to_conversation(
            row["id"], prospect_id=pid, campaign_id="", intent="question",
            message=Message(sender="prospect", content="One more thing."))
    run(another())
    (item,) = listing(client)["items"]
    assert item["unread"] is True and item["snoozed"] is False
    assert item["snippet"] == "One more thing."


# ── Bulk ──


def test_bulk_actions_report_scope_and_each_outcome(client):
    sm = client.sm

    async def seed():
        a = await converse(sm, "ann@example.org")
        b = await converse(sm, "bo@gmail.com")
        draft = await sm.add_outbox_item(
            prospect_id=a[0], conversation_id=a[1], kind="reply", to_email="ann@example.org",
            subject="Re: hello", body="Queued answer.", send_at=iso(now()))
        return a[1], b[1], draft
    a, b, draft = run(seed())

    read = client.post("/api/inbox/bulk", json={"action": "read",
                                                "conversation_ids": [a, "nope", b]}).json()
    assert read["scope"] == {"conversation_ids": [a, "nope", b], "count": 3}
    assert read["succeeded"] == 2 and read["failed"] == 1
    assert [(r["id"], r["ok"], r.get("code")) for r in read["results"]] == [
        (a, True, None), ("nope", False, "not_found"), (b, True, None)]
    assert listing(client, read="unread")["total"] == 0

    until = iso(now() + timedelta(days=1))
    snoozed = client.post("/api/inbox/bulk", json={"action": "snooze", "conversation_ids": [a],
                                                   "until": until}).json()
    assert snoozed["scope"]["until"] == until and snoozed["results"][0]["snoozed_until"] == until

    unconfirmed = client.post("/api/inbox/bulk", json={"action": "exclude",
                                                       "conversation_ids": [a, b],
                                                       "kind": "domain"})
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["code"] == "confirmation_required"
    assert run(sm.find_suppressions("ann@example.org")) == []

    excluded = client.post("/api/inbox/bulk", json={
        "action": "exclude", "conversation_ids": [a, b], "kind": "domain",
        "reason": "asked by phone", "confirm": True}).json()
    assert excluded["scope"]["kind"] == "domain"
    first, second = excluded["results"]
    assert first["ok"] and first["value"] == "example.org" and first["blocked_queued"] == 1
    assert second == {"id": b, "ok": False, "code": "shared_domain",
                      "message": "gmail.com is a shared mail provider; exclude the address instead"}
    assert run(sm.get_outbox_item(draft))["status"] == "blocked"

    bad = client.post("/api/inbox/bulk", json={"action": "delete", "conversation_ids": [a]})
    assert bad.status_code == 400


# ── The screen's views: Needs you, segment counts, stage, events ──


def test_needs_you_is_escalated_a_draft_to_review_or_a_due_reminder(client):
    sm = client.sm

    async def seed():
        calm = await converse(sm, "calm@example.org")
        draft = await converse(sm, "draft@example.org")
        await sm.add_outbox_item(prospect_id=draft[0], conversation_id=draft[1], kind="reply",
                                 to_email="draft@example.org", subject="Re: hello",
                                 body="Answer.", send_at=iso(now()))
        approved = await converse(sm, "approved@example.org")
        await sm.add_outbox_item(prospect_id=approved[0], conversation_id=approved[1],
                                 kind="reply", to_email="approved@example.org",
                                 subject="Re: hello", body="Answer.", send_at=iso(now()),
                                 status="approved")
        angry = await converse(sm, "angry@example.org", intent="escalate", status="needs_human")
        due = await converse(sm, "due@example.org")
        await sm.add_reminder(due[1], iso(now() - timedelta(minutes=5)))
        later = await converse(sm, "later@example.org")
        await sm.add_reminder(later[1], iso(now() + timedelta(days=2)))
        sleepy = await converse(sm, "sleepy@example.org", intent="escalate", status="needs_human")
        await sm.snooze_conversation(sleepy[1], iso(now() + timedelta(days=1)))
        await sm.set_conversations_read([calm[1], later[1]], True)
        return calm[1], draft[1], approved[1], angry[1], due[1], later[1], sleepy[1]
    calm, draft, approved, angry, due, later, sleepy = run(seed())

    data = listing(client, needs_you="true")
    assert {i["id"] for i in data["items"]} == {draft, angry, due}
    assert all(i["needs_you"] for i in data["items"])
    assert facet(data, "needs_you") == {"needs_you": 3, "none": 3}
    # Segment counts ignore the view picked, so each tab shows its own count;
    # snoozed conversations count only under Snoozed and All.
    expected = {"needs_you": 3, "unread": 4, "snoozed": 1, "all": 7}
    assert data["segments"] == expected
    assert listing(client, read="unread")["segments"] == expected
    assert listing(client, snoozed="only")["segments"] == expected
    # Other filters do narrow the counts.
    assert listing(client, q="angry")["segments"] == {"needs_you": 1, "unread": 1,
                                                      "snoozed": 0, "all": 1}
    assert client.get("/api/inbox/conversations",
                      params={"needs_you": "perhaps"}).status_code == 400


def test_a_stage_set_by_hand_is_audited_and_closes_or_reopens(client):
    sm = client.sm
    pid, cid, _ = run(converse(sm, "jane@example.org", stage="engaged"))
    thread = client.get(f"/api/inbox/conversations/{cid}").json()
    assert thread["conversation"]["stage_since"]["reason"] == "replied"
    assert thread["events"] == []

    moved = client.post(f"/api/inbox/conversations/{cid}/stage", json={"stage": "qualifying"})
    assert moved.status_code == 200, moved.text
    assert moved.json() == {"success": True, "id": cid, "stage": "qualifying", "status": "open",
                            "changed": True}
    lost = client.post(f"/api/inbox/conversations/{cid}/stage",
                       json={"stage": "closed_lost"}).json()
    assert lost["status"] == "closed"
    convo = run(sm.get_conversation(cid))
    assert (convo.stage, convo.status) == ("closed_lost", "closed")
    reopened = client.post(f"/api/inbox/conversations/{cid}/stage",
                           json={"stage": "negotiating"}).json()
    assert reopened["status"] == "open"

    thread = client.get(f"/api/inbox/conversations/{cid}").json()
    assert thread["conversation"]["stage"] == "negotiating"
    assert thread["conversation"]["stage_since"]["reason"] == "set"
    assert [(e["kind"], e["from"], e["to"]) for e in thread["events"]] == [
        ("stage", "engaged", "qualifying"), ("stage", "qualifying", "closed_lost"),
        ("stage", "closed_lost", "negotiating")]
    audit = [r for r in run(sm.get_audit("conversation", cid)) if r["action"] == "inbox.stage"]
    assert len(audit) == 3 and {r["outcome"] for r in audit} == {"ok"}

    bad = client.post(f"/api/inbox/conversations/{cid}/stage", json={"stage": "won"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid"
    assert client.post("/api/inbox/conversations/nope/stage",
                       json={"stage": "engaged"}).status_code == 404
    # The Pipeline board follows the stage.
    escalated = run(converse(sm, "angry@example.org", intent="escalate", status="needs_human"))
    kept = client.post(f"/api/inbox/conversations/{escalated[1]}/stage",
                       json={"stage": "qualifying"}).json()
    assert kept["status"] == "needs_human"


def test_an_exclusion_shows_as_an_event_in_the_thread(client):
    sm = client.sm
    _pid, cid, _ = run(converse(sm, "stop@example.org", intent="unsubscribe"))
    client.post("/api/inbox/bulk", json={"action": "exclude", "conversation_ids": [cid],
                                         "confirm": True})
    thread = client.get(f"/api/inbox/conversations/{cid}").json()
    (event,) = thread["events"]
    assert event["kind"] == "exclusion" and event["value"] == "stop@example.org"
    assert event["at"] and thread["restrictions"]["exclusion"]["created_at"] == event["at"]


def test_bulk_says_which_conversations_were_already_that_way(client):
    sm = client.sm
    a = run(converse(sm, "ann@example.org"))[1]
    b = run(converse(sm, "bo@example.org"))[1]
    until = iso(now() + timedelta(days=3))
    client.post(f"/api/inbox/conversations/{b}/snooze", json={"until": until})
    client.post(f"/api/inbox/conversations/{b}/read")

    snoozed = client.post("/api/inbox/bulk", json={"action": "snooze", "conversation_ids": [a, b],
                                                   "until": until}).json()
    assert [(r["id"], r["changed"]) for r in snoozed["results"]] == [(a, True), (b, False)]
    read = client.post("/api/inbox/bulk", json={"action": "read",
                                                "conversation_ids": [a, b]}).json()
    assert [r["changed"] for r in read["results"]] == [True, False]
    unsnoozed = client.post("/api/inbox/bulk", json={"action": "unsnooze",
                                                     "conversation_ids": [a, b]}).json()
    assert [r["changed"] for r in unsnoozed["results"]] == [True, True]
    again = client.post("/api/inbox/bulk", json={"action": "unsnooze",
                                                 "conversation_ids": [a]}).json()
    assert again["results"][0]["changed"] is False


def test_today_lists_each_due_reminder_with_its_contact(client):
    sm = client.sm
    _pid, cid, _ = run(converse(sm, "tina@example.org", first="Tina", last="Santos",
                                company="Arapahoe Example"))
    client.post(f"/api/inbox/conversations/{cid}/reminders",
                json={"due_at": iso(now() - timedelta(minutes=30)),
                      "note": "Confirm the call."})
    today = client.get("/api/today").json()
    (item,) = [i for i in today["items"] if i["key"] == "reminders"]
    assert item["tab"] == "inbox" and today["stats"]["inbox_needs_you"] == 1
    (reminder,) = item["reminders"]
    assert reminder["conversation_id"] == cid and reminder["name"] == "Tina Santos"
    assert reminder["company"] == "Arapahoe Example" and reminder["note"] == "Confirm the call."


def test_the_thread_names_the_offer_and_the_company_signals(client):
    sm = client.sm

    async def seed():
        from mercury.models.campaign import Campaign
        from mercury.models.company import Company
        from mercury.signals import seed_signal_catalog
        await seed_signal_catalog(sm)
        company_id = await sm.add_company(Company(name="Acme Example", domain="acme.example",
                                                  location="Denver, CO"))
        campaign_id = await sm.add_campaign(Campaign(id="", name="c", offer_key="offer_a"))
        pid, cid, _ = await converse(sm, "jane@acme.example")
        async with sm._connect() as db:
            await db.execute("UPDATE prospects SET company_id = ? WHERE id = ?",
                             (company_id, pid))
            await db.commit()
        await sm.update_conversation(cid, campaign_id=campaign_id)
        await sm.add_observation("SERP_RANK", company_id=company_id, value_num=14,
                                 observed_at="2026-01-01T00:00:00")
        await sm.add_observation("SERP_RANK", company_id=company_id, value_num=12,
                                 observed_at="2026-02-01T00:00:00")
        return cid
    cid = run(seed())
    company = client.get(f"/api/inbox/conversations/{cid}").json()["company"]
    assert company["location"] == "Denver, CO" and company["offer_key"] == "offer_a"
    assert company["signals"] == [{"code": "SERP_RANK", "value": 12}]
