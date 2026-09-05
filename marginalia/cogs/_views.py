"""Every embed and view the three cogs send: data in, ``discord.Embed`` out, plus the one
paginator. No client, no db, no I/O, so a card is testable with no bot and no token.

AN EMBED CANNOT PING. Discord ignores AllowedMentions for embed bodies, so a mention moved
in here renders as a name and silently reaches nobody. Every ping stays in the message
CONTENT, where each cog's ``_reply`` applies explicit AllowedMentions.

Every instant goes through ``timefmt.when()``. tests/test_invariants.py checks that by
AST, and an embed field is not an exception to it.

reading.py's ANTI-FEATURES boundary holds here too: no ranking, no streak, no "you are
behind" nudge, no naming a member. A button only re-renders a page its own member asked
for; nothing here starts a conversation the member did not.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Mapping, Sequence

import discord

from marginalia.timefmt import when

CELLS = 8
FULL, EMPTY = "▰", "▱"  # text, so the bar costs no image, no upload, no CDN
COLOUR = discord.Colour.dark_teal()
DESC_MAX = 4096  # Discord's embed description ceiling
FOOT_MAX = 2048
PAGER_TIMEOUT = 600.0


def bar(pct: float, cells: int = CELLS) -> str:
    """Text progress bar: 50% of 8 cells -> ``'▰▰▰▰▱▱▱▱ 50%'``.

    Clamped at BOTH ends: schedule.pace() can hand back over 100 for someone who read
    past the plan's total, and a negative would render as an empty bar plus '-4%'.
    """
    pct = min(100.0, max(0.0, pct))
    filled = round(cells * pct / 100)
    return f"{FULL * filled}{EMPTY * (cells - filled)} {pct:.0f}%"


def opts(**kw) -> dict:
    """discord.py separates MISSING from None, so ``view=None`` does NOT mean "no view".
    Drop the Nones rather than hand them to send_message."""
    return {k: v for k, v in kw.items() if v is not None}


def cover_file(blob: tuple[bytes, str] | None) -> discord.File | None:
    """``library.cover()`` -> (bytes, media type), or None, which is normal. Discord
    matches ``attachment://NAME`` byte for byte, so the name comes from the media type."""
    if blob is None:
        return None
    data, mime = blob
    return discord.File(io.BytesIO(data), filename=f"cover.{'png' if 'png' in mime else 'jpg'}")


def _thumb(e: discord.Embed, cover: discord.File | None) -> discord.Embed:
    """The one place a cover becomes a thumbnail, so the URL always matches cover_file()."""
    if cover is not None:
        e.set_thumbnail(url=f"attachment://{cover.filename}")
    return e


def _reach(cp) -> str:
    """How much to read. A chapters plan stores the same ref twice, so it reads
    "chapter 5", not "chapters 5-5"."""
    a, b, unit = cp["start_ref"], cp["end_ref"], cp["unit"]
    return f"{unit}s {a}-{b}" if a and b and a != b else f"{unit} {b or a or 0}"


def next_embed(cp, month: str = "", cover: discord.File | None = None) -> discord.Embed:
    """What is due, when, and how much to read. The thread is a link BUTTON, not a field."""
    e = discord.Embed(title=str(cp["label"]), colour=COLOUR)
    e.add_field(name="Due", value=when(int(cp["due_at_utc"])), inline=False)
    e.add_field(name="Read to", value=_reach(cp), inline=True)
    foot = " - ".join(x for x in (month, str(cp["dst_note"] or "")) if x)
    if foot:
        e.set_footer(text=foot[:FOOT_MAX])
    return _thumb(e, cover)


def thread_view(guild_id: int, thread_id: int | None) -> discord.ui.View | None:
    """A LINK button: no custom_id, no callback, no round trip to us, so there is nothing
    to persist and a restart cannot break it. None when the thread is not open yet."""
    if not thread_id:
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Open the thread", style=discord.ButtonStyle.link,
                                 url=f"https://discord.com/channels/{guild_id}/{thread_id}"))
    return v


def pace_embed(pct: float, delta: int, unit: str) -> discord.Embed:
    """Bar first: on a phone the member reads the bar and stops. Behind is phrased as
    distance from the plan -- never a reprimand, and never compared to anyone else."""
    gap = ("right on the plan" if delta == 0 else
           f"{abs(delta)} {unit}s {'ahead' if delta > 0 else 'from the plan'}")
    e = discord.Embed(title="Your pace", description=bar(pct), colour=COLOUR)
    e.add_field(name="Versus the plan", value=gap, inline=False)
    e.set_footer(text="Only you can see this.")
    return e


def mystats_embed(unit: str, done: int, joined: int, dropped: int) -> discord.Embed:
    e = discord.Embed(title="Your numbers", colour=COLOUR)
    e.add_field(name="This book", value=f"{unit} {done}", inline=True)
    e.add_field(name="Books joined", value=str(joined), inline=True)
    e.add_field(name="Set down", value=str(dropped), inline=True)
    e.set_footer(text="Only you can see this.")
    return e


def dnf_embed(dnf: int, roster: int) -> discord.Embed:
    """The club aggregate, and only the aggregate: a share and a headcount, no names.
    roster 0 cannot happen for a member who just ran /dnf, but /0 is still guarded."""
    pct = 100.0 * dnf / roster if roster else 0.0
    e = discord.Embed(title="Set down. Nobody is told.", colour=COLOUR)
    e.add_field(name="This book", value=f"{pct:.0f}% of {roster} here did not finish it",
                inline=False)
    e.set_footer(text="No name, no announcement, no follow-up.")
    return e


def page_embed(title: str, lines: list[str], page: int, pages: int, empty: str) -> discord.Embed:
    """One page as a description, not one field per row: 5 rows never approach 4096, while
    a field per row would hit the 25-field wall on a long list."""
    e = discord.Embed(title=title, description=("\n".join(lines) or empty)[:DESC_MAX],
                      colour=COLOUR)
    e.set_footer(text=f"page {page}/{pages}")
    return e


class Confirm(discord.ui.View):
    """/schedule's one button: 8 checkpoints, 24 reminders and 8 events are expensive to
    undo. Lives here with the other views; the work it runs stays in the cog."""

    def __init__(self, run) -> None:
        super().__init__(timeout=PAGER_TIMEOUT)
        self._run = run

    @discord.ui.button(label="Create schedule", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        # A COMPONENT defer edits this same ephemeral message, so the edit stays ephemeral.
        await interaction.response.defer()
        await self._run(interaction)


Render = Callable[[int], Awaitable[tuple[discord.Embed, int, int]]]


class Pager(discord.ui.View):
    """Prev/next over one member's own pages, replacing a re-typed ``page:`` argument.

    NON-PERSISTENT deliberately: a timeout costs one re-run of the command, while a
    persistent view costs a stable custom_id, a registry entry and a restart path. No
    interaction_check either -- these messages are ephemeral, so only the member who ran
    the command can see, let alone press, them.
    """

    def __init__(self, render: Render, page: int, pages: int,
                 *, timeout: float = PAGER_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.render, self.page, self.pages = render, page, pages
        self.prev.disabled = page <= 1
        self.next.disabled = page >= pages

    async def _go(self, interaction: discord.Interaction, step: int) -> None:
        # render() re-clamps the page, so a stale click can never run off either end.
        embed, page, pages = await self.render(self.page + step)
        await interaction.response.edit_message(embed=embed,
                                               view=Pager(self.render, page, pages))

    @discord.ui.button(label="< prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, -1)

    @discord.ui.button(label="next >", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, 1)


def pager(render: Render, page: int, pages: int) -> Pager | None:
    """No buttons at all on a single page: two dead buttons read worse than none."""
    return Pager(render, page, pages) if pages > 1 else None


async def send_page(interaction, title: str, fmt, fetch, page: int, empty: str) -> None:
    """One paginated ephemeral reply. ``fetch(page)`` is any of reading.py's
    ``-> (rows, page, pages)`` readers; ``fmt(row)`` is one line. Shared by /library and
    /progress show so the ephemeral flag and the page clamp are written once."""

    async def render(p: int) -> tuple[discord.Embed, int, int]:
        rows, p, pages = await fetch(p)
        return page_embed(title, [fmt(r) for r in rows], p, pages, empty), p, pages

    embed, page, pages = await render(page)
    await interaction.response.send_message(embed=embed, ephemeral=True,
                                           **opts(view=pager(render, page, pages)))


# --------------------------------------------------------------- quote cards
# Spoiler bars render in a DESCRIPTION and nowhere else -- not in a title, not in a
# footer -- so barred text only ever goes in `body`, and cogs/quote.py fits it there.

CITATIONS = "Citations"  # a constant title; a title that varies with the query can leak


def quote_embed(body: str, byline: str, cover: discord.File | None = None) -> discord.Embed:
    """A passage as a card. ``body`` arrives already barred AND already fitted by the
    caller, because clipping inside ``||...||`` renders the tail in plaintext."""
    e = discord.Embed(description=body, colour=COLOUR)
    e.set_footer(text=byline[:FOOT_MAX])
    return _thumb(e, cover)


def find_embed(locators: list[str], byline: str,
               cover: discord.File | None = None) -> discord.Embed:
    """Citations only: no book text, so nothing here is barred and slicing is safe."""
    e = quote_embed("\n".join(f"- {loc}" for loc in locators)[:DESC_MAX], byline, cover)
    e.title = CITATIONS
    return e


def refusal_embed(reason: str) -> discord.Embed:
    """The reason and NOTHING else -- no title, no footer, no thumbnail. A title carrying
    the query, a chapter title or a match count is the oracle a refusal exists to deny."""
    return discord.Embed(description=reason, colour=COLOUR)


# ---------------------------------------------------------------- club cards
# /status flags a stale beat well BEFORE deploy/watchdog.sh's STALE=600 restarts the
# container, so an organizer sees the failure mode rather than only its remedy.
STALE_BEAT = 180
REM_KEYS = ("pending", "sent", "skipped", "failed")
ROSTER_LIMIT, ROSTER_FIELDS = 1024, 3  # Discord's field-value cap; 3 of them holds ~115 readers


def _pack(mentions: list[str]) -> tuple[list[str], int]:
    """Mentions into <=ROSTER_FIELDS values of <=ROSTER_LIMIT chars; (values, dropped)."""
    out: list[str] = []
    for i, m in enumerate(mentions):
        if out and len(out[-1]) + 2 + len(m) <= ROSTER_LIMIT:
            out[-1] += ", " + m
        elif len(out) < ROSTER_FIELDS:
            out.append(m)
        else:
            return out, len(mentions) - i
    return out, 0


def roster_embed(cycle_month: str, ids: Sequence[int]) -> discord.Embed:
    e = discord.Embed(title=f"Reading in {cycle_month}", colour=discord.Colour.blurple(),
                      description=f"{len(ids)} reading" if ids else "Nobody yet -- /join to start.")
    values, dropped = _pack([f"<@{i}>" for i in ids])
    for n, value in enumerate(values, start=1):
        e.add_field(name="Members" if n == 1 else f"... and on ({n})", value=value, inline=False)
    if dropped:
        e.set_footer(text=f"+{dropped} more not shown")
    return e


def join_embed(cycle_month: str, title: str, author: str, joined: int,
               cover: discord.File | None = None) -> discord.Embed:
    """The month's invitation, the single most-seen message the bot posts."""
    e = discord.Embed(title=title or "This month's book", colour=discord.Colour.blurple(),
                      description=f"by {author}" if author else "Author unrecorded")
    e.add_field(name="Cycle", value=cycle_month)
    e.add_field(name="Joined so far", value=str(joined))
    e.set_footer(text="Tap Join this month to opt in. /leave any time.")
    return _thumb(e, cover)


def nomination_embed(nom_id: int, title: str, author: str, pitch: str, by: int,
                     cover: discord.File | None = None) -> discord.Embed:
    e = discord.Embed(title=title, colour=discord.Colour.blurple(),
                      description=pitch or "No pitch given -- add one next time.")
    e.add_field(name="Author", value=author or "unrecorded")
    e.add_field(name="Nominated by", value=f"<@{by}>")
    e.set_footer(text=f"Nomination #{nom_id} -- an organizer puts it on the /ballot.")
    return _thumb(e, cover)


def ballot_embed(votes: int, leaders: Sequence[str], counts: Sequence[tuple[str, int]],
                 open_id: int | None = None) -> discord.Embed:
    """A TIE is presented AS a tie -- no winner line, and no coin flip anywhere."""
    tie = len(leaders) > 1
    head = f"Tied at {votes} approvals" if tie else f"{leaders[0] if leaders else 'Nothing'} wins"
    e = discord.Embed(title=head, colour=discord.Colour.yellow() if tie
                      else discord.Colour.green(),
                      description="Your call -- I will not coin-flip a month of reading." if tie
                      else f"{votes} approvals.")
    # Titles are sliced: ten answers at the 200-char title column would blow the 1024 cap.
    e.add_field(name="Approvals", value="\n".join(f"**{n}** {t[:60]}" for t, n in counts)
                or "no votes recorded", inline=False)
    if open_id is not None:
        e.set_footer(text=f"Open it with /cycle-open nomination:{open_id}")
    return e


def status_embed(*, cohort: Mapping[str, object] | None = None, now: int = 0, members: int = 0,
                 book: Mapping[str, object] | None = None, sched: Mapping[str, int] | None = None,
                 rem: Mapping[str, int] | None = None,
                 beat: Mapping[str, int] | None = None) -> discord.Embed:
    """Is the bot ACTUALLY scheduling? One embed, WORST STATE WINS on the colour: that colour
    is the whole value of /status -- a failed reminder, and a beat that stopped while the
    process kept running, are both invisible without it."""
    rem = dict(rem or {})
    rem["pending"] = rem.get("pending", 0) + rem.pop("sending", 0)  # in flight reads as pending
    age = None if beat is None else now - int(beat["beat_at"])
    stale = age is None or age > STALE_BEAT
    worst = 2 if rem.get("failed") or stale else bool(rem.get("skipped"))  # 2 red, 1 yellow
    e = discord.Embed(title="Marginalia status", colour=(discord.Colour.green,
                      discord.Colour.yellow, discord.Colour.red)[worst]())
    # The heartbeat is shown even with no cycle: process liveness is not scheduler liveness.
    e.add_field(name="Heartbeat", inline=False, value="never ticked -- the poller has not run "
                "since this database was made" if age is None else
                f"{age}s ago, {when(int(beat['beat_at']))}"
                + (f" -- STALE (> {STALE_BEAT}s)" if stale else ""))
    if cohort is None:
        e.description = "No live cycle. An organizer opens one with /cycle-open."
        return e
    e.add_field(name="Cohort", value=f"**{book['title'] if book else 'unknown book'}** -- "
                f"{cohort['cycle_month']}, {members} reading, `{cohort['status']}`", inline=False)
    e.add_field(name="Schedule", inline=False,
                value="No checkpoints planned -- /schedule plan" if not (sched and sched["n"])
                else f"{sched['fired']}/{sched['n']} checkpoints fired\n"
                + (f"next {when(int(sched['nxt']))}" if sched["nxt"]
                   else "all fired, nothing left"))
    e.add_field(name="Reminders", value=" | ".join(f"{k} **{rem.get(k, 0)}**" for k in REM_KEYS),
                inline=False)
    e.add_field(name="Book text", inline=False,
                value="No EPUB ingested -- quoting stays refused, everything else works"
                if not (book and book["ingested_at"])
                else f"{book['chapter_count']} chapters, {book['paras']} paragraphs searchable")
    return e
