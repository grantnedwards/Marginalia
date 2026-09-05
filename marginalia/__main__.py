"""`python -m marginalia`: config -> Database -> migrate -> gateway, db closed on exit."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys

import discord

from .bot import Marginalia
from .config import Config, ConfigError, load
from .db import Database

log = logging.getLogger("marginalia")

# Exit codes, so a `docker logs` reader and a restart loop can tell the failures apart.
EXIT_CONFIG, EXIT_DB, EXIT_TOKEN, EXIT_INTENTS, EXIT_FORBIDDEN = 2, 3, 4, 5, 6


async def _run(cfg: Config) -> None:
    db = Database(cfg.db_path)
    await db.connect()
    await db.migrate()
    async with Marginalia(cfg, db) as bot:  # closes the gateway on the way out
        try:
            await bot.start(cfg.token)
        finally:
            await db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load()  # at CALL time, never at import time
    except ConfigError as exc:
        log.error("%s", exc)  # the message names the missing vars and never the token
        return EXIT_CONFIG
    try:
        # asyncio.run, not bot.run(): connect/migrate are awaits that must land
        # before the gateway, and SIGINT then unwinds _run's finally so the db
        # closes. There is deliberately no SIGTERM handler: PID 1 in a container
        # gets no default one, so the kernel DISCARDS SIGTERM and `docker stop`
        # SIGKILLs at the 10s deadline with `finally` never reached (measured:
        # 9.9s, exit 137, every stop). The fix is `--stop-signal SIGINT` on the
        # container (compose file / Unraid template), not a handler here.
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
    except (OSError, sqlite3.Error) as exc:
        # One clean line, like ConfigError above. Unhandled, this surfaced as a
        # traceback plus a misleading "Event loop is closed" from loop teardown.
        log.error("cannot open the database at %s: %s", cfg.db_path, exc)
        return EXIT_DB
    # The three first-boot failures that used to be a traceback. Each names the fix.
    except discord.LoginFailure:
        log.error("Discord rejected the token (401). Developer Portal -> Bot -> Reset Token,"
                  " then put the new value in DISCORD_TOKEN and restart.")
        return EXIT_TOKEN
    except discord.PrivilegedIntentsRequired:
        log.error("The Server Members intent is not enabled for this application. Developer"
                  " Portal -> Bot -> Privileged Gateway Intents -> SERVER MEMBERS INTENT -> on"
                  " -> Save Changes, then restart.")
        return EXIT_INTENTS
    except discord.Forbidden as exc:
        log.error("Discord refused a request during startup (%s). Usually GUILD_ID names a"
                  " server the bot is not in, or the invite link lacked the"
                  " applications.commands scope. Re-check both, then restart.", exc.text)
        return EXIT_FORBIDDEN
    return 0


if __name__ == "__main__":
    sys.exit(main())
