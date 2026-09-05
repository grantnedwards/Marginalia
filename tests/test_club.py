"""State transitions only. No Discord client, no token, no HTTP."""

import inspect
import re
from datetime import date, time
from types import SimpleNamespace

import discord
import pytest

import marginalia.bot
from marginalia import cycle, library, schedule
from marginalia.cogs import _views as views
from marginalia.cogs import club, reading
from marginalia.db import Database
from tests.test_library import mkepub  # one synthetic EPUB, not a second copy of it

G, U1, U2 = 1545535072151670824, 1545535072151670825, 1545535072151670826
CH = 1545535072151670830  # the channel the cycle is opened in


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


async def nom(d, title, month="2026-09"):
    return await cycle.add_nomination(d, G, month, title, "An Author", 300, U1)


async def test_close_keeps_member_rows_and_strips_role(db):
    cid = await cycle.open_cycle(db, G, 7, await nom(db, "Ulysses"), cycle_month="2026-09")
    await cycle.join_cohort(db, cid, U1)
    await cycle.join_cohort(db, cid, U2)
    assert await cycle.close_cycle(db, cid) == [U1, U2]
    rows = await db.all("SELECT user_id, role_granted FROM cohort_members WHERE cohort_id=?", cid)
    assert [r["user_id"] for r in rows] == [U1, U2]  # history survives the role
    assert [r["role_granted"] for r in rows] == [0, 0]  # membership intent stripped
    assert (await db.one("SELECT status FROM cohorts WHERE id=?", cid))["status"] == "closed"


async def test_join_is_idempotent_and_rejoin_works(db):
    cid = await cycle.open_cycle(db, G, 7, await nom(db, "Ulysses"), cycle_month="2026-09")
    assert await cycle.join_cohort(db, cid, U1) is True
    assert await cycle.join_cohort(db, cid, U1) is False
    assert await cycle.roster_ids(db, cid) == [U1]
    await cycle.leave_cohort(db, cid, U1)
    assert await cycle.roster_ids(db, cid) == []
    assert await cycle.join_cohort(db, cid, U1) is True
    assert await cycle.roster_ids(db, cid) == [U1]


async def test_shortlist_excludes_a_book_from_an_ended_cohort(db):
    prev = await nom(db, "Dune", month="2026-08")
    await cycle.close_cycle(db, await cycle.open_cycle(db, G, 7, prev, cycle_month="2026-08"))
    keep = await nom(db, "Ulysses")
    await nom(db, "Dune")  # same book, new month, still 'proposed'
    assert [r["id"] for r in await cycle.shortlist(db, G, "2026-09")] == [keep]


async def test_cycle_opens_on_the_ingested_book_end_to_end(db, tmp_path):
    """The whole ebook half, in one line of assertions: ingest, open the cycle ON
    that book, plan in the DEFAULT pages mode, and the ceiling must not be 0."""
    bid = await library.ingest(db, str(mkepub(tmp_path)))  # 2 chapters, ~65% mark in ch. 2
    cid = await cycle.open_cycle(db, G, CH, await nom(db, "T"), cycle_month="2026-09",
                                book_id=bid)
    assert (await db.one("SELECT COUNT(*) FROM books"))[0] == 1  # ONE row, not two
    assert (await db.one("SELECT book_id FROM cohorts WHERE id=?", cid))["book_id"] == bid
    await cycle.join_cohort(db, cid, U1)
    chapters = (await db.one("SELECT chapter_count FROM books WHERE id=?", bid))[0]
    assert chapters == 2  # populated by ingest; 0 is what made every ceiling 0
    cps = schedule.plan(20, 2, 6, time(19, 0), "UTC", "pages", start=date(2026, 1, 4))
    await reading.apply_plan(db, cid, cps, "UTC", int(chapters))
    ceil = await library.ceiling(db, G, CH, U1, bid)
    assert ceil.chapter >= 1, "pages-mode ceiling floored to 0: the book is not attached"
    assert [h.text for h in await library.search(db, bid, "whale", ceil)] == ["the white whale"]


async def test_next_cycle_month_skips_months_that_already_have_a_cohort(db):
    this = cycle.this_month()
    assert await cycle.next_cycle_month(db, G) == this  # nothing yet: this month
    await cycle.open_cycle(db, G, CH, await nom(db, "A", this), cycle_month=this)
    following = cycle.month_after(this)
    assert await cycle.next_cycle_month(db, G) == following  # reading now -> pick for next
    assert cycle.month_after("2026-12") == "2027-01"
    await db.run("UPDATE cohorts SET status='closed', closed_at=unixepoch()")
    assert await cycle.next_cycle_month(db, G) == following  # closed early: still next


async def test_open_cycle_validates_before_anything_is_created(db):
    """check_open is the pre-flight /cycle-open runs before create_role."""
    for bad in ((None, None), (999, None), (None, 999)):
        with pytest.raises(ValueError):
            await cycle.check_open(db, *bad)
    assert (await db.one("SELECT COUNT(*) FROM cohorts"))[0] == 0


async def test_cycle_opens_on_a_bare_ingested_book(db, tmp_path):
    bid = await library.ingest(db, str(mkepub(tmp_path)))
    cid = await cycle.open_cycle(db, G, CH, None, cycle_month="2026-09", book_id=bid)
    assert (await db.one("SELECT book_id FROM cohorts WHERE id=?", cid))["book_id"] == bid
    assert (await db.one("SELECT COUNT(*) FROM nominations"))[0] == 0


async def test_attach_book_repoints_a_live_cycle_and_sweeps_the_stub(db, tmp_path):
    """Opening a cycle before the EPUB exists leaves a text-less stub, and quoting stays
    refused against it. Attaching is what turns quoting on without closing the month."""
    cid = await cycle.open_cycle(db, G, CH, await nom(db, "T"), cycle_month="2026-09")
    stub = (await db.one("SELECT book_id FROM cohorts WHERE id=?", cid))["book_id"]
    await cycle.join_cohort(db, cid, U1)
    assert await library.ceiling(db, G, CH, U1, stub) == library.REFUSED

    with pytest.raises(ValueError):  # a not-yet-ingested id is refused, not attached
        await cycle.attach_book(db, cid, stub)

    bid = await library.ingest(db, str(mkepub(tmp_path)))
    old, new = await cycle.attach_book(db, cid, bid)
    assert (await db.one("SELECT book_id FROM cohorts WHERE id=?", cid))["book_id"] == bid
    assert old == "T" and new
    # The stub is swept, so /purge_book and the autocomplete never offer a ghost.
    assert await db.one("SELECT 1 FROM books WHERE id=?", stub) is None
    assert (await db.one("SELECT COUNT(*) FROM books"))[0] == 1
    # Re-attaching the same book is a no-op, not a crash and not a second sweep.
    assert await cycle.attach_book(db, cid, bid) == (new, new)


async def test_attach_book_keeps_a_real_book_another_cohort_still_reads(db, tmp_path):
    bid1 = await library.ingest(db, str(mkepub(tmp_path, name="one.epub")))
    c1 = await cycle.open_cycle(db, G, CH, None, cycle_month="2026-08", book_id=bid1)
    await db.run("UPDATE cohorts SET status='closed', closed_at=unixepoch() WHERE id=?", c1)
    c2 = await cycle.open_cycle(db, G, CH, None, cycle_month="2026-09", book_id=bid1)
    bid2 = await library.ingest(db, str(mkepub(tmp_path, cover=b"\x89PNG", name="two.epub")))
    await cycle.attach_book(db, c2, bid2)
    assert await db.one("SELECT 1 FROM books WHERE id=?", bid1) is not None


async def test_latest_ballot_is_the_newest_poll_message(db):
    assert await cycle.latest_ballot(db, G) is None
    for title, mid in (("A", 100), ("B", 300), ("C", 200)):
        await db.run("UPDATE nominations SET poll_message_id=? WHERE id=?", mid,
                     await nom(db, title))
    assert await cycle.latest_ballot(db, G) == 300


async def test_cycle_without_an_epub_still_opens(db):
    """Plenty of clubs never upload a file: everything but quoting must work."""
    cid = await cycle.open_cycle(db, G, CH, await nom(db, "No File"), cycle_month="2026-10")
    assert await cycle.join_cohort(db, cid, U1) is True  # roster, role, reminders all fine
    bid = (await db.one("SELECT book_id FROM cohorts WHERE id=?", cid))["book_id"]
    assert await library.ceiling(db, G, CH, U1, bid) == library.REFUSED  # no text to quote
    with pytest.raises(ValueError):  # and a not-yet-ingested id is refused, not attached
        await cycle.open_cycle(db, G, CH, await nom(db, "Other"), cycle_month="2026-11",
                              book_id=bid)


def test_tie_is_reported_not_resolved():
    votes, leaders = cycle.tally({1: 5, 2: 5, 3: 1})
    assert leaders == [1, 2] and votes == 5   # the tie, not a winner
    assert cycle.tally({1: 5, 2: 4}) == (5, [1])


def test_poll_limits_are_checked_before_any_send():
    assert cycle.build_poll("What next?", ["a", "b"]).multiple is True
    for bad in (["a"] * 11, ["y" * 56], []):
        with pytest.raises(ValueError):
            cycle.build_poll("What next?", bad)
    with pytest.raises(ValueError):
        cycle.build_poll("x" * 301, ["a"])
    assert len(cycle.ballot_label("T" * 60, "An Author")) == cycle.POLL_MAX_ANSWER


def test_dynamic_items_exports_classes_not_instances():
    assert isinstance(club.DYNAMIC_ITEMS, tuple) and club.DYNAMIC_ITEMS
    for item in club.DYNAMIC_ITEMS:
        # add_dynamic_items() takes class types; an instance is a TypeError.
        assert isinstance(item, type) and issubclass(item, discord.ui.DynamicItem)


def test_join_button_custom_id_round_trips_through_its_template():
    cohort_id = 1545535072151670827
    button = club.JoinButton(cohort_id)
    # ViewStore matches with fullmatch(), so a merely-partial match is dead too.
    match = button.template.fullmatch(button.custom_id)
    assert match is not None, f"{button.custom_id} does not match {button.template.pattern}"
    assert int(match["cohort"]) == cohort_id


# --- views: built from plain data, asserted with no client ------------------

NOW = 1774000000


def _status(**over):
    """A HEALTHY /status, so each colour test overrides exactly one thing."""
    kw = dict(cohort={"cycle_month": "2026-09", "status": "open"}, now=NOW, members=3,
              book={"title": "Moby-Dick", "ingested_at": NOW, "chapter_count": 2, "paras": 40},
              sched={"n": 4, "fired": 1, "nxt": NOW + 3600},
              rem={"pending": 3, "sent": 1}, beat={"beat_at": NOW - 5})
    return views.status_embed(**(kw | over))


def _field(embed, name):
    return next(f.value for f in embed.fields if f.name == name)


@pytest.mark.parametrize("over,colour,field,needle", [
    # a failed reminder: the one thing an organizer most needs to see. Red is useless
    # without the count, and a live process with a dead scheduler is watchdog.sh's failure.
    ({"rem": {"sent": 1, "failed": 1}}, "red", "Reminders", "failed **1**"),
    ({"beat": {"beat_at": NOW - views.STALE_BEAT - 1}}, "red", "Heartbeat", "STALE"),
    ({"beat": None}, "red", None, None),                        # poller never ticked
    ({"rem": {"sent": 1, "skipped": 1}}, "yellow", None, None),
    ({}, "green", None, None),                                  # nothing wrong
])
def test_status_colour_is_worst_state_wins(over, colour, field, needle):
    e = _status(**over)
    assert e.colour == getattr(discord.Colour, colour)()
    if field:
        assert needle in _field(e, field)


@pytest.mark.parametrize("count", [0, 1, 40])
def test_roster_embed_stays_inside_every_discord_limit(count):
    e = views.roster_embed("2026-09", [1545535072151670824 + i for i in range(count)])
    assert len(e.fields) <= 25
    assert all(len(f.value) <= 1024 for f in e.fields), "a field value blew the 1024 cap"
    assert len(e) <= 6000
    shown = sum(f.value.count("<@") for f in e.fields)
    tail = int(re.search(r"\+(\d+) more", e.footer.text or "+0 more").group(1))
    assert shown + tail == count, "a member vanished with no count to say so"


def test_a_card_with_no_cover_is_still_a_deliberate_embed():
    """A cover is None for most books early on, so no-thumbnail is the NORMAL case."""
    for e in (views.join_embed("2026-09", "Ulysses", "Joyce", 0),
              views.nomination_embed(7, "Ulysses", "", "", U1)):
        assert e.thumbnail.url is None and e.title and e.description
        assert len(e) <= 6000
    cover = views.cover_file((b"\x89PNG", "image/png"))  # name derived from the media type
    assert (views.join_embed("2026-09", "Ulysses", "Joyce", 0, cover).thumbnail.url
            == "attachment://cover.png")


def test_ballot_result_embed_presents_a_tie_as_a_tie():
    tie = views.ballot_embed(5, ["Ulysses", "Dune"], [("Ulysses", 5), ("Dune", 5)])
    assert "Tied at 5" in tie.title and tie.footer.text is None  # nothing to open yet
    assert "Ulysses" in _field(tie, "Approvals") and "Dune" in _field(tie, "Approvals")
    win = views.ballot_embed(5, ["Ulysses"], [("Ulysses", 5), ("Dune", 4)], open_id=7)
    assert win.title == "Ulysses wins" and "#7" in win.footer.text
    e = views.help_embed()
    assert len(e) <= 6000 and all(len(f.value) <= 1024 for f in e.fields)
    for cmd in ("/join", "/next", "/quote", "/cycle-open", "/schedule", "/status"):
        assert any(cmd in f.value for f in e.fields), f"/help does not mention {cmd}"


async def test_status_reads_the_real_reminder_queue_and_heartbeat(db):
    """The queries, not just the view: a typo in the cohort-scoped SQL only shows here."""
    cid = await cycle.open_cycle(db, G, CH, await nom(db, "Ulysses"), cycle_month="2026-09")
    await cycle.join_cohort(db, cid, U1)
    for idx, (due, status) in enumerate(((100, "sent"), (200, "failed"), (9999, "pending")), 1):
        cur = await db.run("INSERT INTO checkpoints (cohort_id, idx, label, unit, start_ref,"
                           " end_ref, chapter_ceiling, due_at_utc, tz_id, local_wall) VALUES"
                           " (?,?,?,'chapter',1,1,1,?,'UTC','2026-09-06 19:00')",
                           cid, idx, f"cp{idx}", due)
        await db.run("INSERT INTO reminders (checkpoint_id, kind, due_at, status)"
                     " VALUES (?,'unlock',?,?)", cur.lastrowid, due, status)
    cog = club.Club(SimpleNamespace(db=db, cfg=None))
    cohort = await cycle.cohort_of(db, G)
    rows = await cog._status_rows(cohort, 1000)
    assert (rows["sched"]["n"], rows["sched"]["fired"], rows["sched"]["nxt"]) == (3, 2, 9999)
    assert rows["rem"] == {"sent": 1, "failed": 1, "pending": 1}
    assert rows["members"] == 1
    e = views.status_embed(cohort=cohort, now=1000, beat={"beat_at": 1000}, **rows)
    assert e.colour == discord.Colour.red()  # the failed reminder, at one glance
    assert "No EPUB ingested" in _field(e, "Book text")  # no file: everything else still works

    stub = _Stub()
    await cog.status.callback(cog, stub)
    content, kw = stub.sent[-1]
    assert kw["ephemeral"] is True, "/status is organizer-only and must not go to the channel"
    assert content is None and "file" not in kw  # an absent kwarg, never file=None
    assert kw["embed"].colour == discord.Colour.red()  # no heartbeat row at all is red too
    assert club.Club.status.default_permissions is not None, "/status lost its organizer gate"


class _Stub:
    """The Interaction /status needs: a guild id and something that records the reply."""

    guild_id = G

    def __init__(self) -> None:
        self.response = self
        self.sent: list[tuple[str | None, dict]] = []

    async def send_message(self, content=None, **kw) -> None:
        self.sent.append((content, kw))


def test_bot_setup_hook_actually_collects_the_export():
    """An export nobody collects is the same as no export, so assert bot.py's own
    getattr names -- a rename on either side fails here."""
    names = re.findall(r'getattr\(mod, "(\w+)", \(\)\)', inspect.getsource(marginalia.bot))
    assert "DYNAMIC_ITEMS" in names, f"bot.py no longer collects DYNAMIC_ITEMS: {names}"
    collected = tuple(x for n in names for x in getattr(club, n, ()))
    assert club.JoinButton in collected, "JoinButton unregistered; the button dies on restart"
