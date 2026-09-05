"""A cohort's reading plan on disk: checkpoints and their reminder fan-out, the wrap-up
meeting, the per-checkpoint thread each unlock opens, and each member's own progress.

Plain ``async def f(db, ...)``; ``cogs/reading.py`` only turns an Interaction into these
arguments, which is why the tests need no token. Calendar arithmetic is schedule.py's.

ANTI-FEATURES, a design boundary, do not "helpfully" add them: no leaderboards, no
streaks, no public ranking, no shaming by omission (no public completion list, no
per-member nagging). Pace, stats and DNF are self-only and unattributed.
"""

from __future__ import annotations

import asyncio
from datetime import date, time

import aiosqlite
import discord

from marginalia import schedule
from marginalia.cycle import LIVE
from marginalia.db import Database
from marginalia.timefmt import Wall, local_wall, resolve_wall, unix

ARCHIVE = 10080  # 7 days: a monthly club's thread must not vanish mid-week
ARCHIVE_OK = (60, 1440, 4320, 10080)  # the only values Discord accepts
PER_PAGE = 5
WEEK = 7 * 86_400
THROTTLE = 0.6  # seconds between bulk creates: ~1-2/sec
COL = {"chapter": "chapter_index", "page": "page"}
PLANNABLE = ("draft", *LIVE)
# Sentinel idx: keeps the meeting out of the weekly 1..n sequence, so apply_plan's
# surplus DELETE (`idx > len(cps)`) can never sweep it.
MEETING_IDX = 0
MEETING_LABEL = "Wrap-up discussion"

_CP_SQL = """
INSERT INTO checkpoints (cohort_id, idx, label, unit, start_ref, end_ref, chapter_ceiling,
                         due_at_utc, tz_id, local_wall, dst_note, is_meeting_anchor)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (cohort_id, idx) DO UPDATE SET
  label=excluded.label, unit=excluded.unit, start_ref=excluded.start_ref,
  end_ref=excluded.end_ref, chapter_ceiling=excluded.chapter_ceiling,
  due_at_utc=excluded.due_at_utc, tz_id=excluded.tz_id, local_wall=excluded.local_wall,
  dst_note=excluded.dst_note, is_meeting_anchor=excluded.is_meeting_anchor
RETURNING id
"""
_REM_SQL = ("INSERT INTO reminders (checkpoint_id, kind, due_at, grace_secs) VALUES (?,?,?,?)"
            " ON CONFLICT (checkpoint_id, kind) DO NOTHING")


def thread_kwargs(name: str, minutes: int = ARCHIVE) -> dict:
    """create_thread kwargs. MEASURED: the library defaults ``type`` to
    ``private_thread``, so passing public_thread EXPLICITLY is what keeps it visible."""
    if minutes not in ARCHIVE_OK:
        raise ValueError(f"auto_archive_duration must be one of {ARCHIVE_OK}, got {minutes}")
    return {"name": name[:100], "type": discord.ChannelType.public_thread,
            "auto_archive_duration": minutes}


def _ceiling_for(cp: schedule.Checkpoint, chapters: int, pages: int) -> int:
    # FLOORED: rounding down under-reveals, and chapters == 0 floors to refuse.
    return (cp.through_chapter if cp.kind == "chapters"
            else chapters * (cp.page_to or 0) // (pages or 1))


def _bounds(cp: schedule.Checkpoint) -> tuple[int | None, int | None]:
    return cp.page_from or cp.through_chapter, cp.page_to or cp.through_chapter


async def apply_plan(
    db: Database, cohort_id: int, cps: list[schedule.Checkpoint], tz: str, chapters: int = 0
) -> tuple[int, int]:
    """Write, or rewrite, a cohort's checkpoints and reminder fan-out; returns
    (checkpoints, reminders inserted). Reschedule is this same call: upsert on
    (cohort_id, idx), and only PENDING reminders are purged, so a re-plan cannot
    re-ping a checkpoint the club already got."""
    inserted = 0
    pages = (cps[-1].page_to or 0) if cps else 0
    async with db.tx() as conn:
        # CRITICAL: keep `AND is_meeting_anchor = 0`. Widening it deletes the meeting's
        # reminders while every surface still claims the meeting is scheduled.
        await conn.execute("DELETE FROM reminders WHERE status = 'pending' AND checkpoint_id IN"
                           " (SELECT id FROM checkpoints WHERE cohort_id = ?"
                           " AND is_meeting_anchor = 0)", (cohort_id,))
        for cp in cps:
            ceiling = _ceiling_for(cp, chapters, pages)
            local = local_wall(cp.due.instant, tz)
            async with conn.execute(_CP_SQL, (
                cohort_id, cp.seq, cp.label, cp.kind[:-1], *_bounds(cp),
                ceiling, unix(cp.due.instant), tz, local,
                cp.due.note or "", 0,
            )) as cur:
                cp_id = int((await cur.fetchone())[0])
            # The pin is re-synced here, not in unlock(): unlock runs once per thread, so
            # only a re-plan can move an already-open thread's ceiling. THIS cp's only.
            await conn.execute("UPDATE channel_policy SET max_chapter = ?,"
                               " updated_at = unixepoch() WHERE checkpoint_id = ?",
                               (ceiling, cp_id))
            for rem in schedule.reminders_for(cp):
                cur2 = await conn.execute(_REM_SQL, (cp_id, rem.kind, unix(rem.due), rem.grace))
                inserted += cur2.rowcount
        # Shorter reschedule: CASCADE takes the dropped weeks' reminders, 'sent' included,
        # AND their threads' channel_policy pins, so an old thread then FAILS CLOSED.
        await conn.execute("DELETE FROM checkpoints WHERE cohort_id = ? AND idx > ?",
                           (cohort_id, len(cps)))
    return len(cps), inserted


async def set_meeting(db: Database, cohort_id: int, d: date, t: time,
                      tz: str) -> tuple[Wall, int]:
    """Pin this cycle's wrap-up meeting: one ``is_meeting_anchor`` checkpoint plus its
    two reminders; returns (resolved wall, reminders inserted). Re-running MOVES it.

    A checkpoint ROW because that is what a reminder can point at (``checkpoint_id`` is
    NOT NULL). chapter_ceiling = 0 is load-bearing: library.ceiling() takes MAX over due
    checkpoints, so an early meeting date would otherwise unlock the whole book.
    """
    w = resolve_wall(d, t, tz)
    local = local_wall(w.instant, tz)
    inserted = 0
    async with db.tx() as conn:
        async with conn.execute(_CP_SQL, (
            cohort_id, MEETING_IDX, MEETING_LABEL, "chapter", 0, 0, 0, unix(w.instant), tz,
            local, w.note or "", 1,
        )) as cur:
            cp_id = int((await cur.fetchone())[0])
        await conn.execute("DELETE FROM reminders WHERE checkpoint_id = ? AND status = 'pending'",
                           (cp_id,))
        for rem in schedule.reminders_for_meeting(w):
            cur2 = await conn.execute(_REM_SQL, (cp_id, rem.kind, unix(rem.due), rem.grace))
            inserted += cur2.rowcount
        # Same transaction as the anchor, so the cohort-level copy cannot disagree.
        await conn.execute("UPDATE cohorts SET meeting_at_utc = ?, meeting_tz_id = ?,"
                           " meeting_local_wall = ? WHERE id = ?",
                           (unix(w.instant), tz, local, cohort_id))
    return w, inserted


async def next_checkpoint(db: Database, cohort_id: int, now: int) -> aiosqlite.Row | None:
    """Soonest checkpoint still ahead; a due-instant tie resolves to the lower idx."""
    return await db.one("SELECT * FROM checkpoints WHERE cohort_id = ? AND due_at_utc > ?"
                        " ORDER BY due_at_utc, idx LIMIT 1", cohort_id, now)


async def unit_of(db: Database, cohort_id: int) -> str | None:
    # The anchor's unit is a placeholder; unfiltered it renames the whole plan's unit.
    row = await db.one("SELECT unit FROM checkpoints WHERE cohort_id = ?"
                       " AND is_meeting_anchor = 0 LIMIT 1", cohort_id)
    return str(row[0]) if row else None  # None == no plan; guessing a unit here BRICKS a member


async def set_progress(db: Database, cohort_id: int, user_id: int, n: int, unit: str) -> int:
    """Record where a member is; member_progress is APPEND-ONLY. A LOWER number than last
    time is ACCEPTED, not clamped: people restart books, and library.ceiling() min()s
    progress with the unlock, so a lower report only ever narrows what is shown."""
    await db.run(f"INSERT INTO member_progress (cohort_id, user_id, {COL[unit]})"
                 " VALUES (?,?,?)", cohort_id, user_id, n)  # COL: literal, never user text
    return n


async def progress_of(db: Database, cohort_id: int, user_id: int, unit: str) -> int:
    row = await db.one(f"SELECT {COL[unit]} FROM member_progress WHERE cohort_id = ?"
                       f" AND user_id = ? AND {COL[unit]} IS NOT NULL"
                       " ORDER BY reported_at DESC, id DESC LIMIT 1", cohort_id, user_id)
    return int(row[0]) if row else 0


async def pace_of(db: Database, cohort_id: int, user_id: int,
                  now: int) -> tuple[float, int, str] | None:
    """(percent, units ahead of plan, unit) for one member; None with no plan."""
    # `due` is the furthest point the checkpoints that have already landed asked for --
    # 0 before the first one falls due. The meeting anchor is excluded, as everywhere
    # else: it is a date, not a reading target.
    row = await db.one("SELECT MAX(end_ref) total, MIN(unit) u, COALESCE(MAX(CASE WHEN"
                       " due_at_utc <= ? THEN end_ref END), 0) due FROM checkpoints"
                       " WHERE cohort_id = ? AND is_meeting_anchor = 0", now, cohort_id)
    if row is None or row["total"] is None:
        return None
    unit = str(row["u"])
    done = await progress_of(db, cohort_id, user_id, unit)
    pct, delta = schedule.pace(int(row["total"]), done, int(row["due"]))
    return pct, delta, unit


async def mark_dnf(db: Database, cohort_id: int, user_id: int, reason: str) -> tuple[int, int]:
    """Flag one member did-not-finish; return (dnf, roster) for THIS cohort only."""
    await db.run("UPDATE cohort_members SET dnf = 1, dnf_reason = ? WHERE cohort_id = ?"
                 " AND user_id = ?", reason, cohort_id, user_id)
    row = await db.one("SELECT COUNT(*) n, COALESCE(SUM(dnf), 0) d FROM cohort_members"
                       " WHERE cohort_id = ?", cohort_id)
    return int(row["d"]), int(row["n"])


def _paging(n: int, page: int, per: int) -> tuple[int, int, int]:
    """(page, pages, offset) for `n` rows. Empty is page 1 of 1, and out-of-range clamps."""
    pages = max(1, -(-n // per))
    page = min(max(1, page), pages)
    return page, pages, (page - 1) * per


async def history_page(db: Database, cohort_id: int, user_id: int, unit: str, page: int = 1,
                       per: int = PER_PAGE) -> tuple[list[aiosqlite.Row], int, int]:
    """One page of ONE member's own progress reports, newest first: (rows, page, pages).
    Self-only: any view of this log that spans members is a leaderboard however labelled."""
    col = COL[unit]  # literal from COL, never user text
    where = f" FROM member_progress WHERE cohort_id = ? AND user_id = ? AND {col} IS NOT NULL"
    row = await db.one(f"SELECT COUNT(*){where}", cohort_id, user_id)
    page, pages, off = _paging(int(row[0]), page, per)
    rows = await db.all(f"SELECT {col} AS n, reported_at AS t{where}"
                        " ORDER BY reported_at DESC, id DESC LIMIT ? OFFSET ?",
                        cohort_id, user_id, per, off)
    return rows, page, pages


async def library_page(db: Database, guild_id: int, page: int = 1,
                       per: int = PER_PAGE) -> tuple[list[aiosqlite.Row], int, int]:
    row = await db.one("SELECT COUNT(*) FROM cohorts WHERE guild_id = ?", guild_id)
    page, pages, off = _paging(int(row[0]), page, per)
    rows = await db.all("SELECT co.cycle_month m, co.status s, b.title t, b.author a"
                        " FROM cohorts co JOIN books b ON b.id = co.book_id WHERE co.guild_id = ?"
                        " ORDER BY co.cycle_month DESC, co.id DESC LIMIT ? OFFSET ?",
                        guild_id, per, off)
    return rows, page, pages


async def unlock(db: Database, checkpoint_id: int, create) -> int | None:
    """Open a checkpoint's chapter thread exactly once, and pin its ceiling. Idempotent
    on ``checkpoints.thread_id``. The channel_policy row is what keeps someone catching
    up in the chapter-7 thread gated at 7 after the cohort reaches 12.
    """
    cp = await db.one("SELECT * FROM checkpoints WHERE id = ?", checkpoint_id)
    if cp is None:
        return None
    if cp["thread_id"] is not None:
        return int(cp["thread_id"])
    co = await db.one("SELECT guild_id FROM cohorts WHERE id = ?", cp["cohort_id"])
    # ponytail: check-then-create, not a claim -- the reminder row is already claimed
    # 'sending' by one poller. If a second bot process ever runs, claim thread_id first.
    thread_id = int(await create(cp))
    async with db.tx() as conn:
        await conn.execute("UPDATE checkpoints SET thread_id = ?, thread_created_at = unixepoch()"
                           " WHERE id = ?", (thread_id, checkpoint_id))
        await conn.execute(
            "INSERT INTO channel_policy (channel_id, guild_id, cohort_id, checkpoint_id, kind,"
            " max_chapter) VALUES (?,?,?,?,'chapter_thread',?)"
            " ON CONFLICT (channel_id) DO NOTHING",
            (thread_id, co["guild_id"] if co else 0, cp["cohort_id"], checkpoint_id,
             cp["chapter_ceiling"]))
    return thread_id


async def open_thread(db: Database, cp: aiosqlite.Row, channel) -> int | None:
    """The 'unlock' half of bot.py's reminder callback; ``cp`` is the row it already joined."""

    async def create(row: aiosqlite.Row) -> int:
        thread = await channel.create_thread(
            **thread_kwargs(f"{cp['cycle_month']} {row['label']}"), reason="checkpoint unlock")
        await asyncio.sleep(THROTTLE)
        return thread.id

    return await unlock(db, int(cp["id"]), create)
