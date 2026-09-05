"""Durable reminder poller: reminders are DATA (rows, no in-memory timer), so restart survival is
definitional. Clock and delivery are injected; bot.py owns the tasks.loop wrapper."""

import os
from collections.abc import Awaitable, Callable

import aiosqlite

from .db import Database

STRANDED_SECS = 600
MAX_ATTEMPTS = 3
BEAT = "reminders"


def _terminal(exc: BaseException) -> bool:
    """NEVER retry these: repeating an invalid request burns the 10k-per-10min ceiling and ends in a
    Cloudflare IP ban on the host. Everything else stays 'sending' for reap()."""
    # Lazy import so this module and its whole test suite load with no discord and no token.
    import discord

    return isinstance(exc, (discord.Forbidden, discord.NotFound))


class Poller:
    def __init__(self, db: Database, deliver: Callable[[aiosqlite.Row], Awaitable[None]]) -> None:
        self._db = db
        self._deliver = deliver

    async def tick(self, now: int) -> tuple[int, int]:
        """Deliver everything due. Returns (sent, skipped)."""
        sent = skipped = 0
        due = await self._db.all(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at", now
        )
        for row in due:
            # grace_secs < 0 means INFINITE grace -- invert this and a thread unlock is permanently
            # skipped after an outage. Only a bounded window can expire.
            if 0 <= row["grace_secs"] < now - row["due_at"]:
                cur = await self._db.run(
                    "UPDATE reminders SET status = 'skipped' WHERE id = ? AND status = 'pending'",
                    row["id"],
                )
                if cur.rowcount == 1:
                    skipped += 1
                continue

            # THE CLAIM: status='pending' + rowcount == 1. Gating on the STATUS column rather than
            # any timestamp comparison is what makes a backward NTP correction harmless.
            cur = await self._db.run(
                "UPDATE reminders SET status = 'sending', claimed_at = ?, attempts = attempts + 1"
                " WHERE id = ? AND status = 'pending'",
                now,
                row["id"],
            )
            if cur.rowcount != 1:
                continue

            # Deliver THEN mark: at-least-once, a duplicate ping beats a missed checkpoint.
            try:
                await self._deliver(row)
            except Exception as exc:
                await self._db.run(
                    "UPDATE reminders SET status = ?, last_error = ? WHERE id = ?",
                    "failed" if _terminal(exc) else "sending",
                    repr(exc)[:500],
                    row["id"],
                )
                continue
            await self._db.run(
                "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?", now, row["id"]
            )
            sent += 1

        # EVERY tick, including a zero-work one: process liveness is not scheduler liveness, and an
        # external watchdog reads this row.
        await self._db.run(
            "INSERT INTO heartbeat (name, beat_at, tick_count, pid, detail) VALUES (?,?,1,?,?)"
            " ON CONFLICT(name) DO UPDATE SET beat_at = excluded.beat_at,"
            " tick_count = heartbeat.tick_count + 1, pid = excluded.pid, detail = excluded.detail",
            BEAT,
            now,
            os.getpid(),
            f"sent={sent} skipped={skipped}",
        )
        return sent, skipped

    async def reap(self, now: int) -> int:
        """Crash recovery: unstrand 'sending' rows a kill -9 left behind. Returns rows moved."""
        cutoff = now - STRANDED_SECS
        # Retire the exhausted ones FIRST, or the requeue below picks them back up.
        failed = await self._db.run(
            "UPDATE reminders SET status = 'failed' WHERE status = 'sending'"
            " AND claimed_at < ? AND attempts >= ?",
            cutoff,
            MAX_ATTEMPTS,
        )
        requeued = await self._db.run(
            "UPDATE reminders SET status = 'pending' WHERE status = 'sending' AND claimed_at < ?",
            cutoff,
        )
        return failed.rowcount + requeued.rowcount
