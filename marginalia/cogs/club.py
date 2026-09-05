"""Discord surface for the month: join/leave/roster, nominate, the ballot, /cycle-open
and /cycle-close, /status and /help. The state transitions live in marginalia/cycle.py."""

from __future__ import annotations

import datetime as dt
import re

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from marginalia import library, reminders
from marginalia.cogs._views import (
    JOINED_FIELD,
    ballot_embed,
    cover_file,
    help_embed,
    join_embed,
    nomination_embed,
    roster_embed,
    status_embed,
)
from marginalia.cycle import (
    BALLOT_HOURS,
    LIVE,
    POLL_MAX_ANSWERS,
    add_nomination,
    attach_book,
    ballot_label,
    build_poll,
    check_open,
    close_cycle,
    cohort_of,
    join_cohort,
    latest_ballot,
    leave_cohort,
    next_cycle_month,
    open_cycle,
    roster_ids,
    shortlist,
    tally,
)
from marginalia.db import Database
from marginalia.timefmt import when

_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

# The most common 403 here, undiscoverable from the error text, so the reply names it.
HIERARCHY_HELP = (
    "I could not change that role. My integration role must sit **above** the cohort role in "
    "Server Settings > Roles -- Discord refuses role edits at or below the bot's own highest "
    "role, and the 403 never says so."
)


# --- discord surface --------------------------------------------------------

async def _reply(interaction: discord.Interaction, text: str | None = None, *,
                 role: discord.Role | None = None, quiet: bool = True, **kw) -> None:
    """Every reply routes here so the AllowedMentions rule cannot be forgotten: an
    interaction parses USERS only, so a role mention would render blue and ping nobody.
    `role=` is the ONE way a reply pings anyone, and only /cycle-open passes it."""
    am = (discord.AllowedMentions(everyone=False, users=False, roles=[role]) if role
          else discord.AllowedMentions.none())
    # A None kwarg means ABSENT: discord.py's sentinel is MISSING, and `file=None` becomes
    # `files=[None]` and dies, so an optional cover cannot just be passed straight through.
    await interaction.response.send_message(text, ephemeral=quiet, allowed_mentions=am,
                                            **{k: v for k, v in kw.items() if v is not None})


async def _cover(db: Database, book_id: int | None) -> discord.File | None:
    """None for most books early on, and then the embed simply carries no thumbnail.
    cover_file() derives the filename from the media type, so attachment://NAME matches."""
    return cover_file(await library.cover(db, book_id)) if book_id else None


async def _apply_role(member: discord.Member, role: discord.Role | None, *,
                      add: bool) -> str | None:
    """Grant or strip one role; returns an error message, or None on success."""
    if role is None:
        return None
    try:
        grant = member.add_roles if add else member.remove_roles
        await grant(role, reason="Marginalia cohort")
    except discord.Forbidden:
        return HIERARCHY_HELP
    except discord.NotFound:  # member or role already gone: terminal, never retried
        pass
    return None


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mgl:join:(?P<cohort>\d+)"):
    """Persistent opt-in button. Restart survival needs all three: `timeout=None` on the
    hosting View, an explicit STABLE custom_id (a generated one makes `is_persistent()`
    False and add_view raise), and `add_dynamic_items(JoinButton)` in bot.py's setup_hook.
    """

    def __init__(self, cohort_id: int) -> None:
        self.cohort_id = cohort_id
        super().__init__(discord.ui.Button(label="Join this month",
                                           style=discord.ButtonStyle.primary,
                                           custom_id=f"mgl:join:{cohort_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /) -> JoinButton:
        return cls(int(match["cohort"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Club = interaction.client.get_cog("Club")  # type: ignore[assignment]
        await cog.opt_in(interaction, self.cohort_id)


# bot.py's setup_hook reads this off the module and calls add_dynamic_items(*it). The
# CLASS, never an instance: the cohort id is parsed back out of the custom_id, so one
# registration covers every cohort. A DynamicItem is routed by template alone, so no
# PERSISTENT_VIEWS / add_view is needed.
DYNAMIC_ITEMS = (JoinButton,)


class NominateModal(discord.ui.Modal, title="Nominate a book"):
    """One interaction, no message_content intent; Open Library enrichment was cut YAGNI."""

    book = discord.ui.TextInput(label="Title", max_length=200)
    author = discord.ui.TextInput(label="Author", required=False, max_length=120)
    why = discord.ui.TextInput(label="Why this one?", style=discord.TextStyle.paragraph,
                               required=False, max_length=400)
    pages = discord.ui.TextInput(label="Pages (optional)", required=False, max_length=5)

    def __init__(self, db: Database, cycle_month: str) -> None:
        super().__init__(title=f"Nominate a book for {cycle_month}")
        self.db, self.cycle_month = db, cycle_month

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title, author = self.book.value.strip(), self.author.value.strip()
        pages = int(self.pages.value) if self.pages.value.strip().isdigit() else None
        nom = await add_nomination(self.db, interaction.guild_id, self.cycle_month, title, author,
                                   pages, interaction.user.id)
        if nom is None:
            await _reply(interaction, f"**{title}** is already nominated for {self.cycle_month}.")
            return
        # A cover only exists once the EPUB is ingested, and a nomination carries no book_id
        # yet, so the card matches on title -- no match is the normal case, and it is fine.
        # ponytail: `why` is shown once, never stored. Add a column in migration 3 if
        # the ballot must quote it.
        row = await self.db.one("SELECT id FROM books WHERE lower(title)=lower(?) AND"
                                " ingested_at IS NOT NULL ORDER BY id DESC LIMIT 1", title)
        file = await _cover(self.db, row["id"] if row else None)
        await _reply(interaction, quiet=False, file=file,
                     embed=nomination_embed(nom, title, author, self.why.value.strip(),
                                            interaction.user.id, file, self.cycle_month))


class Club(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.db: Database = bot.db  # type: ignore[attr-defined]
        self.cfg = bot.cfg  # type: ignore[attr-defined]

    def _role(self, guild: discord.Guild, cohort: aiosqlite.Row) -> discord.Role | None:
        return guild.get_role(cohort["role_id"]) if cohort["role_id"] else None

    async def _cohort(self, interaction: discord.Interaction) -> aiosqlite.Row | None:
        cohort = await cohort_of(self.db, interaction.guild_id)
        if cohort is None:
            await _reply(interaction, "No cycle is open right now. An organizer opens one with"
                                      " /cycle-open.")
        return cohort

    # --- autocomplete: the organizer picks by title, the id travels underneath ---------

    async def nomination_ac(self, interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[int]]:
        month = await next_cycle_month(self.db, interaction.guild_id or 0)
        rows = await shortlist(self.db, interaction.guild_id or 0, month)
        low = current.lower()
        return [app_commands.Choice(name=f"{r['title']} - {r['author']}"[:100], value=r["id"])
                for r in rows if low in r["title"].lower()][:25]

    async def book_ac(self, interaction: discord.Interaction,
                      current: str) -> list[app_commands.Choice[int]]:
        rows = await self.db.all("SELECT id, title, author FROM books WHERE ingested_at IS NOT"
                                 " NULL ORDER BY id DESC LIMIT 25")
        low = current.lower()
        return [app_commands.Choice(name=f"{r['title']} - {r['author']}"[:100], value=r["id"])
                for r in rows if low in r["title"].lower()]

    # --- members ----------------------------------------------------------------------

    async def _sync_card(self, interaction: discord.Interaction, cohort) -> None:
        """Keep the signup card's count live instead of frozen at its posting time.

        The button is already ON that message, so the button path needs no fetch; /join
        and /leave fetch it by the id cycle_open stored. Edits the ONE field rather than
        rebuilding the embed, so the cover thumbnail and everything else survive. Every
        failure is swallowed: a stale count is cosmetic and must never fail a join.
        """
        try:
            msg = interaction.message
            if msg is None and cohort["signup_message_id"]:
                ch = interaction.guild.get_channel(int(cohort["channel_id"]))
                msg = await ch.fetch_message(int(cohort["signup_message_id"])) if ch else None
            if msg is None or not msg.embeds:
                return
            embed = msg.embeds[0]
            at = next((i for i, f in enumerate(embed.fields) if f.name == JOINED_FIELD), None)
            count = str(len(await roster_ids(self.db, cohort["id"])))
            if at is None or embed.fields[at].value == count:
                return  # unchanged: do not spend an edit, they are rate-limited per message
            embed.set_field_at(at, name=JOINED_FIELD, value=count, inline=True)
            await msg.edit(embed=embed)
        except (discord.HTTPException, AttributeError, KeyError):
            pass

    async def opt_in(self, interaction: discord.Interaction, cohort_id: int) -> None:
        """Shared by /join and the persistent button."""
        cohort = await self.db.one("SELECT * FROM cohorts WHERE id=?", cohort_id)
        if cohort is None or cohort["status"] not in LIVE:
            await _reply(interaction, "That cycle has closed. Watch for the next one!")
            return
        fresh = await join_cohort(self.db, cohort_id, interaction.user.id)
        err = await _apply_role(interaction.user, self._role(interaction.guild, cohort), add=True)
        await _reply(interaction, err or (
            f"You're in for {cohort['cycle_month']}. Try /next to see what is due first."
            if fresh else "You're already in for this month."))
        await self._sync_card(interaction, cohort)

    @app_commands.command(description="How Marginalia works, and every command in one place.")
    async def help(self, interaction: discord.Interaction) -> None:
        await _reply(interaction, embed=help_embed())

    @app_commands.command(description="Join this month's reading cohort.")
    async def join(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is not None:
            await self.opt_in(interaction, cohort["id"])

    @app_commands.command(description="Leave this month's cohort. No announcement.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is None:
            return
        await leave_cohort(self.db, cohort["id"], interaction.user.id)
        err = await _apply_role(interaction.user, self._role(interaction.guild, cohort), add=False)
        await _reply(interaction, err or "You're out. Rejoin any time with /join.")
        await self._sync_card(interaction, cohort)

    @app_commands.command(description="Who is reading with us this month.")
    async def roster(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is None:
            return
        ids = await roster_ids(self.db, cohort["id"])
        role = self._role(interaction.guild, cohort)
        # Public, but it pings NOBODY: a roll call anyone can run must not notify the
        # whole cohort every time. The role renders as a pill; AllowedMentions.none()
        # is what keeps it silent.
        await _reply(interaction, f"{role.mention if role else cohort['cycle_month']} --"
                     f" {len(ids)} reading this month.", quiet=False,
                     embed=roster_embed(cohort["cycle_month"], ids))

    @app_commands.command(description="Nominate a book for the next cycle.")
    async def nominate(self, interaction: discord.Interaction) -> None:
        month = await next_cycle_month(self.db, interaction.guild_id or 0)
        await interaction.response.send_modal(NominateModal(self.db, month))

    # --- organizers -------------------------------------------------------------------

    @app_commands.command(name="cycle-open", description="Organizer: open the next reading cycle")
    @app_commands.describe(
        nomination="The winning book -- start typing its title",
        book="An EPUB already added with /ingest, so members can /quote it (optional)",
        month="Cycle month as YYYY-MM. Default: the next month without a cycle",
    )
    @app_commands.autocomplete(nomination=nomination_ac, book=book_ac)
    @app_commands.default_permissions()
    async def cycle_open(self, interaction: discord.Interaction, nomination: int | None = None,
                         book: int | None = None, month: str | None = None) -> None:
        """Everything that can refuse runs BEFORE create_role, so no misfire leaves an
        orphan role; and if the database write fails after the role exists, the role is
        deleted again rather than left for the organizer to find."""
        if interaction.channel_id != self.cfg.channel_id:
            await _reply(interaction, f"Open the cycle in <#{self.cfg.channel_id}> -- that is the"
                                      " configured book club channel, and it is where reminders"
                                      " and chapter threads go for the whole month.")
            return
        if (live := await cohort_of(self.db, interaction.guild_id)) is not None:
            await _reply(interaction, f"{live['cycle_month']} is still open. Close it with"
                                      " /cycle-close first, then open the next one.")
            return
        month = month or await next_cycle_month(self.db, interaction.guild_id)
        if not _MONTH.fullmatch(month):
            await _reply(interaction, f"`{month}` is not a month. Use YYYY-MM, e.g. 2026-10.")
            return
        if await self.db.one("SELECT 1 FROM cohorts WHERE guild_id=? AND cycle_month=?",
                             interaction.guild_id, month):
            await _reply(interaction, f"There is already a cycle for {month}. Pick another"
                                      " month or leave it blank for the next free one.")
            return
        try:
            await check_open(self.db, nomination, book)
        except ValueError as exc:
            await _reply(interaction, f"Cannot open that: {exc}.")
            return
        # mentionable=True makes the monthly ping one API call.
        role = await interaction.guild.create_role(name=f"Marginalia {month}", mentionable=True,
                                                   reason="Marginalia cohort")
        try:
            cohort_id = await open_cycle(self.db, interaction.guild_id, interaction.channel_id,
                                         nomination, cycle_month=month, role_id=role.id,
                                         tz=self.cfg.tz, book_id=book)
        except Exception:
            await role.delete(reason="Marginalia: cycle failed to open")
            raise
        row = await self.db.one("SELECT b.id, b.title, b.author FROM books b"
                                " JOIN cohorts c ON c.book_id=b.id WHERE c.id=?", cohort_id)
        file = await _cover(self.db, row["id"])
        view = discord.ui.View(timeout=None)
        view.add_item(JoinButton(cohort_id))
        # Ping in the content, card in the embed: an embed cannot ping, so moving the role
        # mention into join_embed would silently stop the one announcement that matters.
        await _reply(interaction, f"{role.mention} is open for {month}. Tap to opt in for"
                     " the month.", role=role, quiet=False, view=view, file=file,
                     embed=join_embed(month, row["title"], row["author"], 0, file))
        # Remember the card, so /join and /leave can refresh its count too -- the button
        # gets the message for free, a slash command does not.
        card = await interaction.original_response()
        await self.db.run("UPDATE cohorts SET signup_message_id=? WHERE id=?",
                          card.id, cohort_id)

    @app_commands.command(name="cycle-book",
                          description="Organizer: attach a book to the open cycle")
    @app_commands.describe(book="The book to quote from, already added with /ingest-library")
    @app_commands.autocomplete(book=book_ac)
    @app_commands.default_permissions()
    async def cycle_book(self, interaction: discord.Interaction, book: int) -> None:
        """The repair for a cycle opened before its EPUB existed: quoting stays refused
        until the cohort points at a row that actually holds text."""
        if (cohort := await self._cohort(interaction)) is None:
            return
        try:
            old, new = await attach_book(self.db, cohort["id"], book)
        except ValueError as exc:
            await _reply(interaction, f"Cannot attach that: {exc}.")
            return
        await _reply(interaction, f"{cohort['cycle_month']} now reads **{new}**"
                     + (f", was **{old}**." if old != new else ".")
                     + " Re-run /schedule so the checkpoints unlock real chapters.")

    @app_commands.command(name="cycle-close",
                          description="Organizer: end the cycle and take back the month's role")
    @app_commands.default_permissions()
    async def cycle_close(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is None:
            return
        await interaction.response.defer(ephemeral=True)  # 30 sequential calls will exceed 3s
        members = await close_cycle(self.db, cohort["id"])
        role = self._role(interaction.guild, cohort)
        failed = 0
        # NO bulk role endpoint: 30 members is 30 requests, awaited SEQUENTIALLY. Do NOT
        # asyncio.gather -- one rate-limit bucket, so it buys nothing and invites 429s.
        for uid in members:
            member = interaction.guild.get_member(uid)
            if member is not None and await _apply_role(member, role, add=False):
                failed += 1
        await interaction.followup.send(
            f"Closed {cohort['cycle_month']}. Role stripped from {len(members) - failed}; all"
            f" {len(members)} membership rows kept for the library."
            f"{f' {failed} failed: ' + HIERARCHY_HELP if failed else ''}", ephemeral=True)

    @app_commands.command(name="ballot", description="Organizer: post the vote on the nominations")
    @app_commands.describe(month="Cycle month as YYYY-MM. Default: the next month without a cycle")
    @app_commands.default_permissions()
    async def ballot(self, interaction: discord.Interaction, month: str | None = None) -> None:
        month = month or await next_cycle_month(self.db, interaction.guild_id)
        noms = await shortlist(self.db, interaction.guild_id, month)
        if not 1 <= len(noms) <= POLL_MAX_ANSWERS:
            await _reply(interaction, f"{len(noms)} nominations for {month} we have not already"
                         f" read; a ballot holds 1..{POLL_MAX_ANSWERS}. Add or withdraw some first"
                         " -- choosing which to drop is the organizer's call, not mine.")
            return
        poll = build_poll(f"What are we reading in {month}?",
                          [ballot_label(n["title"], n["author"]) for n in noms])
        closes = int(dt.datetime.now(dt.UTC).timestamp()) + BALLOT_HOURS * 3600
        await _reply(interaction, "Approval voting: tick every book you are willing to read."
                     f" Closes {when(closes)}. I cannot vote -- bots never can.",
                     quiet=False, poll=poll)
        msg = await interaction.original_response()
        # add_answer assigns ids 1..n in insertion order (ENV.md s3) = shortlist order,
        # so the join back to `nominations` is positional.
        async with self.db.tx() as conn:
            for answer_id, n in enumerate(noms, start=1):
                await conn.execute("UPDATE nominations SET status='on_ballot', poll_message_id=?,"
                                   " poll_answer_id=? WHERE id=?", (msg.id, answer_id, n["id"]))

    @app_commands.command(name="ballot-result", description="Organizer: read the finished ballot")
    @app_commands.describe(message_id="The poll message's id. Default: the latest ballot")
    @app_commands.default_permissions()
    async def ballot_result(self, interaction: discord.Interaction,
                            message_id: str | None = None) -> None:
        mid = int(message_id) if message_id and message_id.isdigit() else (
            await latest_ballot(self.db, interaction.guild_id))
        if mid is None:
            await _reply(interaction, "No ballot has been posted yet. /ballot starts one.")
            return
        try:
            msg = await interaction.channel.fetch_message(mid)
        except discord.NotFound:
            await _reply(interaction, "I cannot find that poll in this channel. Run this where"
                                      " the ballot was posted.")
            return
        # An ABSENT results field means unknown, NOT zero: refuse until finalized.
        if msg.poll is None or not msg.poll.is_finalized():
            await _reply(interaction, "That poll has not finished yet -- come back once it"
                                      " closes. Missing counts mean unknown, not zero.")
            return
        rows = await self.db.all("SELECT id, title, poll_answer_id FROM nominations"
                                 " WHERE poll_message_id=?", msg.id)
        by_answer = {r["poll_answer_id"]: r["id"] for r in rows}
        title = {r["id"]: r["title"] for r in rows}
        counts = {by_answer[a.id]: a.vote_count for a in msg.poll.answers if a.id in by_answer}
        votes, leaders = tally(counts)
        ranked = sorted(((title[i], v) for i, v in counts.items()), key=lambda r: -r[1])
        await _reply(interaction, quiet=False, embed=ballot_embed(
            votes, [title[i] for i in leaders], ranked,
            leaders[0] if len(leaders) == 1 else None))

    @app_commands.command(description="Organizer: is the bot actually scheduling?")
    @app_commands.default_permissions()
    async def status(self, interaction: discord.Interaction) -> None:
        """The heartbeat is read even with no cycle open: process liveness is not scheduler
        liveness, and that gap is the failure deploy/watchdog.sh exists for."""
        now = int(dt.datetime.now(dt.UTC).timestamp())
        beat = await self.db.one("SELECT beat_at FROM heartbeat WHERE name=?", reminders.BEAT)
        cohort = await cohort_of(self.db, interaction.guild_id)
        rows = await self._status_rows(cohort, now) if cohort else {}
        await _reply(interaction, embed=status_embed(cohort=cohort, now=now, beat=beat, **rows))

    async def _status_rows(self, cohort: aiosqlite.Row, now: int) -> dict:
        """Four small reads, every one scoped to ONE cohort so the counts mean this month."""
        sched = await self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(due_at_utc <= ?),0) AS fired,"
            " MIN(CASE WHEN due_at_utc > ? THEN due_at_utc END) AS nxt"
            " FROM checkpoints WHERE cohort_id=?", now, now, cohort["id"])
        rem = {r["status"]: r["c"] for r in await self.db.all(
            "SELECT r.status, COUNT(*) AS c FROM reminders r JOIN checkpoints cp"
            " ON cp.id=r.checkpoint_id WHERE cp.cohort_id=? GROUP BY r.status", cohort["id"])}
        book = await self.db.one(
            "SELECT title, chapter_count, ingested_at, (SELECT COALESCE(SUM(para_count),0)"
            " FROM chapters WHERE book_id=books.id) AS paras FROM books WHERE id=?",
            cohort["book_id"])
        return {"sched": sched, "rem": rem, "book": book,
                "members": len(await roster_ids(self.db, cohort["id"]))}


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Club(bot))
