"""/quote, /find, /passage, plus organizer /ingest and /purge_book. Discord surface only:
library.py owns the spoiler gate, the FTS5 query, the locator, the bars and the budget.

EVERY book-text reply is EPHEMERAL, and that is the real spoiler defence: an ephemeral
reply generates no push notification, and a mobile push preview renders spoiler-barred
text RAW on the lock screen. A refusal never echoes the question and never describes what
lies beyond the ceiling -- no chapter title, no match count; the refusal itself spoils.
"""

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from marginalia import calibre, library
from marginalia.cogs import _views
from marginalia.db import Database
from marginalia.epub import EncryptedEpubError

MAX_LEN = 2000  # Discord rejects a longer message; truncate rather than take a 400
TRUNCATED = "\n... (truncated)"

# INTERFACE DRIFT, minimal: the `books` row only -- no book text, no text-table name
# invariant C2 greps for. LIMIT 25 is Discord's choice cap, and keeps ac under 3s.
_BOOKS = ("SELECT id, title, author FROM books WHERE ingested_at IS NOT NULL"
          " ORDER BY id DESC LIMIT 25")
_BOOK = "SELECT title, author, chapter_count, word_count FROM books WHERE id = ?"
_PURGE = "DELETE FROM books WHERE id = ?"

NO_BOOK = "I do not have that book."
# Identical whether the phrase is absent or beyond the ceiling: a difference is an oracle.
NOTHING = "Nothing I can show you matches that."
LOCATOR_ONLY = ("Citations without the text cost you nothing -- `/find` is often what you"
                " actually wanted:")
ATTEST = ("I only ingest a copy you own. Re-run with the DRM-free attestation set to"
          " True if that is the case.")
# No hint about circumvention, ever.
DRM = ("That file is encrypted, so it cannot be parsed. Please supply a DRM-free EPUB"
       " that you own.")
IN_USE = "That book is still attached to a reading cohort, so I did not delete it."
NO_LIBRARY = ("No Calibre library is configured, so there is nothing to pick from. Set"
              " CALIBRE_LIBRARY and bind-mount the library read-only, or upload the file"
              " with /ingest instead.")
GONE = "Calibre still lists that book but its EPUB is not on disk any more."


@dataclass(frozen=True)
class Reply:
    """Always an ``embed``, never message content: a passage printed twice is two bars."""
    ephemeral: bool = True
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None
    file: discord.File | None = None


def _fit(blocks: list[str], cap: int = MAX_LEN) -> str:
    """Join blocks under Discord's limit, dropping WHOLE blocks off the end. Never
    mid-block: clipping inside `||...||` leaves the bars unclosed, and Discord then
    renders the tail in plaintext."""
    out: list[str] = []
    for b in blocks:
        if len("\n\n".join([*out, b])) > cap - len(TRUNCATED):
            return "\n\n".join(out) + TRUNCATED
        out.append(b)
    return "\n\n".join(out)


def quote_card(bars: list[str], byline: str, cover: discord.File | None = None) -> discord.Embed:
    """A served passage as a card: each locator above its own bar (library.spoiler's order,
    so a reader decides before revealing), fitted to the DESCRIPTION cap, not the 2000."""
    return _views.quote_embed(_fit(bars, _views.DESC_MAX), byline, cover)


class LocatorOnly(discord.ui.View):
    """One tap instead of retyping the command with a flag. It carries the citations built
    with the refusal, so a click reads no database, never re-enters serve(), and cannot
    reach library.budget() -- there is no path for a second charge to come from."""

    def __init__(self, locators: list[str], byline: str) -> None:
        super().__init__(timeout=600)
        self.locators, self.byline = locators, byline

    @discord.ui.button(label="Locator only", style=discord.ButtonStyle.secondary)
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        # A button on an ephemeral message may only edit that message; view=None retires it.
        await interaction.response.edit_message(
            embed=_views.find_embed(self.locators, self.byline), view=None)


def _refuse(reason: str) -> Reply:
    return Reply(embed=_views.refusal_embed(reason))


async def _citations(db: Database, book_id: int, locs: list[str], byline: str) -> Reply:
    # cover_file() derives the filename from the media type, so attachment://NAME matches.
    file = _views.cover_file(await library.cover(db, book_id))
    return Reply(embed=_views.find_embed(locs, byline, file), file=file)


async def serve(
    db: Database, *, guild_id: int, channel_id: int, user_id: int, book_id: int,
    query: str, k: int = 1, text: bool = True, share: bool = False,
) -> Reply:
    """Build one reply, as content or as an embed but never as both."""
    book = await db.one(_BOOK, book_id)
    if book is None:
        return _refuse(NO_BOOK)
    ceil = await library.ceiling(db, guild_id, channel_id, user_id, book_id)
    # FAIL CLOSED: chapter 0 means refuse (unknown user, unmapped channel), not "no limit".
    if ceil.chapter < 1:
        return _refuse(library.REASONS.get(ceil.reason, library.REASONS["fail_closed"]))
    hits = await library.search(db, book_id, query, ceil, k)
    byline = f"{book['title']} by {book['author']}"
    if not hits:
        return _refuse(NOTHING)
    locs = [library.locator(h, book["title"], book["author"]) for h in hits]
    if not text:
        return await _citations(db, book_id, locs, byline)  # no book text, nothing to charge
    # budget() WRITES quote_ledger, so it is called EXACTLY ONCE and HERE -- after we
    # know what we would send, before sending. Twice double-charges; after the send, a
    # crash serves a free quote.
    refusal = await library.budget(
        db, user_id, book_id, sum(len(h.text.split()) for h in hits),
        [(h.chapter, h.para) for h in hits])
    if refusal:
        # A refusal writes nothing, and LocatorOnly is handed the strings, not the arguments.
        return Reply(embed=_views.refusal_embed(f"{refusal}\n{LOCATOR_ONLY}"),
                     view=LocatorOnly(locs, byline))
    # spoiler() keeps the locator OUTSIDE the bars and escapes a literal ||. Keep both.
    bars = [library.spoiler(loc, h.text) for loc, h in zip(locs, hits, strict=True)]
    file = _views.cover_file(await library.cover(db, book_id))
    return Reply(embed=quote_card(bars, byline, file), ephemeral=not share, file=file)


async def take(db: Database, path: str, attested: bool, source: str = "uploaded file") -> str:
    if not attested:
        return ATTEST
    try:
        return (f"Ingested book #{await library.ingest(db, path)}. The {source} has been"
                " deleted -- only the parsed text is kept.")
    except EncryptedEpubError:
        return DRM
    finally:
        # ingest() already deleted it on success; off-loop because the poller shares it.
        await asyncio.to_thread(Path(path).unlink, True)


async def take_from_calibre(db: Database, lib: str, book_id: int, attested: bool,
                            tmp: Path) -> str:
    """Ingest a book out of the read-only Calibre library.

    THE COPY IS LOAD-BEARING: library.ingest() DELETES the file it is handed, so passing
    the library path straight in would remove the book from Calibre. The bind mount is
    read-only as a second guard, and this copy is the first.
    """
    if not attested:
        return ATTEST
    try:
        found = await asyncio.to_thread(calibre.entry, lib, book_id)
    except calibre.CalibreError as exc:
        return f"I could not read the Calibre library: {exc}"
    if found is None:
        return GONE
    await asyncio.to_thread(shutil.copyfile, found.path, tmp)
    return await take(db, str(tmp), True, source=f"copy of {found.path.name}")


async def purge(db: Database, book_id: int, confirm: bool) -> str:
    """Honour a takedown in one command. Reports exactly what went."""
    book = await db.one(_BOOK, book_id)
    if book is None:
        return NO_BOOK
    what = (f"{book['title']} by {book['author']} -- {book['chapter_count']} chapters,"
            f" {book['word_count']} words, all stored text, its search index and every"
            " quote-ledger entry for it")
    if not confirm:
        return f"This would permanently delete {what}. Re-run with confirm: True."
    try:
        async with db.tx() as conn:
            await conn.execute(_PURGE, (book_id,))
    except aiosqlite.IntegrityError:
        return IN_USE  # the cohort FK is RESTRICT: close the cohort first
    return f"Deleted {what}."


class Quote(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self) -> Database:
        return self.bot.db  # type: ignore[attr-defined]

    async def book_ac(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        rows = await self.db.all(_BOOKS)
        low = current.lower()
        return [app_commands.Choice(name=f"{r['title']} - {r['author']}"[:100], value=r["id"])
                for r in rows if low in r["title"].lower()]

    async def _book(self, interaction: discord.Interaction, book: int | None) -> int | None:
        """No `book:` means the one the club is reading now. Metadata only (INTERFACE DRIFT
        as above): the cohort's book_id, never text."""
        if book is not None:
            return book
        row = await self.db.one("SELECT book_id FROM cohorts WHERE guild_id = ? AND status IN"
                                " ('open','active') ORDER BY id DESC LIMIT 1",
                                interaction.guild_id or 0)
        return int(row[0]) if row else None

    async def _reply(self, interaction: discord.Interaction, book: int | None,
                     **kw: object) -> None:
        """Every reply routes here so ephemeral and AllowedMentions are set once. serve()
        returns an embed, never message content; opts() drops the None-valued kwargs."""
        book_id = await self._book(interaction, book)
        if book_id is None:
            await interaction.response.send_message(
                "No cycle is open, so there is no book to search. Name one with `book:`.",
                ephemeral=True)
            return
        r = await serve(
            self.db, guild_id=interaction.guild_id or 0, channel_id=interaction.channel_id or 0,
            user_id=interaction.user.id, book_id=book_id, **kw)  # type: ignore[arg-type]
        await interaction.response.send_message(
            ephemeral=r.ephemeral, allowed_mentions=discord.AllowedMentions.none(),
            **_views.opts(embed=r.embed, view=r.view, file=r.file))

    _DESCRIBE = dict(
        query="A word or phrase to look for in the text",
        book="Which book. Default: the one the club is reading now",
        share="Post it in the channel for everyone (default: only you see it)",
    )

    @app_commands.command(description="Quote the best passage you are allowed to see.")
    @app_commands.describe(**_DESCRIBE)
    @app_commands.autocomplete(book=book_ac)
    async def quote(self, interaction: discord.Interaction, query: str,
                    book: int | None = None, share: bool = False) -> None:
        await self._reply(interaction, book, query=query, k=1, share=share)

    @app_commands.command(description="Citations only -- no text, no allowance spent.")
    @app_commands.describe(query=_DESCRIBE["query"], book=_DESCRIBE["book"])
    @app_commands.autocomplete(book=book_ac)
    async def find(self, interaction: discord.Interaction, query: str,
                   book: int | None = None) -> None:
        await self._reply(interaction, book, query=query, k=3, text=False)

    @app_commands.command(description="Quote up to three matching passages.")
    @app_commands.describe(**_DESCRIBE)
    @app_commands.autocomplete(book=book_ac)
    async def passage(self, interaction: discord.Interaction, query: str,
                      book: int | None = None, share: bool = False) -> None:
        await self._reply(interaction, book, query=query, k=3, share=share)

    async def calibre_ac(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Titles from the Calibre library. Blocking sqlite + stat calls go to a thread,
        because autocomplete runs on the event loop the reminder poller shares."""
        lib = self.bot.cfg.calibre_library  # type: ignore[attr-defined]
        if not lib:
            return []
        try:
            found = await asyncio.to_thread(calibre.search, lib, current)
        except calibre.CalibreError:
            return []  # a missing mount must not make the picker hang or error
        return [app_commands.Choice(name=f"{e.title} - {e.author}"[:100], value=e.book_id)
                for e in found]

    @app_commands.command(name="ingest-library",
                          description="Organizer: add a book from the Calibre library.")
    @app_commands.describe(book="Start typing a title from the Calibre library",
                           i_own_a_drm_free_copy="Confirm this is your own DRM-free copy")
    @app_commands.autocomplete(book=calibre_ac)
    @app_commands.default_permissions()
    async def ingest_library(self, interaction: discord.Interaction, book: int,
                             i_own_a_drm_free_copy: bool) -> None:
        # DEFER FIRST: copy + unzip + parse is far past the 3-second interaction deadline.
        await interaction.response.defer(ephemeral=True, thinking=True)
        lib = self.bot.cfg.calibre_library  # type: ignore[attr-defined]
        if not lib:
            await interaction.followup.send(NO_LIBRARY, ephemeral=True)
            return
        tmp = Path(gettempdir()) / f"mgl-cal-{interaction.id}.epub"
        await interaction.followup.send(
            await take_from_calibre(self.db, lib, book, i_own_a_drm_free_copy, tmp),
            ephemeral=True)

    @app_commands.command(description="Organizer: add an EPUB you own so members can quote it.")
    @app_commands.describe(epub="The .epub file (DRM-free). It is deleted after parsing",
                           i_own_a_drm_free_copy="Confirm this is your own DRM-free copy")
    @app_commands.default_permissions()
    async def ingest(self, interaction: discord.Interaction, epub: discord.Attachment,
                     i_own_a_drm_free_copy: bool) -> None:
        # DEFER FIRST: unzip + parse is far past the 3-second interaction deadline.
        await interaction.response.defer(ephemeral=True, thinking=True)
        path = Path(gettempdir()) / f"mgl-{interaction.id}.epub"
        await epub.save(path)
        await interaction.followup.send(
            await take(self.db, str(path), i_own_a_drm_free_copy), ephemeral=True)

    @app_commands.command(description="Organizer: delete a book and everything quoted from it.")
    @app_commands.describe(book="Which book to delete", confirm="Set True to really delete it")
    @app_commands.default_permissions()
    @app_commands.autocomplete(book=book_ac)
    async def purge_book(self, interaction: discord.Interaction, book: int,
                         confirm: bool = False) -> None:
        await interaction.response.send_message(
            await purge(self.db, book, confirm), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quote(bot))
