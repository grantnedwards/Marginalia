"""The reading cycle's Discord surface: /schedule, /meeting, /next, /pace, /progress
set|show, /library, /dnf, /mystats.

The state transitions live in marginalia/progress.py; the cog methods only turn an
Interaction into arguments. Embeds and buttons are _views.py's, and are for STRUCTURE
only -- a one-line confirmation ("Noted: page 42.") stays a plain string, which reads
faster than a titled embed.

ANTI-FEATURES, a design boundary, do not "helpfully" add them: no leaderboards, no
streaks, no public ranking, no shaming by omission (no public completion list, no
per-member nagging). /pace, /mystats and /dnf are ephemeral, self-only, unattributed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time
from typing import Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from marginalia import library, schedule
from marginalia.cogs import _views
from marginalia.cycle import cohort_of
from marginalia.db import Database
from marginalia.progress import (
    PLANNABLE,
    THROTTLE,
    apply_plan,
    history_page,
    library_page,
    mark_dnf,
    next_checkpoint,
    pace_of,
    progress_of,
    set_meeting,
    set_progress,
    unit_of,
)
from marginalia.timefmt import unix, when

log = logging.getLogger("marginalia.reading")

EVENT_CAP = 100  # SCHEDULED-or-ACTIVE scheduled events, per guild
EVENT_SECS = 3600
# Discord shows these as a dropdown; nobody has to know that Monday is 0.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class Reading(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _cohort(self, interaction: discord.Interaction) -> aiosqlite.Row | None:
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.response.send_message(
                "No cycle is open right now. An organizer opens one with /cycle-open.",
                ephemeral=True)
        return co

    async def _events(self, guild: discord.Guild, month: str, cps: list, loc: str) -> int:
        """DISCRETE one-shot events, one per checkpoint. MEASURED (ENV.md 6):
        create_scheduled_event has NO ``recurrence_rule`` in 2.7.1, so a finite list is
        the only available encoding. Discord also sends NO advance event reminder, only a
        go-live ping -- which is why marginalia.reminders exists: do not delete the poller
        believing these cover T-24h and T-1h.

        # ponytail: a re-plan ADDS a fresh set rather than reconciling with the events an
        # earlier run made, so the Events tab collects duplicates. Pass `events: False`
        # when re-running /schedule, or delete the stale ones by hand. Reconciling means
        # deleting the bot's own events, which is destructive enough not to do blind.
        """
        live = (discord.EventStatus.scheduled, discord.EventStatus.active)
        if sum(1 for e in guild.scheduled_events if e.status in live) + len(cps) > EVENT_CAP:
            return 0  # never create 100 to find out
        made = 0
        for cp in cps:
            try:
                # NO `channel=` KEY AT ALL. An EXTERNAL event forbids a channel, but the
                # library's sentinel is MISSING, not None -- `channel=None` counts as SET
                # and raises TypeError before a request is ever made. This shipped that
                # way and created zero events, silently, because the TypeError also broke
                # out of the confirm callback. Same MISSING-vs-None trap as `file=None`
                # in club._reply; do not "clarify" it by passing None back in.
                await guild.create_scheduled_event(
                    name=f"{month} {cp.label}"[:100], start_time=cp.due.instant,
                    end_time=datetime.fromtimestamp(unix(cp.due.instant) + EVENT_SECS, UTC),
                    entity_type=discord.EntityType.external,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    location=loc)  # EXTERNAL requires a location in entity_metadata
            except (discord.HTTPException, TypeError, ValueError):
                # Terminal for the whole batch. Creation needs create_events (bit 44) --
                # the library docstring's manage_events is STALE. Caught broadly on
                # purpose: the calendar is a nicety, and nothing here may cost the
                # organizer the confirmation that their checkpoints were written.
                log.warning("scheduled events stopped after %d", made, exc_info=True)
                break
            made += 1
            await asyncio.sleep(THROTTLE)
        return made

    @app_commands.command(description="Organizer: plan the weekly checkpoints, then confirm.")
    @app_commands.describe(
        total="How long the book is: total pages, or total chapters if by=chapters",
        weeks="How many weekly checkpoints to split it into",
        weekday="The day each checkpoint falls on",
        at="Time of day in the club's timezone, HH:MM (default 19:00)",
        by="Split the book by pages (default) or by chapters",
        start="First week's date as YYYY-MM-DD (default: this week)",
        events="Also add each checkpoint to the server's Events tab",
    )
    @app_commands.default_permissions()
    async def schedule(
        self, interaction: discord.Interaction,
        total: app_commands.Range[int, 1, 5000], weeks: app_commands.Range[int, 1, 52],
        weekday: Weekday, at: str = "19:00",
        by: Literal["pages", "chapters"] = "pages", start: str = "", events: bool = True,
    ) -> None:
        # DEFER FIRST -- the token dies at 3s. Then EDIT THE ORIGINAL: a followup after
        # a defer drops the ephemeral flag, publishing the organizer-only preview.
        await interaction.response.defer(ephemeral=True, thinking=True)
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.edit_original_response(
                content="No cycle is open yet -- /cycle-open first, then plan it.")
            return
        try:
            cps = schedule.plan(total, weeks, WEEKDAYS.index(weekday), time.fromisoformat(at),
                                co["tz_id"], by,
                                start=date.fromisoformat(start) if start else date.today())
        except ValueError as exc:
            await interaction.edit_original_response(
                content=f"Cannot plan that: {exc}. `at` is HH:MM and `start` is YYYY-MM-DD.")
            return
        book = await self.db.one("SELECT title, chapter_count FROM books WHERE id = ?",
                                co["book_id"])
        preview = "\n".join([
            f"**{book['title'] if book else 'the book'}** - {len(cps)} checkpoints,"
            f" {len(schedule.reminders_for(cps[0])) * len(cps)} reminders,"
            f" {len(cps) if events else 0} events, every {weekday} at {at} ({co['tz_id']})",
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
            # The checkpoints are already committed by here, so the organizer is TOLD
            # so even if the calendar half went wrong. Reporting "0 events" is a far
            # better failure than a button that appears to do nothing.
            await inter.edit_original_response(
                content=f"Scheduled {n} checkpoints, {rem} new reminders, {made} events."
                        + ("" if made or not events else
                           " (No events: check the bot has Create Events, or see the log.)")
                        + " Reminders already sent were left alone.", view=None)

        await interaction.edit_original_response(content=preview, view=_views.Confirm(run))

    @app_commands.command(description="What is due next.")
    async def next(self, interaction: discord.Interaction) -> None:
        co = await self._cohort(interaction)
        if co is None:
            return
        cp = await next_checkpoint(self.db, co["id"], int(datetime.now(UTC).timestamp()))
        if cp is None:
            await interaction.response.send_message(
                "Nothing is scheduled ahead -- either the plan is finished, or an organizer"
                " has not run /schedule yet.", ephemeral=True)
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
    @app_commands.describe(number="The page or chapter you have reached")
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
    @app_commands.describe(page="Page of the list to show")
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

    @app_commands.command(description="Organizer: set the cycle's wrap-up meeting.")
    @app_commands.describe(on="Date as YYYY-MM-DD",
                           at="Time of day in the club's timezone, HH:MM (default 19:00)")
    @app_commands.default_permissions()
    async def meeting(self, interaction: discord.Interaction, on: str, at: str = "19:00") -> None:
        # No Confirm view: one checkpoint and two reminders, and re-running just moves them.
        await interaction.response.defer(ephemeral=True, thinking=True)
        co = await cohort_of(self.db, interaction.guild_id or 0, PLANNABLE)
        if co is None:
            await interaction.edit_original_response(
                content="No cycle is open yet -- /cycle-open first.")
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
    @app_commands.describe(page="Page of the list to show")
    async def library(self, interaction: discord.Interaction,
                      page: app_commands.Range[int, 1, 999] = 1) -> None:
        await _views.send_page(
            interaction, "Books this club has read",
            lambda r: f"`{r['m']}` **{r['t']}** - {r['a']} ({r['s']})",
            lambda p: library_page(self.db, interaction.guild_id or 0, p),
            page, "Nothing read yet.")

    @app_commands.command(description="Set this book down. No judgement, no announcement.")
    @app_commands.describe(reason="Optional, for your own record only")
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
