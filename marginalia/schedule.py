"""Checkpoint and reminder planning, and pace. Pure -- no I/O, clock read, db, or discord
-- so a DST-crossing eight-week schedule is testable with no token and no database. Every
*calendar* step is timefmt's; timedelta appears only where no wall clock is implied."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from marginalia.timefmt import Wall, resolve_wall, weekly

BY = ("pages", "chapters")


@dataclass(frozen=True)
class Checkpoint:
    seq: int
    label: str
    through_chapter: int | None
    page_from: int | None
    page_to: int | None
    due: Wall
    kind: str


@dataclass(frozen=True)
class Reminder:
    kind: str
    due: datetime  # aware UTC
    grace: int  # seconds late still worth sending; < 0 == infinite


# `unlock` is a durable state transition, so late delivery is still correct: negative ==
# infinite grace. The rest are perishable -- "starts in 1 hour" three hours late is a lie.
GRACE = {"unlock": -1, "T-24h": 6 * 3600, "T-1h": 45 * 60}
_BEFORE = {"unlock": 0, "T-24h": 24 * 3600, "T-1h": 3600}


def _split(total: int, parts: int) -> list[tuple[int, int]]:
    """Remainder goes to the EARLIEST parts, so the last week is never double-length."""
    base, extra = divmod(total, parts)
    out, start = [], 1
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        out.append((start, start + size - 1))
        start += size
    return out


def _validate(total: int, weeks: int, weekday: int, by: str) -> None:
    if by not in BY:
        raise ValueError(f"by must be one of {BY}, got {by!r}")
    if total < 1 or weeks < 1:
        raise ValueError(f"total and weeks must both be >= 1, got {total} and {weeks}")
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be 0 (Monday) .. 6 (Sunday), got {weekday}")
    if total < weeks:
        raise ValueError(
            f"cannot cover {total} {by} in {weeks} weeks without an empty "
            f"checkpoint; use at most {total} weeks"
        )


def plan(
    total: int, weeks: int, weekday: int, at: time, tz: str, by: str = "pages", *, start: date
) -> list[Checkpoint]:
    """`start` is keyword-only and required: this module may not read a clock, and a
    weekday alone does not name a week."""
    _validate(total, weeks, weekday, by)
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    dues = weekly(resolve_wall(first, at, tz), weeks, tz)
    noun = "Chapter" if by == "chapters" else "Page"
    ch = by == "chapters"
    return [
        Checkpoint(
            seq,
            f"{noun} {lo}" if lo == hi else f"{noun}s {lo}-{hi}",
            hi if ch else None,
            None if ch else lo,
            None if ch else hi,
            due,
            by,
        )
        for seq, ((lo, hi), due) in enumerate(zip(_split(total, weeks), dues, strict=True), 1)
    ]


def reminders_for(cp: Checkpoint) -> list[Reminder]:
    return [
        Reminder(k, cp.due.instant - timedelta(seconds=_BEFORE[k]), GRACE[k])
        for k in ("T-24h", "T-1h", "unlock")
    ]


def reminders_for_meeting(due: Wall) -> list[Reminder]:
    """No 'unlock' twin: the meeting unlocks no chapters, so nothing durable is owed late."""
    return [
        Reminder(f"meeting-{k}", due.instant - timedelta(seconds=_BEFORE[k]), GRACE[k])
        for k in ("T-24h", "T-1h")
    ]


def pace(total: int, done: int, expected: int) -> tuple[float, int]:
    """(percent complete 0..100, units ahead of the plan) -- negative is behind.

    `expected` is what the checkpoints that have already FALLEN DUE asked for, not a
    share of the elapsed calendar. That distinction is the whole point: on a weekly plan
    nothing new is owed until the next checkpoint lands, so a member who has read exactly
    what was asked reads as 0 rather than as "a chapter behind" for six days out of every
    seven. Clamped into 0..total, so a hand-edited row cannot put the expectation past
    the end of the book.
    """
    if total < 1:
        raise ValueError(f"total must be >= 1, got {total}")
    return 100.0 * done / total, done - max(0, min(expected, total))
