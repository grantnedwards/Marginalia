"""The reading cycle's lifecycle: months, cohorts, membership, nominations, the ballot.

Plain ``async def f(db, ...)`` state transitions with no Interaction in sight, which is
what lets the whole month be tested with no token. ``cogs/club.py`` is the Discord
surface over these.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import aiosqlite
import discord

from marginalia.db import Database

# Discord's documented Poll limits. discord.py 2.7.1 validates NONE of them locally
# (docs/ENV.md s3), so ours must run BEFORE send or it is a live 400.
POLL_MAX_ANSWERS, POLL_MAX_QUESTION, POLL_MAX_ANSWER = 10, 300, 55
BALLOT_HOURS = 72
LIVE = ("open", "active")


def this_month() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m")


def month_after(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


async def next_cycle_month(db: Database, guild_id: int) -> str:
    """The month the club is picking a book FOR: the first month, from this one on, that
    has no cohort yet. So nominating while September's book is being read targets
    October, and on 1 October /cycle-open lands on October -- nobody types a month."""
    month = this_month()
    for _ in range(24):
        if await db.one("SELECT 1 FROM cohorts WHERE guild_id=? AND cycle_month=?",
                        guild_id, month) is None:
            return month
        month = month_after(month)
    return month


async def check_open(db: Database, nomination_id: int | None,
                     book_id: int | None) -> aiosqlite.Row | None:
    """Everything /cycle-open can refuse WITHOUT touching Discord, so a refusal never
    leaves an orphan role behind. Returns the nomination row (None when opening on a
    bare book). Organizer-typed ids: validate rather than take an FK error, because
    attaching a not-yet-ingested row silently rebuilds the 0-ceiling bug."""
    if nomination_id is None and book_id is None:
        raise ValueError("name a nomination, an ingested book, or both")
    nom = None
    if nomination_id is not None:
        nom = await db.one("SELECT * FROM nominations WHERE id=?", nomination_id)
        if nom is None:
            raise ValueError(f"there is no nomination #{nomination_id}")
    if book_id is not None and await db.one(
            "SELECT 1 FROM books WHERE id=? AND ingested_at IS NOT NULL", book_id) is None:
        raise ValueError(f"book #{book_id} is not ingested -- /ingest the EPUB first")
    return nom


async def open_cycle(db: Database, guild_id: int, channel_id: int,
                     nomination_id: int | None = None, *, cycle_month: str | None = None,
                     role_id: int | None = None, tz: str = "UTC",
                     book_id: int | None = None) -> int:
    """Open the month's cohort on a nomination, an ingested book, or both. New cohort id.

    `book_id` is an ALREADY-INGESTED books row (the id `/ingest` prints), which is what
    makes the ceilings apply_plan writes non-zero. Without it we create a metadata-only
    row from the nomination: everything but quoting works, and quoting stays refused --
    there is no text. Raises ValueError on anything it will not open; the cog runs
    `check_open` itself first, BEFORE it creates the Discord role.
    """
    nom = await check_open(db, nomination_id, book_id)
    book_id = book_id or (nom["book_id"] if nom is not None else None)
    async with db.tx() as conn:
        if book_id is None:
            cur = await conn.execute("INSERT INTO books (title, author, page_count) VALUES (?,?,?)",
                                     (nom["title"], nom["author"], nom["page_count"]))
            book_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month, role_id, tz_id,"
            " status, opened_at) VALUES (?,?,?,?,?,?,'open',unixepoch())",
            (guild_id, channel_id, book_id, cycle_month or this_month(), role_id, tz))
        if nom is not None:
            await conn.execute("UPDATE nominations SET status='approved', book_id=? WHERE id=?",
                               (book_id, nomination_id))
    return int(cur.lastrowid or 0)


async def attach_book(db: Database, cohort_id: int, book_id: int) -> tuple[str, str]:
    """Point a LIVE cohort at an already-ingested book; returns (old title, new title).

    This is the repair for opening a cycle before the EPUB was ingested. `open_cycle`
    with no `book_id` makes a metadata-only row from the nomination, and quoting stays
    refused against it because it has no text. Re-pointing here is what turns quoting on
    without closing the month and stripping everyone's role.

    The caller must re-run /schedule afterwards: `apply_plan` bakes `chapter_ceiling`
    from the book's chapter_count at plan time, so checkpoints written against the old
    0-chapter row still floor every ceiling to 0.
    """
    if await db.one("SELECT 1 FROM books WHERE id=? AND ingested_at IS NOT NULL",
                    book_id) is None:
        raise ValueError(f"book #{book_id} is not ingested -- /ingest-library it first")
    row = await db.one("SELECT c.book_id, b.title FROM cohorts c JOIN books b ON b.id=c.book_id"
                       " WHERE c.id=?", cohort_id)
    if row is None:
        raise ValueError("that cycle no longer exists")
    old_id, old_title = int(row[0]), str(row[1])
    new = await db.one("SELECT title FROM books WHERE id=?", book_id)
    if old_id == book_id:
        return old_title, str(new[0])
    async with db.tx() as conn:
        await conn.execute("UPDATE cohorts SET book_id=? WHERE id=?", (book_id, cohort_id))
        await conn.execute("UPDATE nominations SET book_id=? WHERE book_id=?", (book_id, old_id))
        # Sweep the stub this replaces, but ONLY a text-less one nothing else points at:
        # a real ingested book stays, and the books FK from cohorts is RESTRICT anyway.
        await conn.execute(
            "DELETE FROM books WHERE id=? AND ingested_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM cohorts WHERE book_id=?)", (old_id, old_id))
    return old_title, str(new[0])


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


async def latest_ballot(db: Database, guild_id: int) -> int | None:
    """The message id of the most recent poll /ballot posted here, so /ballot-result
    needs no id typed. Snowflakes grow with time, so MAX is the newest."""
    row = await db.one("SELECT MAX(poll_message_id) FROM nominations WHERE guild_id=?"
                       " AND poll_message_id IS NOT NULL", guild_id)
    return int(row[0]) if row and row[0] is not None else None


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
