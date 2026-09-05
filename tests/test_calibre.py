"""The Calibre library reader, and the one rule that makes it safe to point at a real
library: ingesting from it must COPY, because library.ingest() deletes what it is given.
"""

import sqlite3

import pytest

from marginalia import calibre
from marginalia.cogs import quote
from marginalia.db import Database
from tests.test_library import mkepub  # one synthetic EPUB, not a second copy of it


def mklibrary(tmp_path, title="Interview With the Vampire", author="Anne Rice",
              rel="Anne Rice/Interview With the Vampire (201)", book_id=201):
    """A throwaway Calibre library: the real table shapes, one book, one EPUB on disk."""
    lib = tmp_path / "lib"
    (lib / rel).mkdir(parents=True)
    epub = mkepub(lib / rel, name="book.epub")
    conn = sqlite3.connect(lib / calibre.DB_NAME)
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT,
                            last_modified TIMESTAMP);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
        CREATE TABLE data (book INTEGER, format TEXT, name TEXT);
    """)
    conn.execute("INSERT INTO books VALUES (?,?,?,'2026-09-05')", (book_id, title, rel))
    conn.execute("INSERT INTO authors VALUES (1,?)", (author,))
    conn.execute("INSERT INTO books_authors_link VALUES (?,1)", (book_id,))
    conn.execute("INSERT INTO data VALUES (?, 'EPUB', ?)", (book_id, epub.stem))
    conn.commit()
    conn.close()
    return lib, epub


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.migrate()
    yield d
    await d.close()


def test_search_and_entry_read_a_real_library_shape(tmp_path):
    lib, epub = mklibrary(tmp_path)
    (hit,) = calibre.search(str(lib), "vampire")
    assert (hit.book_id, hit.title, hit.author, hit.path) == (201, "Interview With the "
                                                              "Vampire", "Anne Rice", epub)
    assert calibre.search(str(lib), "") == [hit]  # no query lists the library
    assert calibre.search(str(lib), "nothing here") == []
    assert calibre.entry(str(lib), 201) == hit
    assert calibre.entry(str(lib), 999) is None  # an id from a stale autocomplete


def test_a_missing_or_unreadable_library_raises_rather_than_returning_nothing(tmp_path):
    """Empty and broken must not look alike: [] means 'no such book', which would send
    an organizer hunting for a title that is really a missing bind mount."""
    with pytest.raises(calibre.CalibreError):
        calibre.search(str(tmp_path / "absent"))
    with pytest.raises(calibre.CalibreError):
        calibre.entry(str(tmp_path / "absent"), 1)


def test_a_row_pointing_outside_the_library_is_refused(tmp_path):
    """books.path is Calibre's to write, but a hand-edited one must not read /etc."""
    lib, epub = mklibrary(tmp_path, rel="../escape")
    assert calibre.search(str(lib), "vampire") == []
    assert calibre.entry(str(lib), 201) is None
    assert epub.exists()  # the file is really there; only the path check refused it


def test_a_metadata_row_whose_file_is_gone_is_not_offered(tmp_path):
    lib, epub = mklibrary(tmp_path)
    epub.unlink()
    assert calibre.search(str(lib), "vampire") == []


async def test_ingesting_from_calibre_copies_and_never_deletes_the_library(db, tmp_path):
    """THE load-bearing test. library.ingest() unlinks the path it is handed, so handing
    it the library path would delete the book out of Calibre. If this regresses, using
    the feature destroys the user's library one book at a time."""
    lib, epub = mklibrary(tmp_path)
    before = epub.read_bytes()
    tmp = tmp_path / "copy.epub"

    msg = await quote.take_from_calibre(db, str(lib), 201, True, tmp)

    assert msg.startswith("Ingested book #")
    assert epub.exists() and epub.read_bytes() == before, (
        "the Calibre library file was deleted or altered -- ingest got the original")
    assert not tmp.exists()  # the copy is what was consumed
    assert (await db.one("SELECT COUNT(*) FROM paragraphs"))[0] > 0


async def test_calibre_ingest_refuses_without_the_attestation_and_touches_nothing(db, tmp_path):
    lib, epub = mklibrary(tmp_path)
    tmp = tmp_path / "copy.epub"
    assert await quote.take_from_calibre(db, str(lib), 201, False, tmp) == quote.ATTEST
    assert epub.exists() and not tmp.exists()
    assert (await db.one("SELECT COUNT(*) FROM books"))[0] == 0


async def test_calibre_failures_are_reported_not_raised(db, tmp_path):
    """An organizer gets a sentence, never a traceback and never a silent no-op."""
    tmp = tmp_path / "copy.epub"
    missing = await quote.take_from_calibre(db, str(tmp_path / "absent"), 1, True, tmp)
    assert "could not read the Calibre library" in missing

    lib, epub = mklibrary(tmp_path)
    epub.unlink()
    assert await quote.take_from_calibre(db, str(lib), 201, True, tmp) == quote.GONE
