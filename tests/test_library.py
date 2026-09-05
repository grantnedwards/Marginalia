"""Synthetic 34-chapter book inserted directly -- no EPUB fixture dependency."""

import zipfile

import aiosqlite
import pytest

from marginalia import db as dbmod
from marginalia import epub
from marginalia import library as lib
from marginalia.db import Database

G, C, U = 1545535072151670824, 1545535072151670825, 1545535072151670826
PHRASE = "leviathanic corposant"  # only in chapter 20, para 3
W = 10  # words per paragraph, so the 2% allowance is exactly countable
OPEN = lib.Ceiling(34, "test")


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.migrate()
    await d.run("INSERT INTO books (id, title, author, chapter_count, word_count, char_count,"
                " source_sha256, ingested_at) VALUES (1,'Moby-Dick','Herman Melville',34,?,?,"
                "'sha',unixepoch())", 34 * 5 * W, 34 * 5 * 60)
    # status: only a LIVE cohort (club.LIVE) scopes a ceiling; the default is 'draft'.
    await d.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, status)"
                " VALUES (1,?,?,1,'2026-09','open')", G, C)
    await d.run("INSERT INTO cohort_members (cohort_id, user_id) VALUES (1,?)", U)
    await d.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id) VALUES (?,?,1)", C, G)
    await d.run("INSERT INTO checkpoints (id, cohort_id, idx, label, start_ref, end_ref,"
                " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                " VALUES (1,1,1,'a',1,5,5,0,'UTC','2026-09-06 19:00')")
    async with d.tx() as conn:
        for ci in range(1, 35):
            await conn.execute("INSERT INTO chapters (id, book_id, chapter_index, title,"
                               " para_count) VALUES (?,1,?,?,5)", (ci, ci, f"Chapter {ci}"))
            for pi in range(1, 6):
                head = PHRASE if (ci, pi) == (20, 3) else "whale"
                text = " ".join([head] + [f"w{ci}x{pi}"] * (W - len(head.split())))
                await conn.execute(
                    "INSERT INTO paragraphs (book_id, chapter_id, chapter_index, para_index,"
                    " text, word_count, char_start, char_count) VALUES (1,?,?,?,?,?,?,?)",
                    (ci, ci, pi, text, W, ((ci - 1) * 5 + pi - 1) * 60, len(text)))
    yield d
    await d.close()


async def find(db):
    return await lib.search(db, 1, PHRASE, await lib.ceiling(db, G, C, U, 1))


async def test_ceiling_hides_and_reveals(db):
    """THE test. Without the positive half an always-empty bug looks like a gate."""
    assert (await lib.ceiling(db, G, C, U, 1)).chapter == 5
    assert await find(db) == []  # chapter 20 is above the ceiling
    await db.run("UPDATE checkpoints SET chapter_ceiling = 34")
    hits = await find(db)
    assert [(h.chapter, h.para) for h in hits] == [(20, 3)]
    # EXACT verbatim paragraph text: this is what makes quoting hallucination-proof.
    assert hits[0].text == (await db.one(
        "SELECT text FROM paragraphs WHERE chapter_index=20 AND para_index=3"))[0]
    assert "**leviathanic**" in hits[0].snippet and 0 < hits[0].pct < 100
    # A reader who has reported less than the cohort is capped at their own place.
    await db.run("INSERT INTO member_progress (cohort_id, user_id, chapter_index)"
                 " VALUES (1,?,3)", U)
    assert (await lib.ceiling(db, G, C, U, 1)).chapter == 3
    assert await find(db) == []


@pytest.mark.parametrize("sql", [
    "DELETE FROM cohort_members",                  # unknown user
    # unmapped channel: no policy row AND no live cohort sitting in this channel
    "DELETE FROM channel_policy; UPDATE cohorts SET channel_id = 0",
    "UPDATE channel_policy SET cohort_id = NULL",  # no cohort
    "UPDATE cohorts SET status = 'closed'",        # no LIVE cohort
    "UPDATE books SET ingested_at = NULL",         # book not ingested
    "UPDATE checkpoints SET chapter_ceiling = -1",  # negative ceiling
    "UPDATE channel_policy SET kind = 'denied'",   # explicit denial
])
async def test_fail_closed_paths_all_give_chapter_zero(db, sql):
    await db.run("UPDATE checkpoints SET chapter_ceiling = 34")
    for stmt in sql.split(";"):
        await db.run(stmt)
    assert await lib.ceiling(db, G, C, U, 1) == lib.REFUSED
    assert await find(db) == []
    assert await lib.ceiling(db, G, C, U, 999) == lib.REFUSED  # missing book


async def test_ceiling_is_scoped_to_book_live_cohort_and_due_checkpoints(db):
    """The thread pin scopes ONE live cohort's book, and only DUE checkpoints raise it."""
    T = C + 1  # a chapter-5 discussion thread of the fixture's cohort
    await db.run("INSERT INTO books (id, title, author, chapter_count, word_count,"
                 " char_count, source_sha256, ingested_at)"
                 " VALUES (2,'Next Month','A',34,340,600,'sha2',unixepoch())")
    await db.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id, kind,"
                 " max_chapter) VALUES (?,?,1,'chapter_thread',5)", T, G)
    # Positive control, or a gate that refuses everything would look like a fix.
    assert await lib.ceiling(db, G, T, U, 1) == lib.Ceiling(5, "thread_pin")
    # A book this thread's cohort is NOT reading is not pinned, it is refused.
    assert await lib.ceiling(db, G, T, U, 2) == lib.REFUSED
    assert await lib.search(db, 2, "whale", await lib.ceiling(db, G, T, U, 2)) == []
    # A cohort that is not live scopes nothing, in the thread or the club channel.
    await db.run("UPDATE cohorts SET status = 'closed'")
    assert await lib.ceiling(db, G, T, U, 1) == lib.REFUSED
    assert await lib.ceiling(db, G, C, U, 1) == lib.REFUSED
    await db.run("UPDATE cohorts SET status = 'active'")
    # A checkpoint due in the FUTURE must not raise the ceiling: apply_plan writes
    # every ceiling up to 34 on day one, so this predicate is the whole unlock.
    await db.run("INSERT INTO checkpoints (id, cohort_id, idx, label, start_ref, end_ref,"
                 " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                 " VALUES (2,1,2,'b',6,34,34,unixepoch() + 86400,'UTC','2026-10-06 19:00')")
    assert (await lib.ceiling(db, G, C, U, 1)).chapter == 5
    assert await find(db) == []  # chapter 20 stays hidden until that checkpoint is due
    # Positive control: matching book + live cohort + DUE checkpoint serves.
    await db.run("UPDATE checkpoints SET due_at_utc = unixepoch() - 1 WHERE id = 2")
    assert await lib.ceiling(db, G, C, U, 1) == lib.Ceiling(34, "cohort_checkpoint")
    assert [(h.chapter, h.para) for h in await find(db)] == [(20, 3)]
    # No policy row in the cohort's own channel means default club policy, not
    # refusal -- nothing in the codebase ever writes a kind='club' row.
    await db.run("DELETE FROM channel_policy WHERE channel_id = ?", C)
    assert await lib.ceiling(db, G, C, U, 1) == lib.Ceiling(34, "cohort_checkpoint")
    assert await lib.ceiling(db, G, C + 999, U, 1) == lib.REFUSED  # still unmapped


async def test_thread_pin_does_not_outrank_membership(db):
    """A chapter thread is PUBLIC, so the pin alone would serve non-members."""
    T = C + 1
    await db.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id, kind,"
                 " max_chapter) VALUES (?,?,1,'chapter_thread',5)", T, G)
    # Positive control first: the member who joined does get chapter 1-5 text here.
    assert await lib.ceiling(db, G, T, U, 1) == lib.Ceiling(5, "thread_pin")
    assert len(await lib.search(db, 1, "whale", await lib.ceiling(db, G, T, U, 1))) == 3
    outsider = U + 500  # in the guild, in the thread, never joined the cohort
    assert await lib.ceiling(db, G, T, outsider, 1) == lib.REFUSED
    assert await lib.search(db, 1, "whale", await lib.ceiling(db, G, T, outsider, 1)) == []
    await db.run("UPDATE cohort_members SET left_at = unixepoch()")
    assert await lib.ceiling(db, G, T, U, 1) == lib.REFUSED  # and one who left


async def test_thread_pin_cannot_exceed_what_is_actually_due(db):
    """A re-plan re-syncs an OPEN thread's pin from its checkpoint, so the pin is no
    longer a snapshot of a checkpoint that fired: the read has to clamp it to the due set.
    Nothing renames the thread, so its name still advertises the old range."""
    T = C + 1
    await db.run("INSERT INTO member_progress (cohort_id, user_id, chapter_index)"
                 " VALUES (1,?,3)", U)
    await db.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id,"
                 " checkpoint_id, kind, max_chapter)"
                 " VALUES (?,?,1,1,'chapter_thread',5)", T, G)
    # Control: checkpoint 1 has fired, so its pin applies -- and the cohort branch below
    # still min()s the member's own progress, which the pin deliberately does not.
    assert await lib.ceiling(db, G, T, U, 1) == lib.Ceiling(5, "thread_pin")
    assert (await lib.ceiling(db, G, C, U, 1)).chapter == 3
    # /schedule re-run with `start` BLANK: every new due date lands in the future, and
    # apply_plan re-syncs this still-open "Chapters 1-5" thread's pin from 5 to 17.
    await db.run("UPDATE checkpoints SET end_ref = 17, chapter_ceiling = 17,"
                 " due_at_utc = unixepoch() + 86400 WHERE id = 1")
    await db.run("UPDATE channel_policy SET max_chapter = 17 WHERE channel_id = ?", T)
    assert await lib.ceiling(db, G, C, U, 1) == lib.REFUSED  # nothing is due anywhere...
    assert await lib.ceiling(db, G, T, U, 1) == lib.REFUSED  # ...the thread included
    assert await lib.search(db, 1, "whale", await lib.ceiling(db, G, T, U, 1)) == []
    # Control: the pin is honoured in full the moment its checkpoint is genuinely due.
    await db.run("UPDATE checkpoints SET due_at_utc = unixepoch() - 1 WHERE id = 1")
    assert await lib.ceiling(db, G, T, U, 1) == lib.Ceiling(17, "thread_pin")
    assert (await lib.ceiling(db, G, C, U, 1)).chapter == 3


async def test_a_stale_chapter_report_stops_capping_after_a_replan_to_pages(db):
    """A chapters plan, a chapter-5 report, then a re-plan by:pages. From then on
    /progress set writes `page` and never chapter_index, so the stale chapter row is
    still the newest one `mine` can see -- and it capped the member at 5 for the whole
    cycle, invisibly: /progress show lists their page reports, not the 5."""
    await db.run("UPDATE checkpoints SET chapter_ceiling = 34, unit = 'chapter'")
    await db.run("INSERT INTO member_progress (cohort_id, user_id, chapter_index)"
                 " VALUES (1,?,5)", U)
    # Positive control: in a genuine CHAPTERS cohort the report still narrows the ceiling,
    # or this clause has deleted the feature rather than scoping it.
    assert await lib.ceiling(db, G, C, U, 1) == lib.Ceiling(5, "cohort_checkpoint")
    assert await find(db) == []
    # Organizer re-plans the same cohort by:pages. reading.apply_plan writes cp.kind[:-1],
    # and a wrap-up meeting anchor keeps unit='chapter' -- it must not re-arm the cap.
    await db.run("UPDATE checkpoints SET unit = 'page', start_ref = 1, end_ref = 400")
    await db.run("INSERT INTO checkpoints (id, cohort_id, idx, label, unit, start_ref,"
                 " end_ref, chapter_ceiling, due_at_utc, tz_id, local_wall,"
                 " is_meeting_anchor) VALUES (2,1,0,'Wrap-up','chapter',0,0,0,0,'UTC',"
                 "'2026-09-30 19:00',1)")
    await db.run("INSERT INTO member_progress (cohort_id, user_id, page) VALUES (1,?,300)", U)
    assert await lib.ceiling(db, G, C, U, 1) == lib.Ceiling(34, "cohort_checkpoint")
    assert [(h.chapter, h.para) for h in await find(db)] == [(20, 3)]
    # It only ever REMOVES a narrowing: never above the cohort's own unlocked ceiling.
    await db.run("UPDATE checkpoints SET chapter_ceiling = 5 WHERE id = 1")
    assert await lib.ceiling(db, G, C, U, 1) == lib.Ceiling(5, "cohort_checkpoint")


async def test_search_ranks_the_best_match_first(db):
    """bm25() is NEGATIVE, so ascending is best-first; DESC would serve the worst match.
    The better hit is the LATER paragraph, so the tie-breakers cannot fake the order."""
    await db.run("UPDATE paragraphs SET text = 'aurora aurora aurora'"
                 " WHERE chapter_index = 2 AND para_index = 1")
    await db.run("UPDATE paragraphs SET text = 'aurora and a good many other words besides'"
                 " WHERE chapter_index = 1 AND para_index = 1")
    hits = await lib.search(db, 1, "aurora", lib.Ceiling(5, "t"), k=2)
    assert [(h.chapter, h.para) for h in hits] == [(2, 1), (1, 1)]


async def test_spoiler_tolerant_opt_out_is_scoped_to_its_own_cohort_and_book(db):
    """The one branch ABOVE the membership check, so its blast radius is pinned here:
    if anyone adds a /spoilers-allowed writer, these are the rows it hands out."""
    await db.run("INSERT INTO books (id, title, author, chapter_count, word_count,"
                 " char_count, source_sha256, ingested_at)"
                 " VALUES (2,'Next Month','A',34,340,600,'sha2',unixepoch())")
    await db.run("UPDATE channel_policy SET kind = 'spoiler_tolerant' WHERE channel_id = ?", C)
    outsider = U + 500  # never joined: the opt-out deliberately outranks membership
    unlimited = lib.Ceiling(lib.UNLIMITED, "spoiler_tolerant")
    assert await lib.ceiling(db, G, C, outsider, 1) == unlimited
    assert len(await lib.search(db, 1, "whale", unlimited)) == 3  # the whole book, ungated
    # ...but ONLY for the book this channel's live cohort is reading, and only while it
    # is live. Without the `scoped` check both of these were Ceiling(UNLIMITED) too.
    assert await lib.ceiling(db, G, C, outsider, 2) == lib.REFUSED
    assert await lib.ceiling(db, G, C, U, 2) == lib.REFUSED
    await db.run("UPDATE cohorts SET status = 'closed'")
    assert await lib.ceiling(db, G, C, outsider, 1) == lib.REFUSED
    assert await lib.ceiling(db, G, C, U, 1) == lib.REFUSED
    # 'denied' still outranks the opt-out.
    await db.run("UPDATE cohorts SET status = 'open'")
    await db.run("UPDATE channel_policy SET kind = 'denied' WHERE channel_id = ?", C)
    assert await lib.ceiling(db, G, C, U, 1) == lib.REFUSED


async def test_rebuild_is_required(db):
    await db.run("INSERT INTO para_fts(para_fts) VALUES('delete-all')")
    assert await lib.search(db, 1, PHRASE, OPEN) == []  # silent, no error, forever
    await db.run(lib._REBUILD)
    assert len(await lib.search(db, 1, PHRASE, OPEN)) == 1


@pytest.mark.parametrize("q", ['unbalanced "quote', "OR", "(a OR b) c", "", "*", "^", "NEAR("])
async def test_malformed_queries_return_empty_not_raise(db, q):
    assert await lib.search(db, 1, q, OPEN) == []


@pytest.mark.parametrize("q,match", [
    ('unbalanced "quote', '"unbalanced" AND "quote"'),
    ("OR", '"OR"'),
    ("NOT", '"NOT"'),
    ("whale AND", '"whale" AND "AND"'),
    ("(a OR b) c", '"a" AND "OR" AND "b" AND "c"'),  # no implicit AND after a group
    ("*", ""),
    ("^", ""),
    ("", ""),
])
async def test_fts_sanitizer_quotes_every_operator(db, q, match):
    """Pins the SANITISER, not search()'s except: each raw string below is an FTS5
    SYNTAX ERROR, so `[]` has to come from quoting rather than a swallowed error."""
    assert lib._fts(q) == match
    with pytest.raises(aiosqlite.Error):  # what the raw string would do to MATCH
        await db.all("SELECT rowid FROM para_fts WHERE para_fts MATCH ?", q)
    if match:  # the quoted form is VALID FTS5 that simply matches nothing
        assert await db.all("SELECT rowid FROM para_fts WHERE para_fts MATCH ?", match) == []
    assert await lib.search(db, 1, q, OPEN) == []


def test_spoiler_escapes_literal_bars():
    out = lib.spoiler("ch. 1", "she said ||nothing|| at all")
    assert out == "ch. 1\n" + r"||she said \|\|nothing\|\| at all||"
    assert out.count("||") == 2  # the payload can no longer close the spoiler early


OPF = ('<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc='
       '"http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title><dc:creator>A</dc:creator>'
       '</metadata><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
       '%s</manifest><spine><itemref idref="a"/></spine></package>')
COVER_ITEM = '<item id="cov" href="front.png" media-type="image/png" properties="cover-image"/>'
PNG = b"\x89PNG\r\n\x1a\n" + b"cover pixels"


def mkepub(tmp_path, cover=None, name="b.epub"):
    with zipfile.ZipFile(tmp_path / name, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><rootfiles><rootfile full-path="c.opf"/>'
                   "</rootfiles></container>")
        z.writestr("c.opf", OPF % (COVER_ITEM if cover else ""))
        z.writestr("a.xhtml", "<html><body><h1>One</h1><p>the white whale</p>"
                              "<h1>Two</h1><p>ahab speaks</p></body></html>")
        if cover:
            z.writestr("front.png", cover)
    return tmp_path / name


async def test_ingest_deletes_source_is_idempotent_and_lands_gated(db, tmp_path):
    await db.run("DELETE FROM paragraphs")  # this test owns book 2, not the fixture's
    bid = await lib.ingest(db, str(mkepub(tmp_path)))
    assert not (tmp_path / "b.epub").exists()  # we keep paragraphs, never the book
    assert await lib.ingest(db, str(mkepub(tmp_path))) == bid  # same sha256, same book
    assert [h.text for h in await lib.search(db, bid, "whale", lib.Ceiling(1, "t"))] == [
        "the white whale"]  # verbatim, and para_fts is populated without a manual rebuild
    assert await lib.search(db, bid, "ahab", lib.Ceiling(1, "t")) == []  # chapter 2 is gated
    assert lib.locator((await lib.search(db, bid, "ahab", lib.Ceiling(2, "t")))[0], "T", "A") == (
        'T by A - ch. 2 "Two", para 2 (~65% through)')


async def test_cover_round_trips_through_ingest(db, tmp_path):
    bid = await lib.ingest(db, str(mkepub(tmp_path, cover=PNG)))
    assert await lib.cover(db, bid) == (PNG, "image/png")


async def test_ingest_without_a_cover_still_succeeds_and_stays_searchable(db, tmp_path):
    """A cover is a nicety: no cover must still ingest, and still be fully searchable.

    Guard against a raising extractor, not just a missing one -- if epub._cover ever
    stops swallowing its own failures, this is what fails instead of every ingest.
    """
    bid = await lib.ingest(db, str(mkepub(tmp_path)))
    assert await lib.cover(db, bid) is None
    assert [h.text for h in await lib.search(db, bid, "whale", lib.Ceiling(2, "t"))] == [
        "the white whale"]


async def test_a_cover_extractor_that_raises_cannot_fail_an_ingest(db, tmp_path, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("corrupt manifest")

    monkeypatch.setattr(epub, "_cover_item", boom)
    bid = await lib.ingest(db, str(mkepub(tmp_path, cover=PNG)))
    assert await lib.cover(db, bid) is None
    assert await lib.search(db, bid, "whale", lib.Ceiling(2, "t")) != []


# ------------------------------------------------------- the migration ladder

async def test_a_v1_database_migrates_to_v2_and_keeps_its_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "ladder.db")
    old = Database(path)
    await old.connect()
    monkeypatch.delitem(dbmod.MIGRATIONS, 2)  # a database that stopped at version 1
    assert await old.migrate() == 1
    await old.run("INSERT INTO books (id, title) VALUES (7, 'Old Book')")
    await old.close()
    monkeypatch.undo()

    new = Database(path)
    await new.connect()
    assert await new.migrate() == 2
    row = await new.one("SELECT title, cover, cover_mime FROM books WHERE id = 7")
    assert (row["title"], row["cover"], row["cover_mime"]) == ("Old Book", None, None)
    await new.close()


async def test_a_fresh_database_lands_on_the_newest_version(db):
    assert (await db.one("PRAGMA user_version"))[0] == max(dbmod.MIGRATIONS)
    assert await lib.cover(db, 1) is None  # the fixture's book has the columns, unset


async def test_migrating_twice_is_a_no_op(db):
    async def ddl():
        return [tuple(r) for r in
                await db.all("SELECT name, sql FROM sqlite_master ORDER BY name")]

    before = await ddl()
    assert await db.migrate() == max(dbmod.MIGRATIONS)  # a re-run of 002 would raise
    assert await ddl() == before


async def test_two_percent_cap_halts_a_sequential_walk(db):
    assert 3 * W <= int(34 * 5 * W * 0.02) < 4 * W  # the allowance is 34 words
    for paras in ([(1, 1)], [(1, 3)], [(1, 5)]):  # gapped, so not abutting
        assert await lib.budget(db, U, 1, W, paras) is None
    assert await lib.budget(db, U, 1, W, [(2, 1)]) == lib.REASONS["cumulative"]


async def test_word_cap_is_per_quote_and_three_per_response(db):
    """75 words PER QUOTE and 3 quotes per response are two SEPARATE limits.

    Each assertion below fails if one specific check is deleted, which is what the
    old aggregate-only `words > 75 * len(runs)` could not do: 3 runs shared a
    225-word budget, so one 225-word passage was served whole.
    """
    await db.run("UPDATE books SET is_public_domain = 1")  # isolate the shape caps
    three = [(30, 1), (31, 1), (32, 1)]  # gapped chapters: three separate quotes
    await db.run("UPDATE paragraphs SET word_count = 200 WHERE chapter_index = 30"
                 " AND para_index = 1")
    # 220 is UNDER the 3 x 75 aggregate, so only a per-run check can refuse this.
    assert await lib.budget(db, U, 1, 220, three) == lib.REASONS["per_quote"]
    await db.run("UPDATE paragraphs SET word_count = 60 WHERE para_index = 1"
                 " AND chapter_index IN (30, 31, 32)")
    # The aggregate check still refuses a caller claiming more than 3 x 75 in total.
    assert await lib.budget(db, U, 1, 300, three) == lib.REASONS["per_quote"]
    assert await lib.budget(db, U, 1, 4 * 60, [*three, (33, 1)]) == lib.REASONS["per_response"]
    assert await lib.budget(db, U, 1, 3 * 60, three) is None  # 3 x 60 words is allowed


async def test_abutting_and_whole_chapter_refused(db):
    assert await lib.budget(db, U, 1, 2 * W, [(4, 1), (4, 2)]) is None
    assert await lib.budget(db, U, 1, W, [(4, 3)]) == lib.REASONS["abutting"]
    whole = [(9, 1), (9, 2), (9, 3)]  # 3 of 5 paragraphs is over half
    assert await lib.budget(db, U, 1, 3 * W, whole) == lib.REASONS["whole_chapter"]
    await db.run("UPDATE books SET is_public_domain = 1")
    assert await lib.budget(db, U, 1, W, [(4, 3)]) is None  # fast path skips the caps
    assert await lib.budget(db, U, 1, 3 * W, whole) == lib.REASONS["whole_chapter"]
