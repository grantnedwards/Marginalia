"""The reading cycle: /schedule, /meeting, /next, /pace, /progress set|show, /library,
/dnf, /mystats, plus the per-chapter thread each checkpoint unlock opens.

Logic is plain ``async def f(db, ...)``; the cog methods only turn an Interaction into
arguments, which is why these tests need no token. Calendar arithmetic is schedule.py's;
embeds and buttons are _views.py's, and are for STRUCTURE only -- a one-line confirmation
("Noted: page 42.") stays a plain string, which reads faster than a titled embed.

ANTI-FEATURES, a design boundary, do not "helpfully" add them: no leaderboards, no
streaks, no public ranking, no shaming by omission (no public completion list, no
per-member nagging). /pace, /mystats and /dnf are ephemeral, self-only, unattributed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time
from typing import Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from marginalia import library, schedule
from marginalia.cogs import _views
from marginalia.cogs.club import LIVE, cohort_of
from marginalia.db import Database
from marginalia.timefmt import Wall, local_wall, resolve_wall, unix, when

ARCHIVE = 10080  # 7 days: a monthly club's thread must not vanish mid-week
ARCHIVE_OK = (60, 1440, 4320, 10080)  # the only values Discord accepts
EVENT_CAP = 100  # SCHEDULED-or-ACTIVE scheduled events, per guild
EVENT_SECS = 3600
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
    row = await db.one("SELECT MIN(due_at_utc) a, MAX(due_at_utc) b, MAX(end_ref) total,"
                       " MIN(unit) u FROM checkpoints WHERE cohort_id = ?"
                       " AND is_meeting_anchor = 0", cohort_id)
    if row is None or row["total"] is None:
        return None
    unit = str(row["u"])
    done = await progress_of(db, cohort_id, user_id, unit)
    pct, delta = schedule.pace(
        int(row["total"]), done,
        datetime.fromtimestamp(int(row["a"]) - WEEK, UTC),  # week 1 covers the week before due 1
        datetime.fromtimestamp(int(row["b"]), UTC), datetime.fromtimestamp(now, UTC))
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


class Reading(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _cohort(self, interaction: discord.Interaction) -> aiosqlite.Row | None:
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.response.send_message("No cohort here yet.", ephemeral=True)
        return co

    async def _events(self, guild: discord.Guild, month: str, cps: list, loc: str) -> int:
        """DISCRETE one-shot events, one per checkpoint. MEASURED (ENV.md 6):
        create_scheduled_event has NO ``recurrence_rule`` in 2.7.1, so a finite list is
        the only available encoding. Discord also sends NO advance event reminder, only a
        go-live ping -- which is why marginalia.reminders exists: do not delete the poller
        believing these cover T-24h and T-1h.
        """
        live = (discord.EventStatus.scheduled, discord.EventStatus.active)
        if sum(1 for e in guild.scheduled_events if e.status in live) + len(cps) > EVENT_CAP:
            return 0  # never create 100 to find out
        made = 0
        for cp in cps:
            try:
                await guild.create_scheduled_event(
                    name=f"{month} {cp.label}"[:100], start_time=cp.due.instant,
                    end_time=datetime.fromtimestamp(unix(cp.due.instant) + EVENT_SECS, UTC),
                    entity_type=discord.EntityType.external,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    channel=None,  # EXTERNAL forbids a channel ...
                    location=loc)  # ... and requires a location in entity_metadata
            except (discord.Forbidden, discord.NotFound):
                # Terminal. Creation needs create_events (bit 44) -- the library
                # docstring's manage_events is STALE.
                break
            made += 1
            await asyncio.sleep(THROTTLE)
        return made

    @app_commands.command(description="Plan this cohort's weekly checkpoints (organizers).")
    @app_commands.default_permissions()
    async def schedule(
        self, interaction: discord.Interaction,
        total: app_commands.Range[int, 1, 5000], weeks: app_commands.Range[int, 1, 52],
        weekday: app_commands.Range[int, 0, 6], at: str = "19:00",
        by: Literal["pages", "chapters"] = "pages", start: str = "", events: bool = True,
    ) -> None:
        # DEFER FIRST -- the token dies at 3s. Then EDIT THE ORIGINAL: a followup after
        # a defer drops the ephemeral flag, publishing the organizer-only preview.
        await interaction.response.defer(ephemeral=True, thinking=True)
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.edit_original_response(content="No cohort here yet.")
            return
        try:
            cps = schedule.plan(total, weeks, weekday, time.fromisoformat(at), co["tz_id"], by,
                                start=date.fromisoformat(start) if start else date.today())
        except ValueError as exc:
            await interaction.edit_original_response(content=f"Cannot plan that: {exc}")
            return
        book = await self.db.one("SELECT title, chapter_count FROM books WHERE id = ?",
                                co["book_id"])
        preview = "\n".join([
            f"**{book['title'] if book else 'the book'}** - {len(cps)} checkpoints,"
            f" {len(schedule.reminders_for(cps[0])) * len(cps)} reminders,"
            f" {len(cps) if events else 0} events",
            f"first: {cps[0].label} {when(cps[0].due.instant)}",
            f"last: {cps[-1].label} {when(cps[-1].due.instant)}",
            *sorted(f"DST: {n}" for n in {c.due.note for c in cps if c.due.note}),
            "Nothing is written until you press the button.",
        ])

        async def run(inter: discord.Interaction) -> None:
            n, rem = await apply_plan(self.db, co["id"], cps, co["tz_id"],
                                      int(book["chapter_count"]) if book else 0)
            made = 0
            if events and inter.guild is not None:
                ch = inter.guild.get_channel(int(co["channel_id"]))
                made = await self._events(inter.guild, co["cycle_month"], cps,
                                          f"#{ch.name}" if ch else "Discord")
            await inter.edit_original_response(
                content=f"Scheduled {n} checkpoints, {rem} new reminders, {made} events."
                        " Reminders already sent were left alone.", view=None)

        await interaction.edit_original_response(content=preview, view=_views.Confirm(run))

    @app_commands.command(description="What is due next.")
    async def next(self, interaction: discord.Interaction) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        cp = await next_checkpoint(self.db, co["id"], int(datetime.now(UTC).timestamp()))
        if cp is None:
            await interaction.response.send_message("Nothing scheduled ahead.", ephemeral=True)
            return
        # No cover is the normal case early on; the embed is built to read fine without one.
        cover = _views.cover_file(await library.cover(self.db, int(co["book_id"])))
        await interaction.response.send_message(
            embed=_views.next_embed(cp, str(co["cycle_month"]), cover), ephemeral=True,
            **_views.opts(file=cover,
                          view=_views.thread_view(interaction.guild_id or 0, cp["thread_id"])))

    @app_commands.command(description="How you are doing. Only you see this.")
    async def pace(self, interaction: discord.Interaction) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        got = await pace_of(self.db, co["id"], interaction.user.id,
                            int(datetime.now(UTC).timestamp()))
        if got is None:
            await interaction.response.send_message("No schedule yet.", ephemeral=True)
            return
        await interaction.response.send_message(embed=_views.pace_embed(*got), ephemeral=True)

    progress = app_commands.Group(name="progress", description="Where you are in the book.")

    @progress.command(name="set", description="Record where you are. Only you see this.")
    async def progress_set(self, interaction: discord.Interaction,
                           number: app_commands.Range[int, 0, 100_000]) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        if (unit := await unit_of(self.db, co["id"])) is None:
            await interaction.response.send_message(
                "No schedule yet - an organizer needs to run /schedule first.", ephemeral=True)
            return
        await set_progress(self.db, co["id"], interaction.user.id, number, unit)
        await interaction.response.send_message(f"Noted: {unit} {number}.", ephemeral=True)

    @progress.command(name="show", description="What you have reported so far. Only you see this.")
    async def progress_show(self, interaction: discord.Interaction,
                            page: app_commands.Range[int, 1, 999] = 1) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        unit = await unit_of(self.db, co["id"]) or "chapter"  # read-only: no plan, no rows
        await _views.send_page(
            interaction, "What you have reported",
            lambda r: f"{unit} {r['n']} - {when(int(r['t']))}",
            lambda p: history_page(self.db, co["id"], interaction.user.id, unit, p),
            page, "Nothing reported yet.")

    @app_commands.command(description="Set this cycle's wrap-up meeting (organizers).")
    @app_commands.default_permissions()
    async def meeting(self, interaction: discord.Interaction, on: str, at: str = "19:00") -> None:
        # No Confirm view: one checkpoint and two reminders, and re-running just moves them.
        await interaction.response.defer(ephemeral=True, thinking=True)
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.edit_original_response(content="No cohort here yet.")
            return
        try:
            w, rem = await set_meeting(self.db, co["id"], date.fromisoformat(on),
                                       time.fromisoformat(at), co["tz_id"])
        except ValueError as exc:  # a date or time Discord happily accepted as text
            await interaction.edit_original_response(content=f"Cannot read that date: {exc}")
            return
        await interaction.edit_original_response(
            content=f"Meeting {when(w.instant)}, {rem} new reminders (24h and 1h before)."
                    + (f" DST: {w.note}" if w.note else ""))

    @app_commands.command(description="Books this club has read.")
    async def library(self, interaction: discord.Interaction,
                      page: app_commands.Range[int, 1, 999] = 1) -> None:
        await _views.send_page(
            interaction, "Books this club has read",
            lambda r: f"`{r['m']}` **{r['t']}** - {r['a']} ({r['s']})",
            lambda p: library_page(self.db, interaction.guild_id or 0, p),
            page, "Nothing read yet.")

    @app_commands.command(description="Set this book down. No judgement, no announcement.")
    async def dnf(self, interaction: discord.Interaction, reason: str = "") -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        d, n = await mark_dnf(self.db, co["id"], interaction.user.id, reason)
        await interaction.response.send_message(embed=_views.dnf_embed(d, n), ephemeral=True)

    @app_commands.command(description="Your own numbers. Only you see this.")
    async def mystats(self, interaction: discord.Interaction) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        unit = await unit_of(self.db, co["id"]) or "chapter"
        done = await progress_of(self.db, co["id"], interaction.user.id, unit)
        row = await self.db.one("SELECT COUNT(*) n, COALESCE(SUM(dnf), 0) d FROM cohort_members"
                                " WHERE user_id = ?", interaction.user.id)
        await interaction.response.send_message(
            embed=_views.mystats_embed(unit, done, int(row["n"]), int(row["d"])), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reading(bot, bot.db))  # type: ignore[attr-defined]
