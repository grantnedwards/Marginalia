"""Real temp FILE database throughout: WAL does not behave the same in :memory:."""

import asyncio
import sqlite3

import pytest

from marginalia.db import Database

SNOWFLAKE = 1545535072151670824  # > 2**53, so float coercion would corrupt it
CP = ("INSERT INTO checkpoints (cohort_id, idx, label, start_ref, end_ref, chapter_ceiling,"
      " due_at_utc, tz_id, local_wall) VALUES (?,1,'Ch 1-3',1,3,3,0,'UTC','2026-09-06 19:00')")


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "marginalia.db"))
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


async def seed(db: Database) -> int:
    """A book, a cohort keyed by real snowflakes, and one checkpoint."""
    await db.run("INSERT INTO books (id, title) VALUES (1, 'Moby-Dick')")
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month)"
                 " VALUES (1, ?, ?, 1, '2026-09')", SNOWFLAKE, SNOWFLAKE)
    return (await db.run(CP, 1)).lastrowid


async def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(CP, 999)  # no such cohort


async def test_cascade_deletes_reminders(db):
    cp = await seed(db)
    await db.run("INSERT INTO reminders (checkpoint_id, kind, due_at) VALUES (?,'unlock',0)", cp)
    await db.run("DELETE FROM checkpoints WHERE id = ?", cp)
    assert await db.all("SELECT 1 FROM reminders") == []


async def test_duplicate_reminder_kind_raises(db):
    cp = await seed(db)
    await db.run("INSERT INTO reminders (checkpoint_id, kind, due_at) VALUES (?,'T-1h',0)", cp)
    with pytest.raises(sqlite3.IntegrityError):
        await db.run("INSERT INTO reminders (checkpoint_id, kind, due_at) VALUES (?,'T-1h',9)", cp)


async def test_migrate_is_idempotent(db):
    before = (await db.one("PRAGMA user_version"))[0]
    assert await db.migrate() == before
    assert (await db.one("PRAGMA user_version"))[0] == before


async def test_snowflake_round_trips_exactly(db):
    await seed(db)
    assert (await db.one("SELECT guild_id FROM cohorts"))[0] == SNOWFLAKE


async def test_run_during_rolling_back_tx_survives(db):
    """One shared connection: a run() write must NOT join someone else's tx and vanish.

    /purge_book on an attached book and a second /cycle-open in the same month are
    DESIGNED rollbacks. Without mutual exclusion any Poller.tick write in that
    window -- the claim, the 'sent' mark, the heartbeat -- is silently discarded,
    so a reminder that was already delivered sends again.
    """
    inside = asyncio.Event()

    async def doomed_tx():
        with pytest.raises(ZeroDivisionError):
            async with db.tx() as conn:
                await conn.execute("INSERT INTO books (id, title) VALUES (1, 'Doomed')")
                inside.set()
                await asyncio.sleep(0.05)  # the window the other coroutine writes in
                raise ZeroDivisionError

    async def innocent_write():
        await inside.wait()
        await db.run("INSERT INTO books (id, title) VALUES (2, 'Innocent')")

    await asyncio.gather(doomed_tx(), innocent_write())
    assert await db.one("SELECT title FROM books WHERE id = 1") is None  # rollback held
    survivor = await db.one("SELECT title FROM books WHERE id = 2")
    assert survivor is not None and survivor[0] == "Innocent"


async def test_overlapping_tx_do_not_raise(db):
    """Two tx() at once used to be OperationalError: transaction within a transaction."""
    async def insert(book_id: int, delay: float):
        async with db.tx() as conn:
            await asyncio.sleep(delay)
            await conn.execute("INSERT INTO books (id, title) VALUES (?,?)",
                               (book_id, f"Book {book_id}"))

    await asyncio.gather(insert(1, 0.02), insert(2, 0))
    assert len(await db.all("SELECT 1 FROM books")) == 2


async def test_fts_external_content_and_triggers(db):
    await seed(db)
    await db.run("INSERT INTO chapters (id, book_id, chapter_index) VALUES (1, 1, 20)")
    await db.run("INSERT INTO paragraphs (book_id, chapter_id, chapter_index, para_index, text)"
                 " VALUES (1, 1, 20, 1, 'Call me Ishmael.')")
    hit = await db.one("SELECT p.text FROM para_fts f JOIN paragraphs p ON p.id = f.rowid"
                       " WHERE para_fts MATCH 'ishmael'")
    assert hit["text"] == "Call me Ishmael."
