"""`python -m marginalia`: config -> Database -> migrate -> gateway, db closed on exit."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys

from .bot import Marginalia
from .config import Config, ConfigError, load
from .db import Database

log = logging.getLogger("marginalia")


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
        return 2
    try:
        # asyncio.run, not bot.run(): connect/migrate are awaits that must land
        # before the gateway, and SIGINT then unwinds _run's finally so the db
        # closes. There is deliberately no SIGTERM handler: PID 1 in a container
        # gets no default one, so the kernel DISCARDS SIGTERM and `docker stop`
        # SIGKILLs at the 10s deadline with `finally` never reached (measured:
        # 9.9s, exit 137, every stop). The fix is `stop_signal: SIGINT` in
        # deploy/docker-compose.yml, not a handler here.
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
    except (OSError, sqlite3.Error) as exc:
        # One clean line, like ConfigError above. Unhandled, this surfaced as a
        # traceback plus a misleading "Event loop is closed" from loop teardown.
        log.error("cannot open the database at %s: %s", cfg.db_path, exc)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
