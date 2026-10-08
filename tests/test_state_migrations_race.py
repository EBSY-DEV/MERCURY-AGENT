import asyncio
import sqlite3

import aiosqlite
import pytest

from mercury.state import MIGRATIONS, StateManager, _split_sql, outbox_hash


@pytest.mark.asyncio
async def test_concurrent_init_db_on_fresh_db(tmp_path):
    """The dashboard fires parallel requests that each call init_db()."""
    db = str(tmp_path / "mercury.db")
    await asyncio.gather(*(StateManager(db).init_db() for _ in range(8)))

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prospects)")}
    assert "email_status" in cols
    # Running again is a no-op.
    await StateManager(db).init_db()


def test_split_sql_keeps_trigger_bodies_whole():
    script = """
    CREATE TABLE t (x TEXT);
    CREATE TRIGGER trg BEFORE INSERT ON t BEGIN
        SELECT RAISE(ABORT, 'no') WHERE NEW.x = 'bad';
    END;
    """
    parts = _split_sql(script)
    assert len(parts) == 2
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].endswith("END;")


def _apply(db: str, scripts: list[str]) -> None:
    conn = sqlite3.connect(db)
    conn.create_function("outbox_hash", 5, outbox_hash, deterministic=True)
    for script in scripts:
        for statement in _split_sql(script):
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {len(scripts)}")
    conn.commit()
    conn.close()


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_order_mailbox_v9_then_warmup_v10():
    # Production DBs are stamped 9 by origin/main's outbox.mailbox migration;
    # it must stay v9 and the warm-up overlay must come after it.
    assert len(MIGRATIONS) >= 10
    assert "ALTER TABLE outbox ADD COLUMN mailbox" in MIGRATIONS[8]
    assert "CREATE TABLE IF NOT EXISTS warmup_inboxes" in MIGRATIONS[9]
    assert "warmup_inboxes" not in MIGRATIONS[8]


@pytest.mark.asyncio
async def test_production_v9_db_upgrades_to_v10(tmp_path):
    db = str(tmp_path / "prod.db")
    _apply(db, MIGRATIONS[:9])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO outbox (id, prospect_id, to_email, subject, body, mailbox) "
                 "VALUES ('o1', 'p1', 'a@b.co', 's', 'b', 'me@x.co')")
    conn.commit()
    assert "mailbox" in _columns(conn, "outbox")
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'warmup_inboxes'").fetchone()
    conn.close()

    await StateManager(db).init_db()

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert {"email", "status", "tasks_json", "notes", "paused_at", "pause_reason",
            "resumed_at"} <= _columns(conn, "warmup_inboxes")
    assert conn.execute("SELECT mailbox FROM outbox WHERE id = 'o1'").fetchone()[0] == "me@x.co"
    conn.close()


@pytest.mark.asyncio
async def test_pre_merge_dev_db_gets_the_mailbox_column(tmp_path):
    # Before the merge this branch used v9 for the warm-up table, so a local
    # dev DB stamped 9 has warmup_inboxes but no outbox.mailbox.
    db = str(tmp_path / "dev.db")
    _apply(db, MIGRATIONS[:8] + [MIGRATIONS[9]])
    await StateManager(db).init_db()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert "mailbox" in _columns(conn, "outbox")
    conn.close()


def test_migration_order_contact_policy_v14_v15_then_pauses_v16_demos_v17():
    # main shipped v14/v15 (exclusions, manual review) first; the
    # integration/p0 work (out-of-office pauses, demo gate) comes after.
    assert len(MIGRATIONS) == 25
    assert "CREATE TABLE suppressions" in MIGRATIONS[13]
    assert "requires_manual_review" in MIGRATIONS[14]
    assert "CREATE TABLE IF NOT EXISTS sequence_pauses" in MIGRATIONS[15]
    assert "CREATE TABLE demos" in MIGRATIONS[16]
    assert "CREATE TABLE audit_log" in MIGRATIONS[17]
    assert "ALTER TABLE outbox ADD COLUMN thread_subject" in MIGRATIONS[18]
    assert "CREATE TABLE IF NOT EXISTS offer_routes" in MIGRATIONS[20]
    assert "CREATE TABLE pains" in MIGRATIONS[21]
    assert "CREATE TABLE IF NOT EXISTS registry_lookups" in MIGRATIONS[22]
    assert "ALTER TABLE outbox ADD COLUMN flags " in MIGRATIONS[23]
    assert "CREATE TABLE inbound_messages" in MIGRATIONS[24]


_FULL_TABLES = {"suppressions", "company_holds", "sequence_pauses", "demos",
                "command_requests", "audit_log", "offer_routes", "pains",
                "registry_lookups", "registry_name_reviews"}


def _assert_full_schema(db: str) -> None:
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert _FULL_TABLES <= tables
    assert {"company_id", "requires_manual_review", "offer_key", "revision",
            "approved_revision", "approved_hash", "thread_subject",
            "thread_references", "pain_code", "word_count", "word_limit", "flags",
            "flags_accepted_by"} <= _columns(conn, "outbox")
    assert "offer_key" in _columns(conn, "campaigns")
    assert "detail_json" in _columns(conn, "observations")
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23])
async def test_main_line_db_upgrades_at_every_version(tmp_path, stamped):
    db = str(tmp_path / "main.db")
    _apply(db, MIGRATIONS[:stamped])
    if stamped >= 20:
        # v20 is applied by normalizing the pause schema; a database stamped
        # past it already has auto_replies, which the inbox migration reads.
        async with aiosqlite.connect(db) as conn:
            await StateManager._normalize_ooo_schema(conn)
            await conn.commit()
    await StateManager(db).init_db()
    _assert_full_schema(db)


@pytest.mark.asyncio
async def test_threading_branch_v16_db_keeps_headers_and_gains_later_migrations(tmp_path):
    db = str(tmp_path / "threading.db")
    _apply(db, MIGRATIONS[:15] + [MIGRATIONS[18]])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO outbox (id, prospect_id, to_email, status, subject, body, "
                 "thread_subject, thread_references, in_reply_to) "
                 "VALUES ('o1', 'p1', 'person@example.com', 'approved', 'followup_a', 'body_a', "
                 "'opener_a', '<message_a@example.com>', '<message_a@example.com>')")
    conn.commit()
    conn.close()

    await StateManager(db).init_db()
    _assert_full_schema(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT thread_subject, thread_references, in_reply_to, approved_revision "
                        "FROM outbox WHERE id = 'o1'").fetchone() == (
                            "opener_a", "<message_a@example.com>", "<message_a@example.com>", 1)
    conn.close()
    await StateManager(db).init_db()
    _assert_full_schema(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("p0_scripts", [1, 2, 3])
async def test_integration_p0_db_gets_main_migrations_without_rerunning_its_own(
        tmp_path, p0_scripts):
    # Before main was merged in, integration/p0 stamped v14 = sequence pauses
    # and v15 = demo gate; the revisions branch used v16 = audit. Such a
    # DB must still get main's exclusions and
    # manual review, keep its pause and demo rows, and not re-run the
    # demo gate's ALTER TABLEs ("duplicate column").
    db = str(tmp_path / "p0.db")
    _apply(db, MIGRATIONS[:13] + MIGRATIONS[15:15 + p0_scripts])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sequence_pauses (id, prospect_id, state) "
                 "VALUES ('s1', 'p1', 'paused')")
    if p0_scripts >= 2:
        conn.execute("INSERT INTO demos (id, prospect_id, offer_key, status) "
                     "VALUES ('d1', 'p1', 'offer_a', 'ready')")
    if p0_scripts == 3:
        conn.execute("INSERT INTO outbox (id, prospect_id, to_email, status, revision, "
                     "approved_revision, approved_hash, approved_by) "
                     "VALUES ('o1', 'p1', 'person@example.com', 'approved', 4, 4, 'hash', 'cli:local')")
        conn.execute("INSERT INTO audit_log (client, operator, action, object_type, object_id, "
                     "revision_before, revision_after, outcome) "
                     "VALUES ('cli', 'local', 'outbox.edit', 'outbox', 'o1', '3', '4', 'ok')")
        conn.execute("INSERT INTO command_requests (client, operator, request_key, action, "
                     "fingerprint, state, result_json) "
                     "VALUES ('cli', 'local', 'request_a', 'outbox.edit', 'mark', 'done', '{}')")
    conn.commit()
    conn.close()

    await StateManager(db).init_db()
    _assert_full_schema(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT state FROM sequence_pauses WHERE id = 's1'").fetchone() == ("paused",)
    if p0_scripts >= 2:
        assert conn.execute("SELECT status FROM demos WHERE id = 'd1'").fetchone() == ("ready",)
    if p0_scripts == 3:
        assert conn.execute("SELECT revision, approved_revision, approved_hash, approved_by "
                            "FROM outbox WHERE id = 'o1'").fetchone() == (4, 4, "hash", "cli:local")
        assert conn.execute("SELECT revision_after FROM audit_log WHERE object_id = 'o1'"
                            ).fetchone() == ("4",)
        assert conn.execute("SELECT state FROM command_requests WHERE request_key = 'request_a'"
                            ).fetchone() == ("done",)
    conn.close()
    # And it stays put on the next start.
    await StateManager(db).init_db()
    _assert_full_schema(db)
