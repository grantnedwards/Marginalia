"""Only the silent bugs: exact coverage, remainder placement, DST, grace, pace."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from marginalia import schedule
from marginalia.timefmt import resolve_wall

CHI = ZoneInfo("America/Chicago")
MON = date(2027, 2, 15)  # a Monday; weekday 6 below makes the first due 2027-02-21


def cps(total, weeks, by="pages"):
    return schedule.plan(total, weeks, 6, time(19, 0), "America/Chicago", by, start=MON)


@pytest.mark.parametrize("total,weeks", [(245, 4), (7, 3), (300, 4), (10, 10), (1, 1), (52, 7)])
def test_coverage_is_exact(total, weeks):
    plan = cps(total, weeks)
    covered = [u for cp in plan for u in range(cp.page_from, cp.page_to + 1)]
    assert covered == list(range(1, total + 1))  # gapless, in order, no overlap
    assert all(cp.page_to >= cp.page_from for cp in plan)  # no empty checkpoint
    assert [cp.seq for cp in plan] == list(range(1, weeks + 1))


def test_remainder_is_spread_early_not_dumped_on_the_last_week():
    sizes = [cp.page_to - cp.page_from + 1 for cp in cps(245, 4)]
    assert sizes == [62, 61, 61, 61]
    assert max(sizes) - min(sizes) <= 1


def test_chapters_mode_uses_through_chapter_and_leaves_pages_null():
    plan = cps(10, 3, "chapters")
    assert [cp.through_chapter for cp in plan] == [4, 7, 10]
    assert all(cp.page_from is None and cp.page_to is None for cp in plan)
    assert plan[-1].label == "Chapters 8-10"


def test_degenerate_inputs_raise_as_documented():
    with pytest.raises(ValueError, match="empty checkpoint"):
        cps(3, 4)
    with pytest.raises(ValueError, match=">= 1"):
        cps(10, 0)
    with pytest.raises(ValueError, match="by must be"):
        cps(10, 2, "paragraphs")


def test_due_keeps_local_wall_time_across_dst():
    local = [cp.due.instant.astimezone(CHI) for cp in cps(60, 6)]  # spans 2027-03-14
    assert {(d.hour, d.minute) for d in local} == {(19, 0)}
    assert {d.utcoffset() for d in local} == {timedelta(hours=-6), timedelta(hours=-5)}


def test_reminders_kinds_offsets_and_grace():
    cp = cps(10, 1)[0]
    rs = schedule.reminders_for(cp)
    assert [r.kind for r in rs] == ["T-24h", "T-1h", "unlock"]
    assert [r.grace for r in rs] == [6 * 3600, 45 * 60, -1]
    assert rs[-1].due == cp.due.instant
    assert [cp.due.instant - r.due for r in rs[:2]] == [timedelta(hours=24), timedelta(hours=1)]
    assert schedule.GRACE["unlock"] < 0  # durable: infinite grace


def test_meeting_reminders_are_bounded_and_hold_local_wall_across_dst():
    # Two meetings at 19:00 local, one each side of the 2027-03-14 US transition.
    walls = [resolve_wall(d, time(19, 0), "America/Chicago")
             for d in (date(2027, 3, 7), date(2027, 3, 21))]
    for w in walls:
        rs = schedule.reminders_for_meeting(w)
        assert [r.kind for r in rs] == ["meeting-T-24h", "meeting-T-1h"]
        assert [r.grace for r in rs] == [6 * 3600, 45 * 60]  # perishable: bounded, never -1
        assert [w.instant - r.due for r in rs] == [timedelta(hours=24), timedelta(hours=1)]
    # Delegated to timefmt: same wall clock, different offset -- so not +7 days of seconds.
    local = [w.instant.astimezone(CHI) for w in walls]
    assert [(d.hour, d.utcoffset()) for d in local] == [
        (19, timedelta(hours=-6)), (19, timedelta(hours=-5))]


def test_pace_on_pace_boundary_and_zero_length_schedule():
    t0 = datetime(2027, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=10)
    assert schedule.pace(100, 50, t0, t1, t0 + timedelta(days=5)) == (50.0, 0)
    assert schedule.pace(100, 40, t0, t1, t0 + timedelta(days=5))[1] == -10
    assert schedule.pace(100, 10, t0, t0, t0) == (10.0, -90)  # no ZeroDivisionError
