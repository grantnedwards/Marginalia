"""Read-only view of a Calibre library that lives on the same host.

Deliberately NOT an HTTP client for calibre-web. The library is a directory, so a
read-only bind mount is the entire integration: no credentials to store, no outbound
requests, and nothing new for the spoiler gate to reason about. This module only ever
opens Calibre's own ``metadata.db`` with ``mode=ro`` and reads bytes off disk.

Calibre keeps the file for book N at ``<library>/<books.path>/<data.name>.<format>``.
``books.path`` is written by Calibre, never by a user, but it is still joined and then
checked to be inside the library -- an absolute or ``..`` path in that column would
otherwise read an arbitrary file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Calibre's own limit-ish; Discord shows at most 25 autocomplete choices anyway.
MAX_HITS = 25
DB_NAME = "metadata.db"


class CalibreError(Exception):
    """The library is missing, unreadable, or not a Calibre library."""


@dataclass(frozen=True)
class Entry:
    book_id: int  # Calibre's id, not marginalia's
    title: str
    author: str
    path: Path  # the EPUB on disk, already checked to be inside the library


_SQL = """
SELECT b.id, b.title,
       COALESCE((SELECT group_concat(a.name, ' & ') FROM authors a
                  JOIN books_authors_link l ON l.author = a.id
                 WHERE l.book = b.id), '') AS author,
       b.path, d.name
  FROM books b
  JOIN data d ON d.book = b.id AND d.format = 'EPUB'
 WHERE b.title LIKE ?
 ORDER BY b.last_modified DESC
 LIMIT ?
"""


def _connect(library: str) -> sqlite3.Connection:
    """Open the library read-only. `mode=ro` still reads a live WAL, so this sees books
    Calibre added seconds ago; it just may never write, checkpoint or lock."""
    db = Path(library) / DB_NAME
    if not db.is_file():
        raise CalibreError(f"no Calibre library at {library} (expected {DB_NAME} in it)")
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise CalibreError(f"cannot read {db}: {exc}") from None


def _resolve(library: str, rel_dir: str, stem: str) -> Path | None:
    """<library>/<rel_dir>/<stem>.epub, or None if it escapes the library or is absent."""
    root = Path(library).resolve()
    try:
        path = (root / rel_dir / f"{stem}.epub").resolve()
    except OSError:
        return None
    if not path.is_relative_to(root):  # a hand-edited books.path must not read /etc
        return None
    return path if path.is_file() else None


def search(library: str, query: str = "", limit: int = MAX_HITS) -> list[Entry]:
    """Books with an EPUB whose title contains `query`, newest first. Blocking: call it
    off the event loop. Raises CalibreError only when the library itself is unusable."""
    like = f"%{query.strip()}%"
    conn = _connect(library)
    try:
        rows = conn.execute(_SQL, (like, max(1, min(limit, MAX_HITS)))).fetchall()
    except sqlite3.Error as exc:
        raise CalibreError(f"Calibre's database did not answer: {exc}") from None
    finally:
        conn.close()
    out = []
    for book_id, title, author, rel_dir, stem in rows:
        path = _resolve(library, rel_dir, stem)
        if path is not None:  # a metadata row whose file is gone is not offerable
            out.append(Entry(int(book_id), str(title), str(author), path))
    return out


def entry(library: str, book_id: int) -> Entry | None:
    """One book by Calibre id, or None. Re-reads rather than trusting an autocomplete
    value, because the id arrives from the client and the file may have moved since."""
    conn = _connect(library)
    try:
        row = conn.execute(
            _SQL.replace("b.title LIKE ?", "b.id = ?"), (int(book_id), 1)
        ).fetchone()
    except sqlite3.Error as exc:
        raise CalibreError(f"Calibre's database did not answer: {exc}") from None
    finally:
        conn.close()
    if row is None:
        return None
    path = _resolve(library, row[3], row[4])
    return None if path is None else Entry(int(row[0]), str(row[1]), str(row[2]), path)
