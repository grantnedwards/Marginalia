"""Ingest, FTS5 search, THE SPOILER GATE, and the quote budget.

The only module that may name ``paragraphs`` or ``para_fts`` -- an invariant test greps
for it. The gate is the SQL ``WHERE`` clause in :data:`_SQL`, never a prompt and never a
Python post-filter; :func:`search` is the only way in; UNKNOWN scope FAILS CLOSED to 0.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from marginalia import epub
from marginalia.db import Database

UNLIMITED = 1_000_000
_SNIPPET_TOKENS = 32  # must be 1..64 or snippet() errors
_MAX_HITS = 50
# Triggers do NOT backfill, so bulk ingest MUST run this or the index is empty forever.
_REBUILD = "INSERT INTO para_fts(para_fts) VALUES('rebuild')"

WORDS_PER_QUOTE = 75
QUOTES_PER_RESPONSE = 3
QUOTES_PER_HOUR = 10
QUOTES_PER_DAY = 40
BOOK_FRACTION = 0.02  # THE anti-reconstruction control: 2% of the book ...
WINDOW_SECS = 30 * 86_400  # ... per user per ROLLING 30 days
ABUT_SECS = 86_400  # abutment memory: 1-10 then 11-20 then 21-30 rebuilds a chapter

# Looked up, never an f-string: a refusal carrying book text or a match count IS a spoiler.
REASONS = {
    "fail_closed": "I cannot work out what you are allowed to see here, so I am not answering.",
    "nothing": "There is nothing there to quote.",
    "per_response": "That would be more passages than I hand out at once.",
    "per_quote": "That passage is longer than I quote in one go.",
    "whole_chapter": "That is too much of one chapter to quote.",
    "abutting": "That runs straight on from a passage you were just given.",
    "hourly": "You have reached this hour's quote limit. Try again later.",
    "daily": "You have reached today's quote limit. Try again tomorrow.",
    "cumulative": "You have reached this book's quote allowance for the last 30 days.",
}


@dataclass(frozen=True)
class Ceiling:
    chapter: int  # max visible chapter_index; 0 == refuse
    reason: str


@dataclass(frozen=True)
class Hit:
    chapter: int
    chapter_title: str
    para: int
    text: str  # VERBATIM, never normalised -- this is what makes quoting honest
    snippet: str
    pct: int


REFUSED = Ceiling(0, "fail_closed")


def _sha256(src: Path) -> str:
    with src.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


async def _chapter_rows(
    conn: aiosqlite.Connection, book_id: int, book: epub.Book,
    by_chapter: dict[int, list[epub.Para]], words: dict[int, int]
) -> list[tuple]:
    """INSERT one chapters row each; return the paragraphs rows to bulk-insert."""
    rows = []
    for ch in book.chapters:
        ps = by_chapter.get(ch.index, [])
        ci = ch.index + 1  # 1-based on disk so ceiling 0 means "nothing visible"
        cur = await conn.execute(
            "INSERT INTO chapters (book_id, chapter_index, title, epub_href,"
            " char_start, char_count, word_count, para_count) VALUES (?,?,?,?,?,?,?,?)",
            (book_id, ci, ch.title, ps[0].href if ps else "",
             ps[0].char_start if ps else 0, sum(len(p.text) for p in ps),
             words.get(ch.index, 0), len(ps)))
        rows += [(book_id, int(cur.lastrowid), ci, p.para_index + 1, p.text,
                  len(p.text.split()), p.char_start, len(p.text), p.href, p.anchor)
                 for p in ps]
    return rows


async def ingest(db: Database, path: str) -> int:
    """Parse an EPUB, idempotent on file sha256. DELETES the source: we keep paragraphs,
    never the book. Blocking CPU threads off the reminder poller's event loop."""
    src = Path(path)
    sha = await asyncio.to_thread(_sha256, src)
    row = await db.one("SELECT id FROM books WHERE source_sha256 = ?", sha)
    if row is not None:
        await asyncio.to_thread(src.unlink, True)
        return int(row[0])

    book = await asyncio.to_thread(epub.parse, src)
    by_chapter: dict[int, list[epub.Para]] = {}
    for p in book.paras:
        by_chapter.setdefault(p.chapter_index, []).append(p)
    words = {ci: sum(len(p.text.split()) for p in ps) for ci, ps in by_chapter.items()}

    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT INTO books (title, author, chapter_count, word_count, char_count,"
            " source_sha256, cover, cover_mime, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,unixepoch())",
            (book.title, book.author, len(book.chapters), sum(words.values()),
             book.total_chars, sha, book.cover, book.cover_mime))
        book_id = int(cur.lastrowid)
        rows = await _chapter_rows(conn, book_id, book, by_chapter, words)
        await conn.executemany(
            "INSERT INTO paragraphs (book_id, chapter_id, chapter_index, para_index, text,"
            " word_count, char_start, char_count, epub_href, epub_anchor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        await conn.execute(_REBUILD)
    await asyncio.to_thread(src.unlink)
    return book_id


async def cover(db: Database, book_id: int) -> tuple[bytes, str] | None:
    """The EPUB's own cover and its media-type, or None -- no cover is normal."""
    row = await db.one("SELECT cover, cover_mime FROM books WHERE id = ?", book_id)
    if row is None or row["cover"] is None or not row["cover_mime"]:
        return None
    return bytes(row["cover"]), str(row["cover_mime"])


_CEILING_SQL = """
SELECT pol.kind, pol.max_chapter, co.id AS scoped,
       (SELECT MAX(cp.chapter_ceiling) FROM checkpoints cp
         WHERE cp.cohort_id = co.id AND cp.due_at_utc <= unixepoch())         AS unlocked,
       (SELECT mp.chapter_index FROM member_progress mp
         WHERE mp.cohort_id = co.id AND mp.user_id = ? AND mp.chapter_index IS NOT NULL
           -- Revert this and a stale chapter row caps a pages-mode member all cycle, unclearable.
           AND EXISTS (SELECT 1 FROM checkpoints cp2
                        WHERE cp2.cohort_id = co.id AND cp2.is_meeting_anchor = 0
                          AND cp2.unit = 'chapter')
         ORDER BY mp.reported_at DESC, mp.id DESC LIMIT 1)                    AS mine,
       (SELECT 1 FROM cohort_members m
         WHERE m.cohort_id = co.id AND m.user_id = ? AND m.left_at IS NULL)   AS member,
       (SELECT 1 FROM books b WHERE b.id = ?
          AND b.ingested_at IS NOT NULL AND b.chapter_count > 0)              AS ingested
  FROM (SELECT 1)
  LEFT JOIN channel_policy pol ON pol.channel_id = ? AND pol.guild_id = ?
  -- Both joins are LEFT so a MISSING policy row is not a missing row: the club
  -- channel is the one a live cohort was opened in, and no row there means
  -- "default club policy", not "refuse" (fail-closed is about UNKNOWN scope).
  -- The status list mirrors club.LIVE, which is the source of truth -- a
  -- 'draft' or 'closed' cohort must not scope a ceiling. Do not import a cog.
  LEFT JOIN cohorts co ON co.guild_id = ? AND co.book_id = ?
                      AND co.status IN ('open', 'active')
                      AND (co.id = pol.cohort_id
                           OR (pol.channel_id IS NULL AND co.channel_id = ?))
 ORDER BY co.id DESC LIMIT 1
"""


def _cap(chapter: int | None, reason: str) -> Ceiling:
    """Below 1 shows nothing, so it is a refusal rather than a small window."""
    return Ceiling(int(chapter), reason) if chapter is not None and chapter >= 1 else REFUSED


async def ceiling(
    db: Database, guild_id: int, channel_id: int, user_id: int, book_id: int
) -> Ceiling:
    """Max chapter_index this member may see here, and why. Never raises; anything
    unresolvable (unknown user, unmapped channel, no live cohort) is REFUSED."""
    try:
        r = await db.one(_CEILING_SQL, user_id, user_id, book_id,
                         channel_id, guild_id, guild_id, book_id, channel_id)
    except Exception:
        return REFUSED  # a ceiling we cannot read is a ceiling we do not trust
    if r is None or not r["ingested"]:
        return REFUSED  # unmapped channel, or the book was never ingested
    if r["kind"] == "denied":
        return REFUSED
    if r["kind"] == "spoiler_tolerant":
        # The ONE branch above membership: an operator's opt-out, never written by this
        # code. `scoped` holds it to this channel's own live cohort+book -- unscoped, it
        # handed EVERY ingested book in the database to every non-member.
        return Ceiling(UNLIMITED, "spoiler_tolerant") if r["scoped"] else REFUSED
    if not r["member"]:
        # PRIMARY guard for the cross-book thread leak too (NULL when the cohort join
        # missed), above the PUBLIC thread pin. Unknown user, or one who left.
        return REFUSED
    if r["kind"] == "chapter_thread":
        # `scoped` is now redundant belt-and-braces behind that check. Keep both.
        # A re-plan can re-sync a live thread's pin to a checkpoint that is NOT due yet.
        pin = min(int(r["max_chapter"] or 0), int(r["unlocked"] or 0))
        return _cap(pin, "thread_pin") if r["scoped"] else REFUSED
    if r["unlocked"] is None:
        return REFUSED  # no cohort for this channel+book, or nothing unlocked yet
    chapter = int(r["unlocked"])
    if r["mine"] is not None:
        chapter = min(chapter, int(r["mine"]))
    return _cap(chapter, "cohort_checkpoint")


# Not [A-Za-z0-9]: the tokenizer is unicode61, so ASCII-only drops non-Latin queries.
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*")

# bm25() scores are NEGATIVE, so ORDER BY ASCENDING is best-first (DESC = worst match).
_SQL = f"""
SELECT p.chapter_index AS ch, c.title AS title, p.para_index AS para, p.text AS text,
       snippet(para_fts, 0, '**', '**', ' ... ', {_SNIPPET_TOKENS}) AS snip,
       COALESCE(CAST(100.0 * p.char_start / NULLIF(b.char_count, 0) AS INTEGER), 0) AS pct
  FROM para_fts
  JOIN paragraphs p ON p.id = para_fts.rowid
  JOIN chapters   c ON c.id = p.chapter_id
  JOIN books      b ON b.id = p.book_id
 WHERE para_fts MATCH ?
   AND p.book_id = ?
   AND c.chapter_index <= ?
 ORDER BY bm25(para_fts), p.chapter_index, p.para_index
 LIMIT ?
"""


def _fts(q: str) -> str:
    """User string -> safe FTS5 MATCH expression; '' when nothing is usable. Quoting each
    token makes every operator literal, so a malformed query can only fail to match: raw,
    ``(a OR b) c``, a lone ``OR``, an unbalanced quote, ``*``, ``^`` are SYNTAX ERRORS."""
    return " AND ".join(f'"{t}"' for t in _TOKEN.findall(q))


async def search(db: Database, book_id: int, q: str, ceil: Ceiling, k: int = 3) -> list[Hit]:
    """THE gated search over book text, and the only way in; [] on anything unusable."""
    match = _fts(q)
    # Load-bearing, not hygiene: `chapter_index <= 'x'` is TRUE for every row (SQLite
    # orders every INTEGER before every TEXT), so a str/None ceiling DELETES the gate.
    if not match or type(ceil.chapter) is not int or ceil.chapter < 1 or k < 1:
        return []
    try:
        rows = await db.all(_SQL, match, book_id, ceil.chapter, min(k, _MAX_HITS))
    except aiosqlite.Error:
        return []  # belt and braces behind _fts; a gate must not fail open
    return [Hit(r["ch"], r["title"], r["para"], r["text"], r["snip"], r["pct"]) for r in rows]


def locator(h: Hit, book_title: str, author: str) -> str:
    """The citation, which goes OUTSIDE the spoiler bars. Carries no book text."""
    title = f' "{h.chapter_title}"' if h.chapter_title else ""
    return (f"{book_title} by {author} - ch. {h.chapter}{title}, "
            f"para {h.para} (~{h.pct}% through)")


def spoiler(locator: str, text: str) -> str:
    """Locator outside the bars, text inside; a literal ``||`` would CLOSE it early."""
    return f"{locator}\n||{text.replace('||', r'\|\|')}||"


def _runs(paras: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    out: list[list[int]] = []
    for ch, pi in sorted(set(paras)):
        if out and out[-1][0] == ch and pi == out[-1][2] + 1:
            out[-1][2] = pi
        else:
            out.append([ch, pi, pi])
    return [(a, b, c) for a, b, c in out]


async def _shape_refusal(
    db: Database, book_id: int, runs: list[tuple[int, int, int]], words: int
) -> str | None:
    """Refusals about the SHAPE of the request, in precedence order."""
    if len(runs) > QUOTES_PER_RESPONSE:
        return REASONS["per_response"]
    if words > WORDS_PER_QUOTE * len(runs):
        return REASONS["per_quote"]
    for ch, start, end in runs:
        # PER RUN as well as in total: 3 runs sharing 225 words is one long passage.
        run_words = await db.one(
            "SELECT COALESCE(SUM(word_count), 0) FROM paragraphs WHERE book_id = ?"
            " AND chapter_index = ? AND para_index BETWEEN ? AND ?", book_id, ch, start, end)
        if int(run_words[0]) > WORDS_PER_QUOTE:
            return REASONS["per_quote"]
        row = await db.one(
            "SELECT para_count FROM chapters WHERE book_id = ? AND chapter_index = ?",
            book_id, ch)
        if row and row[0] > 0 and (end - start + 1) * 2 > row[0]:
            return REASONS["whole_chapter"]
    return None


async def _rate_refusal(
    db: Database, user_id: int, book_id: int, runs: list[tuple[int, int, int]], words: int,
    book: aiosqlite.Row
) -> str | None:
    """Refusals about the USER's recent consumption, in precedence order."""
    recent = await db.all(
        "SELECT chapter_index, para_start, para_end FROM quote_ledger"
        " WHERE user_id = ? AND book_id = ? AND served_at > unixepoch() - ?",
        user_id, book_id, ABUT_SECS)
    for ch, start, end in runs:
        if any(ch == s["chapter_index"] and start <= s["para_end"] + 1
               and s["para_start"] <= end + 1 for s in recent):
            return REASONS["abutting"]
    for reason, cap, since in (("hourly", QUOTES_PER_HOUR, 3600),
                               ("daily", QUOTES_PER_DAY, 86_400)):
        # SUM(quote_count), not COUNT(*): a row may stand for several ranges.
        served = await db.one(
            "SELECT COALESCE(SUM(quote_count), 0) FROM quote_ledger"
            " WHERE user_id = ? AND served_at > unixepoch() - ?", user_id, since)
        if int(served[0]) + len(runs) > cap:
            return REASONS[reason]
    allowance = int(int(book["word_count"] or 0) * BOOK_FRACTION)
    if allowance > 0:
        spent = await db.one(
            "SELECT COALESCE(SUM(word_count), 0) FROM quote_ledger"
            " WHERE user_id = ? AND book_id = ? AND served_at > unixepoch() - ?",
            user_id, book_id, WINDOW_SECS)
        if int(spent[0]) + words > allowance:
            return REASONS["cumulative"]
    return None


async def budget(
    db: Database, user_id: int, book_id: int, words: int, paras: list[tuple[int, int]]
) -> str | None:
    """May these paragraphs be served to this user? ``None`` == allowed.

    Contiguous ``(chapter_index, para_index)`` coordinates count as ONE quote; first
    refusal wins. WRITES quote_ledger on allow, so call it once per served quote.

    # ponytail: a failed send still spends the allowance; split out record() if that bites.
    """
    runs = _runs(paras)
    if not runs or words < 0:
        return REASONS["nothing"]
    book = await db.one("SELECT word_count, is_public_domain FROM books WHERE id = ?", book_id)
    if book is None:
        return REASONS["fail_closed"]
    if refusal := await _shape_refusal(db, book_id, runs, words):
        return refusal
    if not book["is_public_domain"] and (
            refusal := await _rate_refusal(db, user_id, book_id, runs, words, book)):
        return refusal

    share = [words // len(runs)] * len(runs)
    share[0] += words - sum(share)
    async with db.tx() as conn:
        await conn.executemany(
            "INSERT INTO quote_ledger (user_id, book_id, chapter_index, para_start,"
            " para_end, word_count, public_domain) VALUES (?,?,?,?,?,?,?)",
            [(user_id, book_id, ch, start, end, w, book["is_public_domain"])
             for (ch, start, end), w in zip(runs, share, strict=True)])
    return None
