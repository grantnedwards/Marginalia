"""Month-scoped cohort membership, nominations, and the ballot that picks the book."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from marginalia import library, reminders
from marginalia.cogs._views import (
    ballot_embed,
    cover_file,
    join_embed,
    nomination_embed,
    roster_embed,
    status_embed,
)
from marginalia.db import Database
from marginalia.timefmt import when

# Discord's documented Poll limits. discord.py 2.7.1 validates NONE of them locally
# (docs/ENV.md s3), so ours must run BEFORE send or it is a live 400.
POLL_MAX_ANSWERS, POLL_MAX_QUESTION, POLL_MAX_ANSWER = 10, 300, 55
BALLOT_HOURS = 72
LIVE = ("open", "active")

# The most common 403 here, undiscoverable from the error text, so the reply names it.
HIERARCHY_HELP = (
    "I could not change that role. My integration role must sit **above** the cohort role in "
    "Server Settings > Roles -- Discord refuses role edits at or below the bot's own highest "
    "role, and the 403 never says so."
)


def this_month() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m")


# --- state transitions: db in, plain values out -----------------------------

async def open_cycle(db: Database, guild_id: int, channel_id: int, nomination_id: int, *,
                     cycle_month: str | None = None, role_id: int | None = None,
                     tz: str = "UTC", book_id: int | None = None) -> int:
    """Promote a nomination to the month's cohort. New cohort id.

    `book_id` is an ALREADY-INGESTED books row (the id `/ingest` prints), which is what
    makes the ceilings apply_plan writes non-zero. Without it we create a metadata-only
    row: everything but quoting works, and quoting stays refused -- there is no text.
    """
    nom = await db.one("SELECT * FROM nominations WHERE id=?", nomination_id)
    if nom is None:
        raise ValueError(f"no nomination {nomination_id}")
    book_id = book_id or nom["book_id"]
    # Organizer-typed id: validate rather than take an FK error, because attaching a
    # not-yet-ingested row silently rebuilds the 0-ceiling bug.
    if book_id is not None and await db.one(
            "SELECT 1 FROM books WHERE id=? AND ingested_at IS NOT NULL", book_id) is None:
        raise ValueError(f"book {book_id} is not ingested -- /ingest the EPUB first")
    async with db.tx() as conn:
        if book_id is None:
            cur = await conn.execute("INSERT INTO books (title, author, page_count) VALUES (?,?,?)",
                                     (nom["title"], nom["author"], nom["page_count"]))
            book_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month, role_id, tz_id,"
            " status, opened_at) VALUES (?,?,?,?,?,?,'open',unixepoch())",
            (guild_id, channel_id, book_id, cycle_month or this_month(), role_id, tz))
        await conn.execute("UPDATE nominations SET status='approved', book_id=? WHERE id=?",
                           (book_id, nomination_id))
    return int(cur.lastrowid or 0)


async def close_cycle(db: Database, cohort_id: int) -> list[int]:
    """End the month; returns the members whose role the caller must now strip. The
    `cohort_members` rows are KEPT -- the role going away is what makes the subscription
    month-scoped, the rows staying is what keeps /library and DNF stats possible."""
    members = await roster_ids(db, cohort_id)
    async with db.tx() as conn:
        await conn.execute("UPDATE cohorts SET status='closed', closed_at=unixepoch(),"
                           " role_revoked_at=unixepoch() WHERE id=?", (cohort_id,))
        await conn.execute("UPDATE cohort_members SET role_granted=0 WHERE cohort_id=?",
                           (cohort_id,))
    return members


async def join_cohort(db: Database, cohort_id: int, user_id: int) -> bool:
    """Idempotent; True when this is a new or renewed membership (rowcount 0 = already in)."""
    cur = await db.run(
        "INSERT INTO cohort_members (cohort_id, user_id, role_granted) VALUES (?,?,1)"
        " ON CONFLICT (cohort_id, user_id) DO UPDATE SET left_at=NULL, role_granted=1,"
        " joined_at=unixepoch() WHERE cohort_members.left_at IS NOT NULL", cohort_id, user_id)
    return bool(cur.rowcount)


async def leave_cohort(db: Database, cohort_id: int, user_id: int) -> None:
    await db.run("UPDATE cohort_members SET left_at=unixepoch(), role_granted=0"
                 " WHERE cohort_id=? AND user_id=? AND left_at IS NULL", cohort_id, user_id)


async def cohort_of(db: Database, guild_id: int,
                    statuses: Sequence[str] = LIVE) -> aiosqlite.Row | None:
    """The guild's newest cohort in `statuses`, or None. ONE query for both cogs -- the
    status set is the only thing that ever differed (reading.py also plans 'draft')."""
    holes = ",".join("?" * len(statuses))  # placeholders, never user text
    return await db.one(f"SELECT * FROM cohorts WHERE guild_id=? AND status IN ({holes})"
                        " ORDER BY id DESC LIMIT 1", guild_id, *statuses)


async def roster_ids(db: Database, cohort_id: int) -> list[int]:
    return [r["user_id"] for r in await db.all(
        "SELECT user_id FROM cohort_members WHERE cohort_id=? AND left_at IS NULL"
        " ORDER BY joined_at, user_id", cohort_id)]


async def add_nomination(db: Database, guild_id: int, cycle_month: str, title: str, author: str,
                         pages: int | None, by: int) -> int | None:
    """None means this exact title+author is already on this cycle's list."""
    cur = await db.run("INSERT INTO nominations (guild_id, cycle_month, title, author, page_count,"
                       " nominated_by) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                       guild_id, cycle_month, title, author, pages, by)
    return int(cur.lastrowid or 0) if cur.rowcount else None


async def shortlist(db: Database, guild_id: int, cycle_month: str) -> list[aiosqlite.Row]:
    """Live nominations for the cycle, minus anything the club already finished (a
    `cohorts` row with `closed_at`). Returns every match, so an over-cap ballot shows."""
    return await db.all("SELECT n.* FROM nominations n WHERE n.guild_id=? AND n.cycle_month=?"
                        " AND n.status IN ('proposed','on_ballot') AND NOT EXISTS ("
                        "  SELECT 1 FROM cohorts c JOIN books b ON b.id=c.book_id"
                        "  WHERE c.guild_id=n.guild_id AND c.closed_at IS NOT NULL"
                        "  AND lower(b.title)=lower(n.title)) ORDER BY n.id", guild_id, cycle_month)


def tally(counts: Mapping[int, int]) -> tuple[int, list[int]]:
    """Approval tally keyed by nomination id -> (top votes, leaders). More than one leader
    is a TIE, reported and never coin-flipped: a silent flip is unauditable."""
    top = max(counts.values()) if counts else 0
    return top, sorted(k for k, v in counts.items() if v == top)


def ballot_label(title: str, author: str) -> str:
    """<=55 chars, title first so truncation never destroys the identity."""
    label = f"{title} - {author}" if author else title
    return label if len(label) <= POLL_MAX_ANSWER else label[:POLL_MAX_ANSWER - 3].rstrip() + "..."


def build_poll(question: str, answers: Sequence[str], hours: int = BALLOT_HOURS) -> discord.Poll:
    """Check our own limits, then build it. The keyword is `multiple`; `allow_multiselect`
    is a hard TypeError in 2.7.1. Approval voting: the most WILLING-to-read book wins."""
    if not 1 <= len(answers) <= POLL_MAX_ANSWERS:
        raise ValueError(f"poll takes 1..{POLL_MAX_ANSWERS} answers, got {len(answers)}")
    if not 1 <= len(question) <= POLL_MAX_QUESTION:
        raise ValueError(f"question must be 1..{POLL_MAX_QUESTION} chars, got {len(question)}")
    for a in answers:
        if not 1 <= len(a) <= POLL_MAX_ANSWER:
            raise ValueError(f"answer must be 1..{POLL_MAX_ANSWER} chars: {a!r}")
    if not 1 <= hours <= 168:
        raise ValueError(f"duration must be 1..168 hours, got {hours}")
    poll = discord.Poll(question=question, duration=dt.timedelta(hours=hours), multiple=True)
    for a in answers:
        poll.add_answer(text=a)
    return poll


# --- discord surface --------------------------------------------------------

async def _reply(interaction: discord.Interaction, text: str | None = None, *,
                 role: discord.Role | None = None, quiet: bool = True, **kw) -> None:
    """Every reply routes here so the AllowedMentions rule cannot be forgotten: an
    interaction parses USERS only, so a role mention would render blue and ping nobody."""
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
        super().__init__()
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
                                            interaction.user.id, file))


class Club(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.db: Database = bot.db  # type: ignore[attr-defined]
        self.cfg = bot.cfg  # type: ignore[attr-defined]

    def _role(self, guild: discord.Guild, cohort: aiosqlite.Row) -> discord.Role | None:
        return guild.get_role(cohort["role_id"]) if cohort["role_id"] else None

    async def _cohort(self, interaction: discord.Interaction) -> aiosqlite.Row | None:
        cohort = await cohort_of(self.db, interaction.guild_id)
        if cohort is None:
            await _reply(interaction, "No cycle is open. An organizer opens one with /cycle-open.")
        return cohort

    async def opt_in(self, interaction: discord.Interaction, cohort_id: int) -> None:
        """Shared by /join and the persistent button."""
        cohort = await self.db.one("SELECT * FROM cohorts WHERE id=?", cohort_id)
        if cohort is None or cohort["status"] not in ("open", "active"):
            await _reply(interaction, "That cycle is closed.")
            return
        fresh = await join_cohort(self.db, cohort_id, interaction.user.id)
        err = await _apply_role(interaction.user, self._role(interaction.guild, cohort), add=True)
        await _reply(interaction, err or (f"You're in for {cohort['cycle_month']}." if fresh
                                         else "You were already in."))

    @app_commands.command(description="Join this month's reading cohort.")
    async def join(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is not None:
            await self.opt_in(interaction, cohort["id"])

    @app_commands.command(description="Leave this month's cohort.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is None:
            return
        await leave_cohort(self.db, cohort["id"], interaction.user.id)
        err = await _apply_role(interaction.user, self._role(interaction.guild, cohort), add=False)
        await _reply(interaction, err or "You're out. Rejoin any time.")

    @app_commands.command(description="Who is reading with us this month.")
    async def roster(self, interaction: discord.Interaction) -> None:
        if (cohort := await self._cohort(interaction)) is None:
            return
        ids = await roster_ids(self.db, cohort["id"])
        role = self._role(interaction.guild, cohort)
        # The role mention stays HERE, in the content: it is the only place a ping lands.
        await _reply(interaction, f"{role.mention if role else cohort['cycle_month']} --"
                     f" {len(ids)} reading this month.", role=role, quiet=False,
                     embed=roster_embed(cohort["cycle_month"], ids))

    @app_commands.command(description="Nominate a book for the next cycle.")
    async def nominate(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NominateModal(self.db, this_month()))

    @app_commands.command(name="cycle-open", description="Organizer: open a cycle")
    @app_commands.default_permissions()
    async def cycle_open(self, interaction: discord.Interaction, nomination: int,
                         month: str | None = None, book: int | None = None) -> None:
        """`book` is the id /ingest printed: ingest the EPUB first, then open the cycle ON
        it, so there is ONE books row and the ceilings are not all 0.
        ponytail: a raw int like `nomination` -- add book_ac autocomplete if anyone asks.
        """
        # Wrong-channel guard, BEFORE create_role so a misfire leaves no orphan role.
        if interaction.channel_id != self.cfg.channel_id:
            await _reply(interaction, f"Open the cycle in <#{self.cfg.channel_id}> -- that is the"
                                      " configured book club channel, and it is where reminders"
                                      " and chapter threads go for the whole month.")
            return
        month = month or this_month()
        # mentionable=True makes the monthly ping one API call.
        role = await interaction.guild.create_role(name=f"Marginalia {month}", mentionable=True,
                                                   reason="Marginalia cohort")
        cohort_id = await open_cycle(self.db, interaction.guild_id, interaction.channel_id,
                                     nomination, cycle_month=month, role_id=role.id,
                                     book_id=book)
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

    @app_commands.command(name="cycle-close", description="Organizer: close it, strip the role")
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

    @app_commands.command(name="ballot", description="Organizer: post the nomination poll")
    @app_commands.default_permissions()
    async def ballot(self, interaction: discord.Interaction, month: str | None = None) -> None:
        month = month or this_month()
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
    @app_commands.default_permissions()
    async def ballot_result(self, interaction: discord.Interaction, message_id: str) -> None:
        msg = await interaction.channel.fetch_message(int(message_id))
        # An ABSENT results field means unknown, NOT zero: refuse until finalized.
        if msg.poll is None or not msg.poll.is_finalized():
            await _reply(interaction, "That poll has not finalized yet. Missing counts mean"
                         " unknown, not zero.")
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
