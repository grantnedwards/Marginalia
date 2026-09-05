"""The ONE Poller callback, both kinds, no token and no gateway.

There were two deliver() implementations before integration; this is the check
that the survivor still does each half -- thread first for an unlock, one send
with an explicit role mention for everything else.
"""

import pytest

from marginalia import bot as botmod
from marginalia.bot import RESTART_CAP, RESTART_GIVE_UP, RESTART_RESET, Marginalia
from marginalia.config import Config
from marginalia.db import Database

DUE = 1_800_000_000
ROLE, CHAN, THREAD = 55, 7, 999


class Channel:
    """Records what was asked of Discord instead of asking it."""

    def __init__(self) -> None:
        self.threads: list[dict] = []
        self.sends: list[tuple[str, object]] = []

    async def create_thread(self, **kw):
        self.threads.append(kw)
        return type("Thread", (), {"id": THREAD})

    async def send(self, content, allowed_mentions=None, **kw):
        self.sends.append((content, allowed_mentions))


@pytest.fixture
async def bench(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await db.migrate()
    await db.run("INSERT INTO books (id, title) VALUES (1,'Moby-Dick')")
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, role_id)"
                 " VALUES (1,1,?,1,'2026-09',?)", CHAN, ROLE)
    await db.run("INSERT INTO checkpoints (id, cohort_id, idx, label, start_ref, end_ref,"
                 " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                 " VALUES (1,1,1,'Ch 1-3',1,3,3,?,'UTC','2026-09-06 19:00')", DUE)
    await db.run("INSERT INTO reminders (id, checkpoint_id, kind, due_at, grace_secs)"
                 " VALUES (1,1,'unlock',?,-1),(2,1,'T-1h',?,2700)", DUE, DUE)
    bot = Marginalia(Config("token", 1, CHAN, str(tmp_path / "m.db")), db)
    channel, asked = Channel(), []
    bot.get_channel = lambda cid: (asked.append(cid), channel)[1]  # never a real API call
    yield bot, db, channel, asked
    await db.close()


async def test_one_callback_dispatches_on_kind(bench):
    bot, db, channel, asked = bench
    for rid in (1, 2):  # unlock, then T-1h
        await bot.deliver(await db.one("SELECT * FROM reminders WHERE id = ?", rid))

    # The unlock: a PUBLIC thread (the library defaults to private), created once,
    # with its ceiling pinned -- and announced in the cohort channel, not in itself.
    assert [t["type"].name for t in channel.threads] == ["public_thread"]
    assert channel.threads[0]["auto_archive_duration"] == 10080
    assert (await db.one("SELECT max_chapter FROM channel_policy WHERE channel_id = ?",
                         THREAD))[0] == 3
    assert asked == [CHAN, THREAD]  # T-1h follows the club INTO the unlocked thread

    for content, am in channel.sends:  # one send per kind, same rules on both
        assert content.startswith(f"<@&{ROLE}> ") and f"<t:{DUE}:F>" in content
        assert (am.everyone, am.users) == (False, False)
        assert [o.id for o in am.roles] == [ROLE]
    assert f"<#{THREAD}>" in channel.sends[0][0] and "is open" in channel.sends[0][0]
    assert "is due in an hour" in channel.sends[1][0]

    await bot.deliver(await db.one("SELECT * FROM reminders WHERE id = 1"))
    assert len(channel.threads) == 1  # idempotent: a redelivered unlock creates nothing


def test_restart_backoff_escalates_caps_resets_and_gives_up():
    """The DECISION, not asyncio's clock: nothing here sleeps."""
    plan = Marginalia._restart_plan
    delays = []
    fails = 0
    while True:  # one persistent fault, restarted back to back
        fails, d = plan(fails, 0.0)
        if d is None:
            break
        delays.append(d)
    # The actual numbers, not the constants restated: deploy/watchdog.sh's STALE=600
    # only sees a dead scheduler if the whole give-up window finishes well inside it.
    assert delays == [5, 10, 20, 40, 60] and sum(delays) * 4 < 600
    assert delays == sorted(delays) and delays[0] < delays[-1]  # escalates
    assert delays[-1] == RESTART_CAP and max(delays) == RESTART_CAP  # and caps
    assert fails == RESTART_GIVE_UP and len(delays) == RESTART_GIVE_UP - 1  # then gives up
    # A spell of clean running forgives yesterday's fault: back to attempt one.
    assert plan(RESTART_GIVE_UP - 1, RESTART_RESET) == (1, delays[0])


async def test_loop_died_waits_the_planned_delay_then_restarts(bench, monkeypatch):
    bot, *_ = bench
    slept, restarted = [], []

    async def no_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(botmod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(type(bot.poll_reminders), "restart", lambda self: restarted.append(1))
    for _ in range(RESTART_GIVE_UP):
        await bot._loop_died(RuntimeError("boom"))
    assert slept and all(s > 0 for s in slept)  # never a spin: every restart waits
    assert len(restarted) == len(slept) == RESTART_GIVE_UP - 1  # the last one does NOT restart
