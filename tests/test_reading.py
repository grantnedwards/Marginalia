"""Reading-cycle state transitions, and the member-facing views. No client, no token.

The view half tests the BUILDERS in cogs/_views.py -- data in, discord.Embed out -- plus
what the cog hands ``send_message``. Discord itself is never called.
"""

from datetime import UTC, date, datetime, time
from pathlib import Path

import discord
import pytest

from marginalia import progress, schedule
from marginalia.cogs import _views as v
from marginalia.cogs import reading as r
from marginalia.db import Database
from marginalia.timefmt import local_wall, unix, when

G, U = 1545535072151670824, 1545535072151670826
PLAN = dict(weekday=6, at=time(19, 0), tz="America/Chicago", by="chapters", start=date(2026, 3, 1))
CP = {"label": "Chapter 5", "due_at_utc": 1_772_060_400, "start_ref": 5, "end_ref": 5,
      "unit": "chapter", "dst_note": ""}  # a row is only read by key, so a dict is one


class Rec:
    """An Interaction that records the reply instead of sending it; ``.sent`` is the kwargs.
    The same trick as tests/test_ephemeral.py's Recorder, kept local so that file (another
    agent's) stays untouched."""

    def __init__(self) -> None:
        self.guild_id, self.id = G, U
        self.user = self.response = self
        self.sent: list[dict] = []

    async def send_message(self, content: str | None = None, **kw) -> None:
        self.sent.append({"content": content, **kw})

    async def edit_message(self, **kw) -> None:
        self.sent.append(kw)

    def last(self) -> dict:
        assert self.sent, "the command replied to nobody"
        return self.sent[-1]


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.migrate()
    await d.run("INSERT INTO books (id, title, chapter_count) VALUES (1,'Moby-Dick',34)")
    await d.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, tz_id)"
                " VALUES (1,?,2,1,'2026-03','America/Chicago')", G)
    await d.run("INSERT INTO cohort_members (cohort_id, user_id) VALUES (1,?)", U)
    yield d
    await d.close()


async def test_plan_writes_checkpoints_and_reminder_fanout(db):
    cps = schedule.plan(34, 8, **PLAN)
    assert await progress.apply_plan(db, 1, cps, "America/Chicago", 34) == (8, 24)
    kinds = [x[0] for x in await db.all(
        "SELECT kind FROM reminders rem JOIN checkpoints c ON c.id = rem.checkpoint_id"
        " WHERE c.idx = 1 ORDER BY due_at")]
    assert kinds == [x.kind for x in schedule.reminders_for(cps[0])] == ["T-24h", "T-1h", "unlock"]
    assert (await db.one("SELECT chapter_ceiling FROM checkpoints WHERE idx = 8"))[0] == 34


async def test_reschedule_drops_pending_keeps_sent(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    await db.run("UPDATE reminders SET status = 'sent' WHERE kind = 'unlock'")
    _, new = await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    assert new == 16  # the 8 sent unlocks were not re-created ...
    rows = await db.all("SELECT status, COUNT(*) FROM reminders GROUP BY status ORDER BY 1")
    assert [tuple(x) for x in rows] == [("pending", 16), ("sent", 8)]  # ... nor duplicated


async def test_shorter_replan_sweeps_only_this_cohorts_surplus_weeks(db):
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, tz_id)"
                 " VALUES (2,?,3,1,'2026-04','America/Chicago')", G)
    await progress.set_meeting(db, 1, date(2026, 4, 12), time(19, 0), "America/Chicago")
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    await progress.apply_plan(db, 2, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)

    async def create(cp):
        return 906

    await progress.unlock(db, (await db.one("SELECT id FROM checkpoints WHERE cohort_id = 1"
                                     " AND idx = 6"))[0], create)
    await progress.apply_plan(db, 1, schedule.plan(34, 4, **PLAN), "America/Chicago", 34)
    idxs = [x[0] for x in await db.all("SELECT idx FROM checkpoints WHERE cohort_id = 1"
                                       " ORDER BY idx")]
    assert idxs == [0, 1, 2, 3, 4]  # weeks 5-8 swept; idx 0, the meeting anchor, survives
    assert (await db.one("SELECT COUNT(*) FROM checkpoints WHERE cohort_id = 2"))[0] == 8
    # DOCUMENTED, not desired: the sweep's CASCADE unmaps week 6's still-live thread, so
    # quoting there fails closed with nothing said. Safe, silent; see SPEC's reschedule note.
    assert await db.one("SELECT 1 FROM channel_policy WHERE channel_id = 906") is None


async def test_next_checkpoint_breaks_tie_on_idx(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    due = (await db.one("SELECT due_at_utc FROM checkpoints WHERE idx = 3"))[0]
    await db.run("UPDATE checkpoints SET due_at_utc = ? WHERE idx = 2", due)
    assert (await progress.next_checkpoint(db, 1, due - 1))["idx"] == 2
    assert await progress.next_checkpoint(db, 1, 1 << 40) is None


async def test_unlock_is_idempotent(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    calls = []

    async def create(cp):
        calls.append(cp["idx"])
        return 999

    cp_id = (await db.one("SELECT id FROM checkpoints WHERE idx = 1"))[0]
    assert (await progress.unlock(db, cp_id, create) == 999
            == await progress.unlock(db, cp_id, create))
    assert calls == [1]
    assert (await db.one("SELECT COUNT(*) FROM channel_policy"))[0] == 1
    pol = await db.one("SELECT * FROM channel_policy WHERE channel_id = 999")
    assert (pol["kind"], pol["max_chapter"]) == ("chapter_thread", 5)


def test_illegal_auto_archive_duration_rejected():
    assert progress.thread_kwargs("x")["type"].name == "public_thread"
    with pytest.raises(ValueError):
        progress.thread_kwargs("x", 2880)


async def test_progress_accepts_a_lower_number(db):
    await progress.set_progress(db, 1, U, 12, "chapter")
    await progress.set_progress(db, 1, U, 3, "chapter")  # re-reading, or fixing a typo
    assert await progress.progress_of(db, 1, U, "chapter") == 3  # latest wins, never clamped
    # The read side keeps BOTH reports, newest first: the lower one is history, not a fix.
    rows, page, pages = await progress.history_page(db, 1, U, "chapter")
    assert [x["n"] for x in rows] == [3, 12] and (page, pages) == (1, 1)
    assert await progress.mark_dnf(db, 1, U, "lost the thread") == (1, 1)


@pytest.mark.parametrize("n,pages,last", [(0, 1, 0), (5, 1, 5), (6, 2, 1)])
async def test_progress_history_pagination_boundaries(db, n, pages, last):
    for i in range(n):
        await progress.set_progress(db, 1, U, i + 1, "chapter")
    assert (await progress.history_page(db, 1, U, "chapter", 1))[1:] == (1, pages)
    assert len((await progress.history_page(db, 1, U, "chapter", 99))[0]) == last  # 99 clamps


async def test_meeting_anchor_and_its_two_reminders_persist(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, tz_id)"
                 " VALUES (2,?,3,1,'2026-04','America/Chicago')", G)
    await progress.apply_plan(db, 2, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    w, rem = await progress.set_meeting(db, 1, date(2026, 4, 12), time(19, 0), "America/Chicago")
    assert rem == 2
    # The purge is THIS anchor's own pending rows: both cohorts' 24 weekly reminders live.
    assert (await db.one("SELECT COUNT(*) FROM reminders WHERE status = 'pending'"))[0] == 50
    cp = await db.one("SELECT * FROM checkpoints WHERE is_meeting_anchor = 1")
    assert (cp["idx"], cp["due_at_utc"], cp["chapter_ceiling"]) == (0, unix(w.instant), 0)
    rows = await db.all("SELECT kind, due_at, grace_secs FROM reminders WHERE checkpoint_id = ?"
                        " ORDER BY due_at", cp["id"])
    assert [tuple(x) for x in rows] == [
        ("meeting-T-24h", unix(w.instant) - 24 * 3600, 6 * 3600),
        ("meeting-T-1h", unix(w.instant) - 3600, 45 * 60)]
    # The anchor is not a week: a re-plan cannot sweep it, and it never widens pace.
    await progress.apply_plan(db, 1, schedule.plan(34, 4, **PLAN), "America/Chicago", 34)
    assert (await db.one("SELECT COUNT(*) FROM checkpoints WHERE is_meeting_anchor = 1"))[0] == 1
    assert (await progress.pace_of(db, 1, U, unix(w.instant)))[2] == "chapter"
    still = await db.all("SELECT kind FROM reminders WHERE checkpoint_id = ?"
                         " AND status = 'pending' ORDER BY kind", cp["id"])
    assert [x[0] for x in still] == ["meeting-T-1h", "meeting-T-24h"]


async def test_no_plan_yields_no_unit_to_guess(db):
    # /progress set before /schedule used to write chapter_index, which then gated the
    # member for the whole cycle of a pages plan and never showed up in /progress show.
    assert await progress.unit_of(db, 1) is None
    await progress.apply_plan(db, 1, schedule.plan(400, 4, **{**PLAN, "by": "pages"}),
                       "America/Chicago", 34)
    assert await progress.unit_of(db, 1) == "page"


async def test_pages_plan_reads_its_unit_past_the_meeting_anchor(db):
    pages = {**PLAN, "by": "pages"}
    w, _ = await progress.set_meeting(db, 1, date(2026, 4, 12), time(19, 0), "America/Chicago")
    await progress.apply_plan(db, 1, schedule.plan(400, 4, **pages), "America/Chicago", 34)
    # MEASURED: unfiltered, MIN(unit) is the anchor's 'chapter' (SQLite MIN is
    # alphabetical) and the anchor is row 1, so both reads pick up the wrong column.
    assert await progress.unit_of(db, 1) == "page"
    assert (await progress.pace_of(db, 1, U, unix(w.instant)))[2] == "page"


async def test_replan_resyncs_a_thread_pinned_before_the_book_was_ingested(db):
    cps = schedule.plan(400, 4, **{**PLAN, "by": "pages"})
    # No chapter_count yet: every ceiling floors to 0.
    await progress.apply_plan(db, 1, cps, "America/Chicago", 0)

    async def create(cp):
        return 999

    cp_id = (await db.one("SELECT id FROM checkpoints WHERE idx = 1"))[0]
    await progress.unlock(db, cp_id, create)
    pinned = "SELECT max_chapter FROM channel_policy WHERE channel_id = 999"
    assert (await db.one(pinned))[0] == 0  # quoting refused in the thread, forever
    await progress.apply_plan(db, 1, cps, "America/Chicago", 34)
    assert (await db.one(pinned))[0] == 8 == (
        await db.one("SELECT chapter_ceiling FROM checkpoints WHERE id = ?", cp_id))[0]


@pytest.mark.parametrize("n,pages,last", [(0, 1, 0), (5, 1, 5), (6, 2, 1)])
async def test_library_pagination_boundaries(db, n, pages, last):
    await db.run("DELETE FROM cohorts")
    for i in range(n):
        await db.run("INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month)"
                     " VALUES (?,2,1,?)", G, f"2026-{i + 1:02d}")
    assert (await progress.library_page(db, G, 1))[1:] == (1, pages)
    assert len((await progress.library_page(db, G, 99))[0]) == last  # 99 clamps to the last page


# ------------------------------------------------------------- member-facing views

async def _cog(db) -> r.Reading:
    return r.Reading(None, db)  # the cog only ever touches .db


@pytest.mark.parametrize("pct,filled,text", [
    (0, 0, "0%"), (30, 2, "30%"), (50, 4, "50%"), (100, 8, "100%"),
    (-4, 0, "0%"), (140, 8, "100%")])  # pace() may hand back either side of the range
def test_progress_bar_fill_and_clamp(pct, filled, text):
    got = v.bar(pct)
    cells, _, pct_text = got.partition(" ")
    # Hand-computed counts, NOT a re-run of the formula: breaking the arithmetic fails here.
    assert cells == v.FULL * filled + v.EMPTY * (v.CELLS - filled)
    assert pct_text == text
    assert len(cells) == v.CELLS  # never overflows or shrinks the bar, at either extreme


async def test_next_embed_fields_carry_due_and_how_much_to_read(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    cp = await db.one("SELECT * FROM checkpoints WHERE idx = 1")
    e = v.next_embed(cp, "2026-03")
    assert e.title == cp["label"]
    assert [(f.name, f.value) for f in e.fields] == [
        ("Due", when(int(cp["due_at_utc"]))),  # invariant 1: the markup is timefmt's
        ("Read to", "chapter 5")]              # a chapters plan reads as one number
    assert e.footer.text == "2026-03"
    assert e.thumbnail.url is None and len(e) < 6000  # no cover: still a valid embed


async def test_next_embed_reads_a_pages_plan_as_a_range(db):
    await progress.apply_plan(db, 1, schedule.plan(400, 4, **{**PLAN, "by": "pages"}),
                       "America/Chicago", 34)
    cp = await db.one("SELECT * FROM checkpoints WHERE idx = 2")
    assert v.next_embed(cp).fields[1].value == f"pages {cp['start_ref']}-{cp['end_ref']}"


def test_a_book_with_no_cover_still_builds_the_embed():
    assert v.cover_file(None) is None
    plain = v.next_embed(CP)
    assert plain.thumbnail.url is None and plain.fields[0].name == "Due"


@pytest.mark.parametrize("mime,name", [("image/jpeg", "cover.jpg"), ("image/png", "cover.png")])
def test_cover_thumbnail_name_matches_the_attachment(mime, name):
    f = v.cover_file((b"\x00binary", mime))
    assert f.filename == name
    assert v.next_embed(CP, cover=f).thumbnail.url == f"attachment://{name}"


def test_link_button_needs_no_custom_id_and_no_handler():
    view = v.thread_view(G, 906)
    (item,) = view.children
    assert item.style is discord.ButtonStyle.link
    assert item.url == f"https://discord.com/channels/{G}/906"
    assert item.custom_id is None  # nothing to register, nothing to route, nothing to break
    assert v.thread_view(G, None) is None


async def test_next_offers_the_thread_as_a_link_button(db):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    await db.run("UPDATE checkpoints SET due_at_utc = unixepoch() + 86400, thread_id = 906"
                 " WHERE idx = 1")
    cog, inter = await _cog(db), Rec()
    await cog.next.callback(cog, inter)
    kw = inter.last()
    assert kw["ephemeral"] is True and kw["content"] is None
    assert kw["embed"].fields[0].name == "Due"
    assert kw["view"].children[0].url.endswith("/906")
    assert "file" not in kw  # no cover on this book, so no attachment is sent at all

    # cover ships in migration 2, which the fixture now runs: /next reads a real blob and
    # attaches it, with the filename derived from the stored media type.
    await db.run("UPDATE books SET cover = ?, cover_mime = 'image/png' WHERE id = 1", b"\x89PNG")
    inter = Rec()
    await cog.next.callback(cog, inter)
    kw = inter.last()
    assert kw["file"].filename == "cover.png"
    assert kw["embed"].thumbnail.url == "attachment://cover.png"


@pytest.mark.parametrize("delta,gap", [
    (0, "right on the plan"), (3, "3 chapters ahead"), (-2, "2 chapters from the plan")])
def test_pace_embed_pairs_the_bar_with_the_gap(delta, gap):
    e = v.pace_embed(52.0, delta, "chapter")
    assert e.description == v.bar(52.0)
    assert (e.fields[0].name, e.fields[0].value) == ("Versus the plan", gap)
    assert "behind" not in e.fields[0].value  # distance from the plan, never a reprimand


def test_mystats_embed_is_personal_only():
    e = v.mystats_embed("page", 42, 3, 1)
    assert [(f.name, f.value) for f in e.fields] == [
        ("This book", "page 42"), ("Books joined", "3"), ("Set down", "1")]
    assert e.footer.text == "Only you can see this."


@pytest.mark.parametrize("dnf,roster,value", [
    (0, 0, "0% of 0 here did not finish it"), (5, 8, "62% of 8 here did not finish it"),
    (1, 1, "100% of 1 here did not finish it")])
def test_dnf_embed_shows_the_aggregate_and_never_a_member(dnf, roster, value):
    e = v.dnf_embed(dnf, roster)  # roster 0 must not divide by zero
    assert e.fields[0].value == value
    blob = f"{e.title} {e.fields[0].value} {e.footer.text}"
    assert str(U) not in blob and "@" not in blob


def test_page_embed_stays_inside_the_description_limit():
    e = v.page_embed("t", ["x" * 900] * 6, 2, 9, "empty")
    assert len(e.description) == v.DESC_MAX  # truncated to the ceiling, never over it
    assert e.footer.text == "page 2/9"
    assert v.page_embed("t", [], 1, 1, "Nothing read yet.").description == "Nothing read yet."


async def _shelve(db, n: int) -> None:
    await db.run("DELETE FROM cohorts")
    for i in range(n):
        await db.run("INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month)"
                     " VALUES (?,2,1,?)", G, f"2026-{i + 1:02d}")


@pytest.mark.parametrize("n,page,rows,foot,view", [
    (0, 1, 0, "page 1/1", False),   # empty
    (5, 1, 5, "page 1/1", False),   # exactly one page: no dead buttons
    (6, 1, 5, "page 1/2", True),    # one over
    (6, 99, 1, "page 2/2", True)])  # last page, out-of-range clamps
async def test_library_pages_with_buttons_only_when_there_is_a_second_page(
        db, n, page, rows, foot, view):
    await _shelve(db, n)
    cog, inter = await _cog(db), Rec()
    await cog.library.callback(cog, inter, page=page)
    kw = inter.last()
    assert kw["ephemeral"] is True
    assert kw["embed"].footer.text == foot
    body = kw["embed"].description
    assert (len(body.splitlines()) if n else 0) == rows
    assert ("view" in kw) is view
    if view:
        pager = kw["view"]
        assert (pager.prev.disabled, pager.next.disabled) == (page == 1, page > 1)
        assert pager.timeout and not pager.is_persistent()  # non-persistent BY DESIGN


async def test_progress_show_paginates_and_the_button_edits_the_same_message(db):
    for i in range(6):
        await progress.set_progress(db, 1, U, i + 1, "chapter")
    cog, inter = await _cog(db), Rec()
    await cog.progress_show.callback(cog, inter)
    first = inter.last()
    assert first["ephemeral"] is True and first["embed"].footer.text == "page 1/2"
    assert first["embed"].description.startswith("chapter 6 - ")  # newest first

    press = Rec()
    await first["view"].next.callback(press)  # the pressed button re-renders in place
    second = press.last()
    assert second["embed"].footer.text == "page 2/2"
    assert (second["view"].prev.disabled, second["view"].next.disabled) == (False, True)


@pytest.mark.parametrize("name", ["next", "pace", "progress_show", "mystats", "dnf", "library"])
async def test_member_views_stay_ephemeral(db, name):
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    await db.run("UPDATE checkpoints SET due_at_utc = unixepoch() + 86400 WHERE idx = 1")
    await progress.set_progress(db, 1, U, 3, "chapter")
    cog, inter = await _cog(db), Rec()
    await getattr(cog, name).callback(cog, inter)
    assert inter.last()["ephemeral"] is True, f"/{name} is personal or self-serve: keep it private"


async def test_a_one_line_confirmation_stays_a_string(db):
    """/progress set is already perfect at one line; a titled embed reads slower."""
    await progress.apply_plan(db, 1, schedule.plan(34, 8, **PLAN), "America/Chicago", 34)
    cog, inter = await _cog(db), Rec()
    await cog.progress_set.callback(cog, inter, number=42)
    assert inter.last() == {"content": "Noted: chapter 42.", "ephemeral": True}


def test_wall_clock_formatting_stays_in_timefmt():
    """Date formatting is timefmt's job: reading.py must call local_wall(), not strftime."""
    assert local_wall(datetime(2026, 3, 8, 12, tzinfo=UTC), "America/Chicago") == "2026-03-08 07:00"
    src = Path(r.__file__).read_text()
    assert "strftime" not in src, "format the wall clock via timefmt.local_wall()"
