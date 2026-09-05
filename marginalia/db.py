"""aiosqlite connection, pragmas, and a PRAGMA user_version migration ladder."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

# Append-only: bump the schema by adding 2: "0002_whatever.sql", never by editing schema.sql.
MIGRATIONS: dict[int, str] = {1: "schema.sql", 2: "migrations/002_cover.sql",
                              3: "migrations/003_roster_message.sql"}

# foreign_keys is OFF BY DEFAULT in SQLite -- without this opt-in every REFERENCES and ON DELETE
# CASCADE in schema.sql is decoration (tests/test_db.py::test_foreign_keys_enforced proves it took).
PRAGMAS: tuple[str, ...] = (
    "journal_mode=WAL",
    "synchronous=NORMAL",
    "foreign_keys=ON",
    "busy_timeout=5000",
)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection = None  # type: ignore[assignment]
        # ONE connection means BEGIN is global to it, so one lock guards tx/run/one/all: a write
        # issued during another coroutine's open tx() JOINS it and is SILENTLY DISCARDED on
        # rollback. INVARIANT: a tx() body must use the YIELDED conn, never self.run/one/all --
        # re-entering this lock deadlocks the bot permanently.
        # ponytail: one lock per database, second connection w/ own tx if write throughput matters.
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        # isolation_level=None or executescript fires an implicit COMMIT, silently voiding
        # migrate()'s "user_version bumps in the SAME transaction as its DDL" guarantee.
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await self._db.execute(f"PRAGMA {pragma}")

    async def close(self) -> None:
        await self._db.close()

    async def migrate(self) -> int:
        row = await self.one("PRAGMA user_version")
        version = int(row[0]) if row else 0
        for target in sorted(MIGRATIONS):
            if target <= version:
                continue
            ddl = (Path(__file__).parent / MIGRATIONS[target]).read_text()
            # user_version takes NO bound parameter (`= ?` is a syntax error); the int() cast is
            # what makes interpolation safe. Do not "fix" it into a placeholder.
            await self._db.executescript(
                f"BEGIN;\n{ddl}\nPRAGMA user_version = {int(target)};\nCOMMIT;"
            )
            version = target
        return version

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self._db.execute("BEGIN")
            try:
                yield self._db
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            else:
                await self._db.execute("COMMIT")

    async def one(self, sql: str, *a: Any) -> aiosqlite.Row | None:
        async with self._lock, self._db.execute(sql, a) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, *a: Any) -> list[aiosqlite.Row]:
        async with self._lock, self._db.execute(sql, a) as cur:
            return list(await cur.fetchall())

    async def run(self, sql: str, *a: Any) -> aiosqlite.Cursor:
        async with self._lock:
            return await self._db.execute(sql, a)
