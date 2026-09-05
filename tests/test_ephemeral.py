"""What the cogs actually hand Discord: ephemerality and AllowedMentions.

The other suites assert on what ``serve()`` RETURNS. Nothing asserted what
``_reply()`` passes to ``interaction.response.send_message``, so flipping
``ephemeral=False``, ``AllowedMentions.all()`` or ``quiet=False`` used to pass the
whole suite. Ephemeral is the real spoiler defence: an ephemeral reply raises no
push notification, and a mobile push preview renders ``||spoiler||`` text RAW on
the lock screen.

The cogs are loaded unmodified; the Interaction is a recorder, not a sender.
"""

import discord
import pytest

from marginalia import bot as botmod
from marginalia.cogs import quote as quotemod
from marginalia.config import Config
from marginalia.db import Database

G, C, U = 1545535072151670824, 1545535072151670825, 1545535072151670826
ROLE_ID = 1545535072151670827
SECRET, TITLE2 = "sister dies", "Belowdecks"


class Role:  # what guild.get_role returns; AllowedMentions never inspects it
    id = ROLE_ID
    mention = f"<@&{ROLE_ID}>"


class Recorder:
    """An Interaction that RECORDS the reply it was handed instead of sending it.

    ``.sent`` is [(content, kwargs)]; the cog cannot tell the difference, and no
    token, gateway or HTTP session is involved.
    """

    def __init__(self, *, channel_id: int = C, role: Role | None = None) -> None:
        self.guild_id, self.channel_id = G, channel_id
        self.id = U  # .user is self, so this is interaction.user.id
        self.user = self
        self.guild = self
        self.response = self
        self.role = role
        self.sent: list[tuple[str | None, dict]] = []

    # --- user / member ---
    async def add_roles(self, *a, **kw) -> None:
        pass

    async def remove_roles(self, *a, **kw) -> None:
        pass

    # --- guild ---
    def get_role(self, role_id: int) -> Role | None:
        return self.role

    # --- response ---
    async def send_message(self, content: str | None = None, **kw) -> None:
        self.sent.append((content, kw))

    async def defer(self, **kw) -> None:
        self.sent.append((None, kw))

    def last(self) -> tuple[str, dict]:
        assert self.sent, "the command replied to nobody"
        content, kw = self.sent[-1]
        return content or "", kw



@pytest.fixture
async def loaded(tmp_path):
    """A real Bot with all three cogs loaded, plus one two-chapter book at ceiling 1."""
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await db.migrate()
    await db.run("INSERT INTO books (id, title, author, chapter_count, word_count, char_count,"
                 " source_sha256, ingested_at) VALUES (1,'Moby-Dick','Melville',2,1000,600,"
                 "'sha',unixepoch())")
    await db.run("INSERT INTO cohorts (id, guild_id, channel_id, book_id, cycle_month, status,"
                 " role_id) VALUES (1,?,?,1,'2026-09','open',?)", G, C, ROLE_ID)
    await db.run("INSERT INTO cohort_members (cohort_id, user_id) VALUES (1,?)", U)
    await db.run("INSERT INTO channel_policy (channel_id, guild_id, cohort_id) VALUES (?,?,1)",
                 C, G)
    await db.run("INSERT INTO checkpoints (id, cohort_id, idx, label, unit, start_ref, end_ref,"
                 " chapter_ceiling, due_at_utc, tz_id, local_wall)"
                 " VALUES (1,1,1,'Chapter 1','chapter',1,1,1,0,'UTC','2026-09-06 19:00')")
    async with db.tx() as conn:
        for ci, title, body in ((1, "Nantucket", "the white whale ||sounded|| twice"),
                                (2, TITLE2, f"and then the {SECRET} at last")):
            await conn.execute("INSERT INTO chapters (id, book_id, chapter_index, title,"
                               " para_count) VALUES (?,1,?,?,4)", (ci, ci, title))
            await conn.execute(
                "INSERT INTO paragraphs (book_id, chapter_id, chapter_index, para_index, text,"
                " word_count, char_start, char_count) VALUES (1,?,?,1,?,?,?,?)",
                (ci, ci, body, len(body.split()), ci * 100, len(body)))
    bot = botmod.Marginalia(Config("token", G, C, str(tmp_path / "m.db")), db)
    for name in botmod.COGS:
        await bot.load_extension(name)
    yield bot
    await db.close()


def assert_nothing_is_mass_pinged(kw: dict) -> discord.AllowedMentions:
    """No @everyone, no blanket user or role allow -- .all() and the default both fail."""
    am = kw.get("allowed_mentions")
    assert isinstance(am, discord.AllowedMentions), "reply sent without explicit AllowedMentions"
    assert am.everyone is False, "an @everyone in book text would ping the server"
    assert am.users is False
    assert am.roles is not True, "blanket role allow: use roles=[role]"
    return am


async def test_a_book_text_reply_is_ephemeral_and_pings_nobody(loaded):
    """The lock-screen property: /quote hands Discord ephemeral=True, not just returns it."""
    cog, inter = loaded.cogs["Quote"], Recorder()
    await cog.quote.callback(cog, inter, book=1, query="whale", share=False)
    content, kw = inter.last()
    assert kw["ephemeral"] is True, "a public quote push-previews the spoiler text RAW"
    assert_nothing_is_mass_pinged(kw)
    # Bars render only in the description, so the passage lives there, never in content.
    assert r"||the white whale \|\|sounded\|\| twice||" in kw["embed"].description


async def test_a_refusal_is_ephemeral_and_describes_nothing(loaded):
    """The refusal itself must not become the oracle: no query echo, title or count."""
    cog, inter = loaded.cogs["Quote"], Recorder()
    q = f"what happens when the {SECRET} in chapter 2"
    await cog.quote.callback(cog, inter, book=1, query=q, share=False)
    content, kw = inter.last()
    assert kw["ephemeral"] is True
    assert_nothing_is_mass_pinged(kw)
    desc = kw["embed"].description
    assert not content and desc == quotemod.NOTHING  # NOTHING is the embed body, not content
    for leak in (SECRET, TITLE2, q, "chapter"):
        assert leak not in desc
    assert not any(ch.isdigit() for ch in desc)  # no match count, no chapter number


async def test_a_role_mention_is_explicitly_scoped_never_blanket(loaded):
    """/roster is public BY DESIGN and mentions the cohort role, so it is the one reply
    that may allow a mention -- exactly one, named."""
    cog, inter = loaded.cogs["Club"], Recorder(role=Role())
    await cog.roster.callback(cog, inter)
    content, kw = inter.last()
    am = assert_nothing_is_mass_pinged(kw)
    assert isinstance(am.roles, list) and [r.id for r in am.roles] == [ROLE_ID]
    assert Role.mention in content


async def test_club_replies_default_to_ephemeral(loaded):
    """club._reply's `quiet` default is what every non-announcement reply relies on."""
    await loaded.db.run("UPDATE cohorts SET role_id = NULL WHERE id = 1")
    cog, inter = loaded.cogs["Club"], Recorder()
    await cog.join.callback(cog, inter)
    _, kw = inter.last()
    assert kw["ephemeral"] is True
    assert_nothing_is_mass_pinged(kw)


async def test_personal_stats_are_ephemeral(loaded):
    """/pace, /mystats and /progress show are per-member numbers. The anti-features
    boundary (no leaderboards, no shaming by omission) is only real if they stay private."""
    cog = loaded.cogs["Reading"]
    await loaded.db.run("INSERT INTO member_progress (cohort_id, user_id, chapter_index)"
                        " VALUES (1,?,1)", U)
    for name in ("pace", "mystats", "progress_show"):
        inter = Recorder()
        await getattr(cog, name).callback(cog, inter)
        _, kw = inter.last()
        assert kw["ephemeral"] is True, f"/{name} is personal, so it must stay private"


async def test_a_public_share_is_opt_in_per_invocation_and_keeps_the_locator_outside(loaded):
    """share=True is the ONLY way a quote goes public, and even then the citation sits
    outside the bars so a preview leaks a reference, never the prose."""
    await loaded.db.run("UPDATE books SET is_public_domain = 1")  # dodge the abutment rule
    cog = loaded.cogs["Quote"]
    private = Recorder()
    await cog.quote.callback(cog, private, book=1, query="whale", share=False)
    assert private.last()[1]["ephemeral"] is True  # control: the default stays private

    public = Recorder()
    await cog.quote.callback(cog, public, book=1, query="whale", share=True)
    content, kw = public.last()
    assert kw["ephemeral"] is False
    assert_nothing_is_mass_pinged(kw)
    # The passage is printed ONCE, in the description: content stays empty, so two bars
    # here (not four) prove it was not duplicated into the message body as well.
    desc = kw["embed"].description
    assert not content  # the passage is in the embed only, never duplicated into content
    locator, _, barred = desc.partition("\n")
    assert locator.startswith("Moby-Dick by Melville - ch. 1") and "||" not in locator
    assert barred == r"||the white whale \|\|sounded\|\| twice||"
    assert desc.count("||") == 2  # bars opened once and closed once
