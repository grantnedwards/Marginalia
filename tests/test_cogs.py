"""Every cog in COGS constructs and registers its commands -- offline, no token.

Nothing else in the suite builds a Cog, so a broken constructor or a typo'd
module name reaches production as a WARNING and a silently reduced command sync.
"""


import pytest

from marginalia import bot as botmod
from marginalia.config import Config
from marginalia.db import Database

CLUB = {"help", "join", "leave", "roster", "nominate", "cycle-open", "cycle-close",
        "cycle-book", "ballot", "ballot-result", "status"}
READING = {"schedule", "next", "pace", "progress", "progress set", "progress show", "meeting",
           "library", "dnf", "mystats"}
QUOTE = {"quote", "find", "passage", "ingest", "ingest-library", "purge_book"}
TREE = CLUB | READING | QUOTE
TOP_LEVEL = len(TREE) - 2  # 'progress set'/'progress show' hang off the progress group


class PastTheGuard(RuntimeError):
    """Raised by a stub Discord call that only a passing guard check can reach."""


@pytest.fixture
async def loaded(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await db.migrate()
    bot = botmod.Marginalia(Config("token", 1, 7, str(tmp_path / "m.db")), db)
    for name in botmod.COGS:
        await bot.load_extension(name)
    yield bot
    await db.close()


async def test_loading_every_cog_registers_the_whole_command_tree(loaded):
    assert set(loaded.cogs) == {"Club", "Reading", "Quote"}
    # A load error swallowed into a warning shows up here as a SHORT list.
    assert {c.qualified_name for c in loaded.tree.walk_commands()} == TREE
    assert len(loaded.tree.get_commands()) == TOP_LEVEL
    for cog in loaded.cogs.values():
        for attr, want in (("cfg", loaded.cfg), ("db", loaded.db), ("bot", loaded)):
            if hasattr(cog, attr):
                assert getattr(cog, attr) is want, f"{cog.qualified_name}.{attr}"


async def test_cycle_open_refuses_before_creating_a_role(loaded):
    """Every refusal -- wrong channel, unknown nomination, nothing named -- happens
    BEFORE create_role, so a misfire never leaves an orphan role in the server."""
    club, sent = loaded.cogs["Club"], []

    class Stub:  # its own .guild and .response, so the whole call is one object
        guild_id = 1

        def __init__(self) -> None:
            self.guild = self.response = self

        async def create_role(self, **kw):
            raise PastTheGuard("a refused /cycle-open reached create_role")

        async def send_message(self, text, **kw):
            sent.append(text)

    interaction = Stub()
    interaction.channel_id = loaded.cfg.channel_id + 1
    await club.cycle_open.callback(club, interaction, nomination=1)
    assert sent and f"<#{loaded.cfg.channel_id}>" in sent[0]

    interaction.channel_id = loaded.cfg.channel_id
    await club.cycle_open.callback(club, interaction, nomination=1)  # no such nomination
    assert "no nomination #1" in sent[-1]
    await club.cycle_open.callback(club, interaction)  # nothing named at all
    assert "name a nomination" in sent[-1]
    await club.cycle_open.callback(club, interaction, nomination=1, month="2026-13")
    assert "not a month" in sent[-1]
    assert (await loaded.db.one("SELECT COUNT(*) FROM cohorts"))[0] == 0

    await loaded.db.run("INSERT INTO nominations (id, guild_id, cycle_month, title,"
                        " nominated_by) VALUES (1, 1, '2026-09', 'Ulysses', 5)")
    with pytest.raises(PastTheGuard):  # positive control: a valid call proceeds to the role
        await club.cycle_open.callback(club, interaction, nomination=1)
