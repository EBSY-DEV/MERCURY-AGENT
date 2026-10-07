import asyncio
import sqlite3

import pytest

from mercury.state import MIGRATIONS, StateManager, _split_sql


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
