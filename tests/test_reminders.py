"""Fake clock, recording stub deliver: no Discord connection and no token anywhere."""

import asyncio
import types

import discord
import pytest

from marginalia.db import Database
from marginalia.reminders import Poller

DUE = 1_800_000_000
LATE = DUE + 3 * 86400  # a three-day outage


def http(cls: type, code: int) -> Exception:
    return cls(types.SimpleNamespace(status=code, reason=""), "x")


def recorder(got: list[int], exc: Exception | None = None):
    async def deliver(row):
        got.append(row["id"])
        if exc is not None:
            raise exc
    return deliver


OPEN: list[Database] = []


@pytest.fixture(autouse=True)
async def _close_connections():
    yield  # an aiosqlite worker thread outlives the event loop if left open
    while OPEN:
        await OPEN.pop().close()


async def fresh(path) -> Database:
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    OPEN.append(db)
    return db


async def bench(tmp_path, exc=None, grace=3600, status="pending", attempts=0, claimed_at=None):
    """One reminder row on the FK chain, plus a Poller and the ids it delivered."""
    db = await fresh(tmp_path / "m.db")
    await db.run("INSERT INTO books (id, title) VALUES (1,'Moby-Dick')")
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month)"
                 " VALUES (1,1,1,1,'2026-09')")
    await db.run("INSERT INTO checkpoints (id, cohort_id, idx, label, start_ref, end_ref,"
                 " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                 " VALUES (1,1,1,'Ch 1-3',1,3,3,?,'UTC','2026-09-06 19:00')", DUE)
    await db.run("INSERT INTO reminders (checkpoint_id, kind, due_at, grace_secs, status, attempts,"
                 " claimed_at) VALUES (1,'unlock',?,?,?,?,?)",
                 DUE, grace, status, attempts, claimed_at)
    got: list[int] = []
    return db, Poller(db, recorder(got, exc)), got


async def status(db) -> str:
    return (await db.one("SELECT status FROM reminders"))[0]


async def test_two_concurrent_ticks_deliver_exactly_once(tmp_path):
    db, one, got = await bench(tmp_path)
    two = Poller(await fresh(tmp_path / "m.db"), recorder(got))  # second process, same file
    await asyncio.gather(one.tick(DUE), two.tick(DUE))
    assert got == [1]
    assert tuple(await db.one("SELECT status, attempts FROM reminders")) == ("sent", 1)


async def test_survives_restart(tmp_path):
    db, _, _ = await bench(tmp_path)  # rows on disk, then the process dies
    got: list[int] = []
    await Poller(await fresh(tmp_path / "m.db"), recorder(got)).tick(DUE + 60)
    assert got == [1] and await status(db) == "sent"


@pytest.mark.parametrize(("grace", "now", "counts", "after"), [
    (3600, DUE + 7200, (0, 1), "skipped"),  # bounded window: too late to be true
    (-1, LATE, (1, 0), "sent"),             # negative == infinite: the unlock, 3 days late
])
async def test_grace(tmp_path, grace, now, counts, after):
    db, poller, got = await bench(tmp_path, grace=grace)
    assert await poller.tick(now) == counts
    assert await status(db) == after and got == ([1] if after == "sent" else [])


@pytest.mark.parametrize(("attempts", "after", "delivered"), [(1, "sent", [1]), (3, "failed", [])])
async def test_reap_recovers_a_crashed_send_until_attempts_run_out(tmp_path, attempts, after,
                                                                  delivered):
    db, poller, got = await bench(tmp_path, status="sending", attempts=attempts, claimed_at=DUE)
    assert await poller.reap(DUE + 601) == 1
    await poller.tick(DUE + 601)
    await poller.tick(DUE + 700)  # never twice, and never at all once failed
    assert got == delivered and await status(db) == after


@pytest.mark.parametrize(("exc", "after"), [
    (http(discord.Forbidden, 403), "failed"),       # terminal: no retry, ever
    (http(discord.NotFound, 404), "failed"),
    (http(discord.HTTPException, 500), "sending"),  # retryable: reap requeues it
])
async def test_error_classification(tmp_path, exc, after):
    db, poller, _ = await bench(tmp_path, exc=exc)
    assert await poller.tick(DUE) == (0, 0)
    assert await status(db) == after


async def test_backward_clock_jump_does_not_resend(tmp_path):
    _, poller, got = await bench(tmp_path)
    await poller.tick(DUE)
    await poller.tick(DUE - 3600)  # NTP correction
    assert got == [1]


async def test_heartbeat_advances_on_a_zero_work_tick(tmp_path):
    db = await fresh(tmp_path / "m.db")
    poller = Poller(db, recorder([]))
    assert await poller.tick(DUE) == (0, 0)
    await poller.tick(DUE + 30)
    assert tuple(await db.one("SELECT beat_at, tick_count FROM heartbeat")) == (DUE + 30, 2)
