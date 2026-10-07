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
