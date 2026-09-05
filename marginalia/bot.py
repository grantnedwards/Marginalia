"""The client: intents, setup_hook, the reminder loop, and reminder delivery.

reminders.py takes its clock and delivery as parameters (that is what makes it
testable with no token), so the tasks.loop wrapper and `deliver` live here.
Logging names ids, kinds and counts only: never the token, never `payload`,
never book text.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
import aiosqlite
import discord
from discord.ext import commands, tasks

from . import timefmt
from .config import Config
from .db import Database
from .reminders import Poller

log = logging.getLogger("marginalia.bot")

COGS = ("marginalia.cogs.club", "marginalia.cogs.reading", "marginalia.cogs.quote")
REAP_EVERY = 20  # ticks; 20 * 30s = every 10 minutes, matching reminders.STRANDED_SECS
RESTART_BASE, RESTART_CAP, RESTART_RESET, RESTART_GIVE_UP = 5, 60, 600, 6
WORDING = {  # the five kinds schema.sql's CHECK allows
    "unlock": "is open", "T-24h": "is due tomorrow", "T-1h": "is due in an hour",
    "meeting-T-24h": "meeting is tomorrow", "meeting-T-1h": "meeting starts in an hour",
}  # fmt: skip


class Marginalia(commands.Bot):
    def __init__(self, cfg: Config, db: Database) -> None:
        # members is PRIVILEGED: without the portal toggle guild.members and
        # role.members read EMPTY with no error, so the roster silently says zero.
        intents = discord.Intents(guilds=True, members=True, guild_scheduled_events=True)
        # Explicit, not merely absent: interactions carry their own payload so message
        # content is never read, and presences is the priciest intent for nothing here.
        intents.message_content = False
        intents.presences = False
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.cfg = cfg
        self.db = db
        self.poller = Poller(db, self.deliver)
        self._ticks = 0
        # Here on self.poll_reminders, NOT at class scope: Loop.__get__ does not copy
        # _valid_exception onto the per-instance copy (measured in ext/tasks), so at
        # class scope a transient sqlite lock stops being survivable.
        self.poll_reminders.add_exception_type(aiosqlite.Error, aiohttp.ClientError)
        self._fails, self._last_fail = 0, 0.0

    async def setup_hook(self) -> None:
        # Here, NOT on_ready: on_ready fires again on every reconnect and the cap is
        # 200 command creates per day per guild, so syncing there eventually 429s.
        for name in COGS:
            # No except: a swallowed ExtensionError syncs a REDUCED tree and the bot
            # looks healthy while commands are simply gone. Refusing to boot is louder.
            await self.load_extension(name)
            mod = self.extensions[name]
            # DYNAMIC_ITEMS holds DynamicItem CLASSES -- an instance is a TypeError.
            for view in getattr(mod, "PERSISTENT_VIEWS", ()):
                self.add_view(view)
            self.add_dynamic_items(*getattr(mod, "DYNAMIC_ITEMS", ()))
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)  # guild-scoped: appears instantly, one quota
        self.poll_reminders.start()

    async def deliver(self, row: aiosqlite.Row) -> None:
        """Send one due reminder: the ONE Poller callback, dispatching on kind.

        Every kind ends in exactly one channel.send, so the AllowedMentions rule and
        timefmt.when() cannot be forgotten on one branch and not another.
        """
        cp = await self.db.one(
            "SELECT c.id, c.label, c.thread_id, c.due_at_utc, h.channel_id, h.role_id,"
            " h.cycle_month FROM checkpoints c JOIN cohorts h ON h.id = c.cohort_id"
            " WHERE c.id = ?",
            row["checkpoint_id"],
        )
        if cp is None:  # FK cascade makes this impossible; fail loudly if it happens
            raise LookupError(f"reminder {row['id']} has no checkpoint")
        kind = row["kind"]
        # T-24h/T-1h go INTO the thread; the unlock CREATES it, so it is announced
        # in the cohort channel.
        cid = cp["channel_id"] if kind == "unlock" else (cp["thread_id"] or cp["channel_id"])
        channel = self.get_channel(cid) or await self.fetch_channel(cid)
        link = ""
        if kind == "unlock":
            from .progress import open_thread

            link = f"<#{await open_thread(self.db, cp, channel)}> "
        role_id = cp["role_id"]
        text = (
            f"{f'<@&{role_id}> ' if role_id else ''}{link}**{cp['label']}** "
            f"{WORDING.get(kind, kind)} -- {timefmt.when(cp['due_at_utc'])}"
        )
        # roles=[...] EXPLICITLY: the default parses users only, so a role mention
        # otherwise renders blue and pings NOBODY.
        await channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=role_id)] if role_id else False,
            ),
        )

    @tasks.loop(seconds=30)
    async def poll_reminders(self) -> None:
        now = int(time.time())
        if self._ticks % REAP_EVERY == 0:  # tick 0 == once at startup: crash recovery
            moved = await self.poller.reap(now)
            if moved:
                log.info("reap moved %d stranded reminders", moved)
        self._ticks += 1
        sent, skipped = await self.poller.tick(now)
        if sent or skipped:
            log.info("tick sent=%d skipped=%d", sent, skipped)

    @poll_reminders.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    @staticmethod
    def _restart_plan(fails: int, since_last_fail: float) -> tuple[int, float | None]:
        """(new consecutive-failure count, seconds to wait) -- None means STOP restarting.

        Staying dead after RESTART_GIVE_UP is deliberate: the heartbeat goes stale and
        deploy/watchdog.sh restarts the container. See docs/SPEC.md for the tradeoff.
        """
        fails = 1 if since_last_fail >= RESTART_RESET else fails + 1
        if fails >= RESTART_GIVE_UP:
            return fails, None
        return fails, min(RESTART_CAP, RESTART_BASE * 2 ** (fails - 1))

    @poll_reminders.error
    async def _loop_died(self, exc: BaseException) -> None:
        # Without this handler ONE exception kills the loop permanently while the
        # process stays up answering commands: process liveness is not scheduler
        # liveness, which is why the watchdog reads Poller's heartbeat row.
        now = time.monotonic()
        self._fails, delay = self._restart_plan(self._fails, now - self._last_fail)
        self._last_fail = now
        if delay is None:
            log.critical("reminder loop failed %d times, NOT restarting", self._fails, exc_info=exc)
            return
        log.error("reminder loop died, restart #%d in %gs", self._fails, delay, exc_info=exc)
        await asyncio.sleep(delay)
        self.poll_reminders.restart()
