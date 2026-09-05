"""Only the silent bugs: wall clock across DST, and the negative control."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from marginalia import timefmt

CHI = ZoneInfo("America/Chicago")


def test_weekly_holds_local_wall_clock_across_dst():
    first = timefmt.resolve_wall(date(2027, 2, 21), time(19, 0), "America/Chicago")
    walls = timefmt.weekly(first, 8, "America/Chicago")
    local = [w.instant.astimezone(CHI) for w in walls]
    assert len(walls) == 8
    assert {(d.hour, d.minute) for d in local} == {(19, 0)}
    # 2027-03-14 is the transition: offsets must differ either side, hour must not.
    assert {d.utcoffset() for d in local} == {timedelta(hours=-6), timedelta(hours=-5)}

    # NEGATIVE CONTROL: a timedelta on the aware instant carries the stale
    # pre-transition offset, so week 3 lands at 20:00 local, an hour wrong.
    bad = (first.instant + timedelta(weeks=3)).astimezone(CHI)
    assert (bad.hour, bad.minute) == (20, 0)
    good = local[3]
    assert good.date() == bad.date() and (good.hour, good.minute) == (19, 0)


def test_spring_forward_gap_pushed_past():
    w = timefmt.resolve_wall(date(2027, 3, 14), time(2, 30), "America/Chicago")
    assert w.instant == datetime(2027, 3, 14, 8, 30, tzinfo=UTC)  # 03:30 CDT
    assert w.note == "gap: pushed forward"


def test_fall_back_ambiguity_takes_earlier():
    w = timefmt.resolve_wall(date(2027, 11, 7), time(1, 30), "America/Chicago")
    assert w.instant == datetime(2027, 11, 7, 6, 30, tzinfo=UTC)  # CDT, not 07:30 CST
    assert w.note == "ambiguous: took earlier"


def test_lord_howe_half_hour_gap():
    w = timefmt.resolve_wall(date(2027, 10, 3), time(2, 15), "Australia/Lord_Howe")
    assert w.instant == datetime(2027, 10, 2, 15, 45, tzinfo=UTC)  # 02:45 +11, +30min
    assert w.note == "gap: pushed forward"


@pytest.mark.parametrize("tz, start, offsets", [
    ("Australia/Sydney", date(2027, 3, 28), 2),  # southern hemisphere, DST ends
    ("America/Phoenix", date(2027, 3, 7), 1),  # no DST at all
    ("Australia/Lord_Howe", date(2027, 9, 19), 2),  # 30-minute shift
])
def test_wall_clock_holds_in_other_zones(tz, start, offsets):
    zone = ZoneInfo(tz)
    walls = timefmt.weekly(timefmt.resolve_wall(start, time(19, 0), tz), 4, tz)
    local = [w.instant.astimezone(zone) for w in walls]
    assert {(d.hour, d.minute) for d in local} == {(19, 0)}
    assert len({d.utcoffset() for d in local}) == offsets


def test_seconds_not_millis_and_naive_raises():
    dt = datetime(2026, 9, 4, 20, 30, 45, tzinfo=UTC)
    assert timefmt.ts(dt) == "<t:1788553845:F>"  # millis would read as year ~55000
    assert timefmt.when(dt) == "<t:1788553845:F> (<t:1788553845:R>)"
    with pytest.raises(ValueError):
        timefmt.unix(dt.replace(tzinfo=None))
