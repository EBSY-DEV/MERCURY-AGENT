"""Databases created by either OOO branch keep their data after integration."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from mercury.control.pauses import PauseService
from mercury.state import MIGRATIONS, StateManager, _split_sql, outbox_hash


def apply(conn, scripts):
    conn.create_function("outbox_hash", 5, outbox_hash)
    for script in scripts:
        for statement in _split_sql(script):
            conn.execute(statement)


@pytest.mark.asyncio
async def test_original_pause_history_schema_keeps_messages_overrides_and_thread_fields(tmp_path):
    path = str(tmp_path / "history.db")
    conn = sqlite3.connect(path)
    apply(conn, MIGRATIONS[:15] + [MIGRATIONS[18]])
    conn.executescript("""
        CREATE TABLE sequence_pauses (
          id TEXT PRIMARY KEY, prospect_id TEXT NOT NULL,
          reason TEXT DEFAULT 'out_of_office', status TEXT DEFAULT 'active',
          review_state TEXT NOT NULL, review_reason TEXT DEFAULT '',
          message_key TEXT DEFAULT '', message_at TIMESTAMP, confidence REAL DEFAULT 0,
          return_text TEXT DEFAULT '', resume_at TIMESTAMP, timezone TEXT DEFAULT '',
          manual_override INTEGER DEFAULT 0, override_at TIMESTAMP, override_by TEXT DEFAULT '',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          ended_at TIMESTAMP, ended_by TEXT DEFAULT '', ended_reason TEXT DEFAULT ''
        );
        CREATE TABLE auto_replies (
          message_key TEXT PRIMARY KEY, prospect_id TEXT DEFAULT '', from_email TEXT DEFAULT '',
          mailbox TEXT DEFAULT '', kind TEXT NOT NULL, detected_by TEXT DEFAULT '',
          subject TEXT DEFAULT '', excerpt TEXT DEFAULT '', received_at TIMESTAMP,
          return_text TEXT DEFAULT '', parsed_resume_at TIMESTAMP, pause_id TEXT DEFAULT '',
          outcome TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO sequence_pauses (id, prospect_id, review_state, message_key, resume_at,
            manual_override, override_by, timezone)
          VALUES ('pause_a', 'contact_a', 'scheduled', 'message_a', '2026-10-20T11:00:00',
                  1, 'operator_a', 'America/New_York');
        INSERT INTO sequence_pauses (id, prospect_id, status, review_state, ended_at)
          VALUES ('pause_history', 'contact_a', 'resumed', 'scheduled', '2026-09-01T12:00:00');
        INSERT INTO auto_replies (message_key, prospect_id, kind, excerpt, pause_id, outcome)
          VALUES ('message_a', 'contact_a', 'out_of_office', 'Example vacation notice', 'pause_a', 'paused');
        INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, thread_subject, thread_references)
          VALUES ('email_a', 'contact_a', 'contact@example.com', 'Example', 'Example body',
                  'approved', 'Example opener', '<opener@example.com>');
        PRAGMA user_version = 17;
    """)
    conn.commit()
    conn.close()
    state = StateManager(path)
    await state.init_db()
    pause = await state.get_active_pause("contact_a")
    assert pause["id"] == "pause_a" and pause["manual_override"] == 1
    assert pause["state"] == "paused" and pause["trigger_message_id"] == "message_a"
    assert pause["resume_at"] == "2026-10-20T11:00:00" and pause["override_by"] == "operator_a"
    assert (await state.list_pauses(ended=True))[0]["id"] == "pause_history"
    assert (await state.auto_replies_for("contact_a"))[0]["excerpt"] == "Example vacation notice"
    item = await state.get_outbox_item("email_a")
    assert item["thread_references"] == "<opener@example.com>"
    assert item["thread_subject"] == "Example opener"
    assert item["status"] == "approved" and item["approved_revision"] == 1
    await state.init_db()
    assert (await state.get_active_pause("contact_a"))["id"] == "pause_a"


@pytest.mark.asyncio
async def test_original_integration_pause_preserves_its_override_and_end_history(tmp_path):
    path = str(tmp_path / "integration.db")
    conn = sqlite3.connect(path)
    apply(conn, MIGRATIONS[:17])
    conn.execute("""INSERT INTO sequence_pauses
        (id, prospect_id, state, trigger_message_id, trigger_at, resume_at, manual_override,
         override_at, return_text, confidence)
        VALUES ('pause_a', 'contact_a', 'paused', 'message_a', '2026-10-05T12:00:00',
                '2026-10-20T11:00:00', 1, '2026-10-06T12:00:00', 'October 20', .95)""")
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()
    state = StateManager(path)
    await state.init_db()
    pause = await state.get_active_pause("contact_a")
    assert pause["message_key"] == "message_a" and pause["manual_override"] == 1
    assert pause["override_at"] == "2026-10-06T12:00:00"
    assert pause["review_state"] == "scheduled" and pause["return_text"] == "October 20"
    await state.resume_pause("contact_a", now=datetime(2026, 10, 21, 12))
    await state.record_ooo_pause("contact_a", message_id="message_b", message_at=datetime(2026, 10, 22, 12),
                                 state="needs_review")
    assert len(await state.list_pauses(ended=True)) == 1
    assert (await state.get_active_pause("contact_a"))["id"] != "pause_a"


@pytest.mark.asyncio
async def test_operator_return_day_survives_weekend_and_business_day_buffer(tmp_path):
    state = StateManager(str(tmp_path / "manual.db"))
    await state.init_db()
    _, pause = await state.record_auto_reply(
        message_key="message_a", kind="out_of_office", prospect_id="contact_a",
        received_at="2026-10-05T12:00:00", parsed={"review_reason": "no_date"})
    config = SimpleNamespace(
        usage=SimpleNamespace(quiet_hours=SimpleNamespace(timezone="America/New_York", end="07:00")),
        channels=SimpleNamespace(email=SimpleNamespace(provider="smtp", ooo_resume_buffer_days=2)))
    service = PauseService(state, config, clock=lambda: datetime(2026, 10, 8, 12))
    result = await service.set_return_date(pause["id"], "2026-10-16")
    assert result["back_on"] == "2026-10-16"
    assert result["resume_at"] == "2026-10-20T11:00:00"
    await state.init_db()
    assert (await service.get(pause["id"]))["back_on"] == "2026-10-16"
