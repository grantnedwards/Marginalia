"""Two chapters, ceiling 1. Chapter 2 is what must never leak."""

import zipfile

import pytest

from marginalia import library
from marginalia.cogs import _views, quote
from marginalia.db import Database

G, C, U = 1545535072151670824, 1545535072151670825, 1545535072151670826
SECRET, TITLE2 = "sister dies", "Belowdecks"
KW = dict(guild_id=G, channel_id=C, user_id=U, book_id=1)
BAR = r"||the white whale \|\|sounded\|\| twice||"
JPEG = b"\xff\xd8\xff" + b"\x00" * 32


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.migrate()  # cover/cover_mime arrive in migration 2, not in schema.sql
    await d.run("INSERT INTO books (id, title, author, chapter_count, word_count, char_count,"
                " source_sha256, ingested_at) VALUES (1,'Moby-Dick','Melville',2,1000,600,"
                "'sha',unixepoch())")
    # status: only a LIVE cohort (club.LIVE) scopes a ceiling; the default is 'draft'.
    await d.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, status)"
                " VALUES (1,?,?,1,'2026-09','open')", G, C)
    await d.run("INSERT INTO cohort_members (cohort_id, user_id) VALUES (1,?)", U)
    await d.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id) VALUES (?,?,1)", C, G)
    await d.run("INSERT INTO checkpoints (id, cohort_id, idx, label, start_ref, end_ref,"
                " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                " VALUES (1,1,1,'a',1,2,1,0,'UTC','2026-09-06 19:00')")
    async with d.tx() as conn:
        for ci, title, body in ((1, "Nantucket", "the white whale ||sounded|| twice"),
                                (2, TITLE2, f"and then the {SECRET} at last")):
            await conn.execute("INSERT INTO chapters (id, book_id, chapter_index, title,"
                               " para_count) VALUES (?,1,?,?,4)", (ci, ci, title))
            await conn.execute(
                "INSERT INTO paragraphs (book_id, chapter_id, chapter_index, para_index, text,"
                " word_count, char_start, char_count) VALUES (1,?,?,1,?,?,?,?)",
                (ci, ci, body, len(body.split()), ci * 100, len(body)))
    yield d
    await d.close()


class Tap:
    """The interaction a button click hands back. A component on an ephemeral message can
    only edit that message, so edit_message is the whole surface."""

    def __init__(self) -> None:
        self.response = self
        self.edits: list[dict] = []

    async def edit_message(self, **kw) -> None:
        self.edits.append(kw)


def texts(embed) -> list[str]:
    """Every user-visible string on an embed. Not to_dict(): the colour is digits by nature
    and would make the no-digit check vacuous."""
    if embed is None:
        return []
    return [t for t in (embed.title, embed.description, embed.footer.text, embed.author.name,
                        *(x for f in embed.fields for x in (f.name, f.value))) if t]


def counted(monkeypatch) -> list:
    """Records every library.budget() call. Nothing else can charge the allowance, so a
    length of 1 is the whole "exactly once per served quote" rule."""
    calls: list = []
    real = library.budget

    async def wrapper(*a, **kw):
        calls.append(a)
        return await real(*a, **kw)

    monkeypatch.setattr(library, "budget", wrapper)
    return calls


async def test_over_ceiling_refuses_and_under_ceiling_serves(db, monkeypatch):
    """Both halves: without the positive control an always-empty gate looks fine."""
    calls = counted(monkeypatch)
    q = f"what happens when the {SECRET} in chapter 2"
    r = await quote.serve(db, query=q, **KW)
    assert (r.ephemeral, r.embed.description) == (True, quote.NOTHING)
    assert not calls  # nothing served, nothing charged
    r = await quote.serve(db, query="whale", **KW)
    assert r.ephemeral is True  # ephemeral by default: no push notification is the defence
    assert r.embed.description.startswith("Moby-Dick by Melville - ch. 1")
    assert BAR in r.embed.description  # library.spoiler, not undone
    assert r.embed.description.count("||") == 2
    assert len(calls) == 1, "budget() writes the ledger, so exactly once per served quote"


async def test_public_share_is_opt_in(db):
    await db.run("UPDATE books SET is_public_domain = 1")  # dodge the 24h abutment rule
    assert (await quote.serve(db, query="whale", share=True, **KW)).ephemeral is False


@pytest.mark.parametrize("path", [{"book_id": 99}, {"user_id": U + 1}, {}])
async def test_no_refusal_names_the_query_a_chapter_or_a_count(db, path):
    """No such book, a refused ceiling, and no match: an embed TITLE is a new place to
    leak, so check every visible string on the card, not just the description."""
    q = f"what happens when the {SECRET} in chapter 2"
    r = await quote.serve(db, **{**KW, "query": q, **path})
    said = [s for s in texts(r.embed) if s]
    assert said and r.ephemeral is True
    for s in said:
        for leak in (SECRET, TITLE2, q, "chapter", "whale"):
            assert leak not in s, f"refusal leaked {leak!r}"
        assert not any(ch.isdigit() for ch in s)  # no match count, no chapter number
    assert (await db.one("SELECT COUNT(*) FROM quote_ledger"))[0] == 0


def test_the_quote_card_bars_the_text_and_leaves_the_citation_outside():
    loc = 'Moby-Dick by Melville - ch. 1 "Nantucket", para 1 (~16% through)'
    e = quote.quote_card([library.spoiler(loc, "the white whale ||sounded|| twice")],
                         "Moby-Dick by Melville")
    citation, _, barred = e.description.partition("\n")
    assert citation == loc and "||" not in citation
    assert barred == BAR  # a literal || in the passage is still escaped
    assert e.description.count("||") == 2
    assert e.footer.text == "Moby-Dick by Melville" and "||" not in e.footer.text
    assert e.title is None and len(e) < 6000
    assert e.thumbnail.url is None  # no cover: still a valid, deliberate-looking embed
    png = _views.cover_file((JPEG, "image/png"))  # the name follows the media type
    assert quote.quote_card([BAR], "b", png).thumbnail.url == "attachment://cover.png"


def test_fit_uses_the_description_cap_not_the_2000_of_a_message():
    bars = [library.spoiler("ch. 1", "x" * 800)] * 3  # ~2.4k chars: over MAX_LEN, under 4096
    body = quote.quote_card(bars, "b").description
    assert quote.TRUNCATED not in body and len(body) > quote.MAX_LEN
    assert body.count("||") == 6  # every bar still opened and closed


async def test_find_is_a_citation_list_with_no_book_text_and_no_charge(db, monkeypatch):
    calls = counted(monkeypatch)
    r = await quote.serve(db, query="whale", k=3, text=False, **KW)
    assert r.ephemeral is True and not calls
    assert r.embed.description.startswith("- Moby-Dick by Melville - ch. 1")
    assert "||" not in r.embed.description and "the white whale" not in r.embed.description
    assert r.file is None and r.embed.thumbnail.url is None  # no cover is the common case
    assert (await db.one("SELECT COUNT(*) FROM quote_ledger"))[0] == 0

    await db.run("UPDATE books SET cover = ?, cover_mime = 'image/jpeg'", JPEG)
    r = await quote.serve(db, query="whale", k=3, text=False, **KW)
    assert r.file.filename == "cover.jpg"
    assert r.embed.thumbnail.url == "attachment://cover.jpg"


async def test_budget_exhaustion_offers_a_locator_only_button_and_charges_nothing_twice(
        db, monkeypatch):
    await db.run("UPDATE books SET word_count = 50")  # 2% allowance = 1 word
    calls = counted(monkeypatch)
    r = await quote.serve(db, query="whale", **KW)
    assert r.ephemeral is True
    assert quote.LOCATOR_ONLY in r.embed.description  # the refusal still offers the mode ...
    assert "||" not in r.embed.description  # ... and carries no text
    assert len(calls) == 1

    tap = Tap()
    await r.view.children[0].callback(tap)  # the "Locator only" button
    assert len(calls) == 1, "the button re-entered the charging path"
    assert (await db.one("SELECT COUNT(*) FROM quote_ledger"))[0] == 0
    shown = tap.edits[-1]["embed"]
    assert "ch. 1" in shown.description and "||" not in shown.description
    assert tap.edits[-1]["view"] is None  # one tap, then the button retires


async def test_ingest_reports_drm_plainly(db, tmp_path):
    p = tmp_path / "drm.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><EncryptedData><EncryptionMethod '
                   'Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>'
                   "</EncryptedData></encryption>")
    assert await quote.take(db, str(p), True) == quote.DRM
    assert not p.exists()


async def test_purge_refuses_a_book_a_cohort_still_holds_before_asking_to_confirm(db):
    """The cohorts FK is RESTRICT, so the delete would fail anyway -- but the organizer
    must learn that at the dry run, not after typing confirm: True on a real takedown."""
    dry = await quote.purge(db, 1, confirm=False)
    assert dry.startswith(quote.IN_USE) and "2026-09" in dry
    assert await quote.purge(db, 1, confirm=True) == dry  # confirming changes nothing
    assert await db.one("SELECT 1 FROM books WHERE id = 1") is not None


async def test_purge_deletes_a_book_no_cohort_holds(db):
    await db.run("INSERT INTO books (id, title, author, chapter_count, word_count,"
                 " char_count, source_sha256, ingested_at) VALUES (2,'Frankenstein','Shelley',"
                 "43,89393,500000,'sha2',unixepoch())")
    assert (await quote.purge(db, 2, confirm=False)).startswith("This would permanently")
    assert (await quote.purge(db, 2, confirm=True)).startswith("Deleted Frankenstein")
    assert await db.one("SELECT 1 FROM books WHERE id = 2") is None

