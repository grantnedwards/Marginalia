"""Discord timestamp markup and DST-safe local wall-clock scheduling. Deliberately no
discord import, so the tests run with no bot token. Markup is a literal `<t:SECONDS:X>`
-- seconds, not millis; millis render as roughly year 55000."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

STYLES = "tTdDfFR"  # Discord documents 9; s/S unused here, do not widen.


def unix(dt: datetime) -> int:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime {dt!r}: attach a tzinfo")
    return int(dt.timestamp())


def ts(dt: datetime | int, style: str = "F") -> str:
    if len(style) != 1 or style not in STYLES:
        raise ValueError(f"style {style!r} not one of {STYLES!r}")
    return f"<t:{dt if isinstance(dt, int) else unix(dt)}:{style}>"


def when(dt: datetime | int) -> str:
    return f"{ts(dt, 'F')} ({ts(dt, 'R')})"


def local_wall(instant: datetime, tz: str) -> str:
    """The stored display copy of an instant: 'YYYY-MM-DD HH:MM' as read in ``tz``."""
    return instant.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Wall:
    instant: datetime
    note: str | None


def _resolve(naive: datetime, zone: ZoneInfo) -> Wall:
    """Gap -> pushed forward past it; ambiguous -> earlier instant (fold=0), early not late."""
    aware = naive.replace(tzinfo=zone)
    # A gap time has no round trip: UTC->local lands past the gap.
    back = aware.astimezone(UTC).astimezone(zone)
    if back.replace(tzinfo=None) != naive:
        return Wall(back.astimezone(UTC), "gap: pushed forward")
    if aware.utcoffset() != aware.replace(fold=1).utcoffset():
        return Wall(aware.astimezone(UTC), "ambiguous: took earlier")
    return Wall(aware.astimezone(UTC), None)


def resolve_wall(d: date, t: time, tz: str) -> Wall:
    return _resolve(datetime.combine(d, t.replace(tzinfo=None)), ZoneInfo(tz))


def weekly(first: Wall, count: int, tz: str) -> list[Wall]:
    """Advance the *naive* calendar, then resolve: a timedelta on the aware instant would
    carry the stale pre-transition offset and silently slide the hour across a DST boundary."""
    if count < 1:
        return []
    zone = ZoneInfo(tz)
    local = first.instant.astimezone(zone).replace(tzinfo=None)
    return [first, *(_resolve(local + timedelta(weeks=i), zone) for i in range(1, count))]
