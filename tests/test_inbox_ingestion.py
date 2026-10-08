"""Inbound mail is stored before it is handled (issue #7).

Real SQLite databases and fake mail providers: several mailboxes, provider
ids that collide across inboxes, the same message delivered twice, a failure
part way through handling and its retry, repeated polling, and messages that
were handled before inbound storage existed.
"""

import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mercury.agents.handler import Handler
from mercury.config import ProductConfig
from mercury.integrations.mail_provider import InboundMessage
from mercury.integrations.mailboxes import Mailbox, MailboxPool
from mercury.state import INBOUND_MAX_ATTEMPTS, MIGRATIONS, StateManager
from tests.test_outbox_native import FakeProvider, StubBrain, _now_iso, seed_prospect


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "inbox.db"))
        await sm.init_db()
        yield sm


def config(**email):
    settings = dict(enabled=True, provider="smtp", max_daily_sends=50, send_to_risky=False,
                    require_approval=True, max_bounce_rate=0.05, mailboxes=[],
                    warmup_initial_cap=5, warmup_weekly_increase=5,
                    auto_approve_followups=False, spread_sends=False)
    settings.update(email)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Sam Rivera", email="sam@example.com",
                                company="Example Co", role="BD", tone="direct"),
        channels=SimpleNamespace(email=SimpleNamespace(**settings),
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


def two_inboxes():
    a, b = FakeProvider(), FakeProvider()
    pool = MailboxPool([Mailbox(email="a@example.com", provider=a, daily_cap=20),
                        Mailbox(email="b@example.net", provider=b, daily_cap=20)])
    return pool, a, b


def handler_for(state, pool, intent="question", brain=None):
    handler = Handler(brain=brain or StubBrain(intent=intent), state=state, config=config(),
                      env=SimpleNamespace(instantly_api_key=""))
    handler.mailboxes = pool
    handler.provider = pool.primary.provider
    return handler


def reply(provider_id, sender, body="Tell me more please.", message_id="", **kw):
    return InboundMessage(provider_id=provider_id, from_email=sender, subject="Re: hello",
                          body=body, message_id=message_id, **kw)


async def sent(state, pid, email, message_id, mailbox):
    item = await state.add_outbox_item(prospect_id=pid, to_email=email, subject="hello",
                                       body="Opening line.", send_at=_now_iso(),
                                       status="approved", campaign_id="c1", step=1)
    await state.update_outbox_item(item, status="sent", sent_at=_now_iso(),
                                   message_id=message_id, mailbox=mailbox)
    return item


def rows(state, sql="SELECT * FROM inbound_messages ORDER BY rowid", params=()):
    conn = sqlite3.connect(state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


async def reply_drafts(state):
    return [r for r in await state.get_outbox(status="pending_review") if r["kind"] == "reply"]


@pytest.mark.asyncio
async def test_replies_from_two_mailboxes_are_stored_with_their_metadata(state):
    pool, a, b = two_inboxes()
    jane = await seed_prospect(state, email="jane@example.org", status="contacted")
    lee = await seed_prospect(state, email="lee@example.org", status="contacted")
    opener = await sent(state, jane, "jane@example.org", "<out1@example.com>", "a@example.com")
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>",
                       in_reply_to="<out1@example.com>", thread_ref="t-a",
                       references="<out1@example.com>", date="Mon, 5 Oct 2026 09:30:00 +0000")]
    b.inbound = [reply("g-2", "lee@example.org", message_id="<r2@example.org>")]

    await handler_for(state, pool)._run_native()

    stored = {r["mailbox"]: r for r in rows(state)}
    assert set(stored) == {"a@example.com", "b@example.net"}
    first = stored["a@example.com"]
    assert first["provider"] == "fake" and first["external_id"] == "g-1"
    assert first["rfc_message_id"] == "<r1@example.org>"
    assert first["received_at"] == "2026-10-05T09:30:00"
    assert first["outbox_id"] == opener and first["prospect_id"] == jane
    assert first["status"] == "processed" and first["intent"] == "question"
    assert first["conversation_id"]

    drafts = {d["to_email"]: d for d in await reply_drafts(state)}
    assert drafts["jane@example.org"]["mailbox"] == "a@example.com"
    assert drafts["jane@example.org"]["answers_inbound_id"] == first["id"]
    assert drafts["jane@example.org"]["in_reply_to"] == "<r1@example.org>"
    assert drafts["jane@example.org"]["thread_references"] == \
        "<out1@example.com> <r1@example.org>"
    assert drafts["lee@example.org"]["mailbox"] == "b@example.net"
    assert stored["b@example.net"]["prospect_id"] == lee


@pytest.mark.asyncio
async def test_the_same_provider_id_in_two_inboxes_is_two_messages(state):
    pool, a, b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    await seed_prospect(state, email="lee@example.org", status="contacted")
    # Both providers number their messages from 1; the ids collide.
    a.inbound = [reply("1", "jane@example.org", message_id="<x1@example.org>")]
    b.inbound = [reply("1", "lee@example.org", message_id="<y1@example.org>")]

    await handler_for(state, pool)._run_native()

    assert [(r["mailbox"], r["status"]) for r in rows(state)] == [
        ("a@example.com", "processed"), ("b@example.net", "processed")]
    assert sorted(d["to_email"] for d in await reply_drafts(state)) == [
        "jane@example.org", "lee@example.org"]


@pytest.mark.asyncio
async def test_one_message_delivered_to_two_inboxes_is_handled_once(state):
    pool, a, b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    a.inbound = [reply("a-7", "jane@example.org", message_id="<same@example.org>")]
    b.inbound = [reply("b-9", "jane@example.org", message_id="<same@example.org>")]

    await handler_for(state, pool)._run_native()

    first, second = rows(state)
    assert first["status"] == "processed"
    assert second["status"] == "duplicate" and second["duplicate_of"] == first["id"]
    assert len(await reply_drafts(state)) == 1
    (convo,) = await state.get_conversations_by_status("open")
    assert len(convo.thread) == 1


@pytest.mark.asyncio
async def test_polling_the_same_message_again_changes_nothing(state):
    pool, a, _b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>")]
    handler = handler_for(state, pool)

    await handler._run_native()
    await handler._run_native()
    await handler._run_native()

    (row,) = rows(state)
    assert row["status"] == "processed" and row["attempts"] == 1
    assert len(await reply_drafts(state)) == 1
    (convo,) = await state.get_conversations_by_status("open")
    assert len(convo.thread) == 1


class FlakyBrain(StubBrain):
    """Classifies fine; the reply writer fails the first time."""

    def __init__(self):
        super().__init__(intent="interested", reply="Glad to hear it. Does Tuesday work?")
        self.failures = 1

    async def think(self, prompt, session_id=None, agent="", task=""):
        if task == "generate_response" and self.failures:
            self.failures -= 1
            raise RuntimeError("model unavailable")
        return await super().think(prompt, session_id, agent, task)


@pytest.mark.asyncio
async def test_a_failure_mid_handling_is_retried_from_storage_once(state):
    pool, a, _b = two_inboxes()
    jane = await seed_prospect(state, email="jane@example.org", status="contacted")
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>")]
    handler = handler_for(state, pool, brain=FlakyBrain())

    await handler._run_native()
    (row,) = rows(state)
    assert row["status"] == "retry" and row["attempts"] == 1
    assert "model unavailable" in row["last_error"]
    assert await reply_drafts(state) == []

    # The provider no longer returns it (outside its window): it is still
    # retried from storage, and nothing the first try did is repeated.
    a.inbound = []
    await handler._run_native()
    (row,) = rows(state)
    assert row["status"] == "processed" and row["attempts"] == 2
    (draft,) = await reply_drafts(state)
    assert draft["answers_inbound_id"] == row["id"]
    (convo,) = await state.get_conversations_by_status("open")
    assert [m.content for m in convo.thread] == ["Tell me more please."]
    assert convo.stage == "engaged"   # advanced once, not twice
    logged = rows(state, "SELECT * FROM actions WHERE action_type = 'reply_received'")
    assert len(logged) == 1 and jane in logged[0]["details_json"]


@pytest.mark.asyncio
async def test_a_retry_keeps_the_answer_its_first_try_queued(state):
    pool, a, _b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>")]
    handler = handler_for(state, pool)
    real_log = state.log_action
    calls = {"n": 0}

    async def log_fails_after_queueing(action_type, agent, details=None):
        if action_type == "reply_queued" and not calls["n"]:
            calls["n"] += 1
            raise RuntimeError("disk hiccup")
        return await real_log(action_type, agent, details)

    state.log_action = log_fails_after_queueing
    await handler._run_native()
    assert rows(state)[0]["status"] == "retry"
    await handler._run_native()
    assert rows(state)[0]["status"] == "processed"
    (draft,) = await reply_drafts(state)
    assert draft["status"] == "pending_review"
    assert await state.get_outbox(status="cancelled") == []


@pytest.mark.asyncio
async def test_a_message_that_keeps_failing_is_kept_as_failed(state):
    pool, a, _b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>")]
    handler = handler_for(state, pool)

    async def broken(*_a, **_k):
        raise RuntimeError("classifier down")

    handler._classify_intent = broken
    for _ in range(INBOUND_MAX_ATTEMPTS + 2):
        await handler._run_native()
    (row,) = rows(state)
    assert row["status"] == "failed" and row["attempts"] == INBOUND_MAX_ATTEMPTS
    assert row["body"] == "Tell me more please."   # kept, never dropped


@pytest.mark.asyncio
async def test_messages_handled_before_storage_existed_are_not_handled_again(state):
    pool, a, _b = two_inboxes()
    await seed_prospect(state, email="jane@example.org", status="contacted")
    await state.mark_reply_processed("g-1")      # the old unscoped dedup key
    a.inbound = [reply("g-1", "jane@example.org", message_id="<r1@example.org>")]

    await handler_for(state, pool)._run_native()

    (row,) = rows(state)
    assert row["status"] == "skipped"
    assert await reply_drafts(state) == []
    assert await state.get_conversations_by_status("open") == []


@pytest.mark.asyncio
async def test_bounces_and_automatic_replies_are_stored_and_linked(state):
    pool, a, _b = two_inboxes()
    jane = await seed_prospect(state, email="jane@example.org", status="contacted")
    opener = await sent(state, jane, "jane@example.org", "<out1@example.com>", "a@example.com")
    a.inbound = [
        InboundMessage(provider_id="d-1", from_email="mailer-daemon@example.com",
                       subject="Delivery Status Notification (Failure)",
                       body="550 5.1.1 user unknown", in_reply_to="<out1@example.com>",
                       is_bounce=True),
        InboundMessage(provider_id="o-1", from_email="jane@example.org",
                       subject="Automatic reply: hello", body="I am away until Monday.",
                       message_id="<ooo@example.org>", headers={"auto-submitted": "auto-replied"}),
    ]
    await handler_for(state, pool)._run_native()

    bounce, auto = rows(state)
    assert bounce["kind"] == "bounce" and bounce["outbox_id"] == opener
    assert bounce["prospect_id"] == jane and bounce["status"] == "processed"
    assert auto["kind"] == "automatic" and auto["auto_kind"] == "out_of_office"
    assert auto["prospect_id"] == jane and auto["conversation_id"] == ""


@pytest.mark.asyncio
async def test_a_queued_reply_goes_out_with_the_whole_reference_chain(state):
    from tests.test_outbox_native import make_sender

    pid = await seed_prospect(state, email="jane@example.org", status="replied")
    provider = FakeProvider()
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@example.org", subject="Re: hello",
        body="Thanks, here is the answer.", send_at=_now_iso(), status="approved",
        kind="reply", in_reply_to="<r1@example.org>",
        thread_references="<out1@example.com> <r1@example.org>")
    await make_sender(state, provider)._drain_due()
    (out,) = provider.sent
    assert out["in_reply_to"] == "<r1@example.org>"
    assert out["references"] == "<out1@example.com> <r1@example.org>"


def test_automatic_replies_recorded_before_the_upgrade_are_backfilled():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "old.db")
        import asyncio
        asyncio.run(StateManager(db).init_db())
        conn = sqlite3.connect(db)
        # Roll the file back to the version before the inbox (v24), as an
        # existing install would be.
        for table in ("inbound_messages", "inbox_state", "contact_notes", "inbox_reminders"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP INDEX idx_outbox_conversation")
        conn.execute("DROP INDEX idx_outbox_message_id")
        conn.execute("ALTER TABLE outbox DROP COLUMN answers_inbound_id")
        conn.execute(
            "INSERT INTO auto_replies (message_key, prospect_id, from_email, mailbox, kind, "
            "subject, excerpt, received_at, created_at) VALUES "
            "('<ooo@example.org>', 'p1', 'jane@example.org', 'a@example.com', "
            "'out_of_office', 'Away', 'Back on Monday.', '2026-10-01T08:00:00', "
            "'2026-10-01T08:01:00')")
        conn.execute("PRAGMA user_version = 24")
        conn.commit()
        conn.close()

        asyncio.run(StateManager(db).init_db())
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        (row,) = [dict(r) for r in conn.execute("SELECT * FROM inbound_messages")]
        conn.close()
        assert row["source"] == "backfill" and row["kind"] == "automatic"
        assert row["rfc_message_id"] == "<ooo@example.org>"
        assert row["mailbox"] == "a@example.com" and row["prospect_id"] == "p1"
        assert row["body"] == "Back on Monday." and row["status"] == "processed"
