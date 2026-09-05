# marginalia on unRAID

Docker, not systemd: unRAID runs its OS from RAM off a USB stick, so
`/etc/systemd/system/` does not survive a reboot. Containers do.

## READ THIS FIRST: the database path

The database lives on `/mnt/cache/appdata/marginalia/`. A **direct** cache-SSD
path. Never `/mnt/user/appdata/...`.

1. `/mnt/user` is unRAID's shfs FUSE layer. SQLite in WAL mode takes byte-range
   locks and keeps `-wal` and `-shm` sidecars next to the db, so the mount has
   to behave like a real filesystem. FUSE's locking semantics are not that, and
   this is the widely-documented cause of corrupt Plex/Sonarr/Radarr databases.
2. `/mnt/user` can land writes on the parity array, which spins disks up for
   every small frequent write on a box that should be idling.

Get this wrong and you lose the reading history quietly, weeks later.

## Install

```sh
mkdir -p /mnt/cache/appdata/marginalia
chown 99:100 /mnt/cache/appdata/marginalia   # nobody:users -- the container's uid

git clone <url> /mnt/cache/marginalia        # the REPO. Anywhere on cache, but NOT
cd /mnt/cache/marginalia/deploy              # inside appdata/marginalia -- that dir is
                                             # bind-mounted to /data and holds only the db.
cp ../.env.example .env
vi .env                                      # DISCORD_TOKEN, GUILD_ID, BOOK_CLUB_CHANNEL_ID
chmod 600 .env                               # it holds the bot token
docker compose up -d --build
docker logs -f marginalia
```

`MARGINALIA_DB` is set by the compose file to `/data/marginalia.db` (inside the
container) and does not need to be in `.env`. Leave `ports` alone: the gateway
connection is outbound, nothing listens.

On unRAID that build is native x86_64 and needs no flags. **Building from an
Apple-Silicon Mac is the trap**: the default build would produce an arm64 image
that cannot run on unRAID at all. Cross-build explicitly:

```sh
cd <repo>
docker buildx build --platform linux/amd64 --provenance=false \
  -f deploy/Dockerfile -t marginalia:latest .
```

`--platform` is the load-bearing flag. `--provenance=false` only stops buildx
attaching an attestation manifest, which otherwise makes `docker images`
misreport the size (see below).

The `RUN python - <<'EOF'` self-check needs BuildKit for its heredoc. `#
syntax=docker/dockerfile:1` is line 1 of the Dockerfile, and buildx must be the
builder -- plain legacy `docker build` will not parse it.

### Measured sizes

Built 2026-09-04, buildx 0.37.0 / BuildKit 0.30.0, base `python:3.13-slim`
(Debian trixie, Python 3.13.15):

| | compressed (what unRAID pulls/stores as blobs) | on-disk filesystem (`du -sxm /`) |
|---|---|---|
| linux/amd64 | 54.7 MB | 156 MB |
| linux/arm64 | 55.1 MB | 181 MB |

amd64 cross-build under QEMU took **1m39s** cold, 7s warm; native arm64 took
22s. All 14 runtime pins resolved to prebuilt `manylinux` wheels on **both**
architectures -- lxml included -- so nothing compiled from source and no
toolchain is needed in the image.

Do not trust `docker images` for a cross-built image: it reported 57.4 MB for
amd64 (the compressed total) against 260 MB for the natively-built arm64,
because a cross-platform `--load` into a non-containerd image store loses the
per-layer size metadata. The table above measures both the same way.

## Verify it is SCHEDULING, not merely RUNNING

`docker ps` cannot answer this. A poller task can die while the container stays
up, Discord stays connected and slash commands keep working -- and no reminder
is ever sent again. The heartbeat is the only signal that distinguishes them:

```sh
sqlite3 -readonly /mnt/cache/appdata/marginalia/marginalia.db \
  "SELECT strftime('%s','now') - beat_at AS age_secs, tick_count, detail
     FROM heartbeat WHERE name='reminders'"
```

Under 60 is healthy (the loop ticks every 30s). Over ~600 means the scheduler
is dead regardless of what `docker ps` says.

## Watchdog (User Scripts)

Settings -> User Scripts -> Add New Script -> name it `marginalia-watchdog`,
paste `watchdog.sh`, schedule **Custom** `*/5 * * * *`.

It reads the heartbeat read-only and `docker restart marginalia` if it is older
than 600s. Idempotent, safe at any frequency. The threshold is deliberately
generous: `bot.py` stops restarting its own loop after 6 consecutive failures
(~135s of backoff) *by design*, so the heartbeat can go stale and prove the
scheduler is genuinely dead. A shorter threshold would just fight that.

The script tries the host `sqlite3` first and falls back to `docker exec` into
the container if unRAID has none. Both branches are exercised and working.

It restarts **only a container that is currently running**. `docker stop
marginalia` for maintenance is respected and stays stopped -- a stopped
container's heartbeat goes stale by definition, and without that guard the cron
would quietly undo you within five minutes and you would have to remember to
disable the User Script as well. A container that does not exist at all is the
same non-event. Neither case touches the storm stamp.

It also never judges staleness from a *missing* heartbeat row: the row must
exist first. That is what makes first boot safe -- between container start and
the loop's first tick there is no row, which reads as "nothing to do" rather
than as infinitely stale, so there is no startup restart loop and no grace
period needed.

## What has actually been verified

Built and run 2026-09-04 on a linux/amd64 image, against a real ext4 directory
owned by `99:100` and bind-mounted to `/data` -- i.e. the setup the Install
section produces. Not inferred:

- **Build self-check passes.** `self-check OK: tz Jan-6/Jul-5, discord.py 2.7.1`
  -- CST is UTC-6 and CDT is UTC-5, so the zoneinfo database is real and DST is
  live. A stub tzdata would print equal offsets and fail the build.
- **No config fails loudly and safely.** `docker run` with no environment exits
  **2** with `missing or empty environment variables: DISCORD_TOKEN, GUILD_ID,
  BOOK_CLUB_CHANNEL_ID, MARGINALIA_DB` -- one clean log line, no traceback, and
  the token is never echoed. Importing the package with an empty environment
  still succeeds, so the failure is at call time, not import time.
- **Migrations run before the gateway.** `PRAGMA user_version` = 1,
  `journal_mode` = `wal`, `integrity_check` = ok. 13 logical tables + 4 fts5
  shadow tables + `sqlite_sequence` = 18; 15 explicit indexes + 8
  UNIQUE-constraint autoindexes = 23; 3 triggers. All of it lands *before* the
  bot tries to log in, so a bad token still leaves a correct schema behind.
- **Non-root really is non-root.** `id` inside the container is
  `uid=99 gid=100(users)`, and it creates and unlinks files in `/data`.
  Every file it writes is owned `99:100` on the host side.
- **WAL sidecars land on the host.** `marginalia.db-wal` and `-shm` appear next
  to the db in the bind mount while the container is live, owned `99:100`. They
  are *absent* after a clean stop, which is correct -- SQLite checkpoints and
  removes them on close. Seeing no `-wal` on a stopped bot is not a fault.
- **Watchdog, end to end.** Six cases, both read branches:
  running + stale (5001s) -> restart issued, `StartedAt` advanced, while
  `docker ps` said "Up" throughout; running + fresh (1s) -> no restart;
  repeat run inside the window -> `restarted recently, holding off`, no
  restart; **stopped** + stale -> stays down, no stamp written; container
  absent -> exit 0, no error; running with the heartbeat row deleted -> `no
  heartbeat row yet`, no restart. The db's mtime is unchanged by a watchdog
  read, so the monitor cannot corrupt what it monitors.
- **`docker stop` needed `stop_signal: SIGINT`, and now shuts down cleanly.**
  This was a real bug, found by the smoke suite and fixed in the compose file.
  `docker stop` signals PID 1, PID 1 is `python`, and the kernel *discards* a
  signal sent to a PID-namespace init that has no handler for it. CPython
  installs a handler for `SIGINT` and nothing else -- `/proc/1/status` reads
  `SigCgt: 0000000000000002`, bit 1 alone. Measured with the default `SIGTERM`:
  `docker stop -t 10` took **9905ms** and exited **137**, i.e. SIGKILL at the
  deadline, with `_run`'s `finally` never reached and so `db.close()` never
  called -- on every stop and every `compose down`. With `stop_signal: SIGINT`:
  **74ms**, exit **0**, and the log shows `db closed` then `interrupted`. Same
  result on a native image and a cross-built one. `init: true` does not fix it:
  tini makes `python` a non-init child so `SIGTERM`'s default action applies, but
  that action is die-immediately -- exit 143, still no `db.close()`.
- **A hard kill is survivable anyway.** After `docker kill -s KILL`,
  `marginalia.db-wal` (28 KB) and `-shm` were left behind, the next open recovered
  them, `integrity_check` was `ok`, and the row written immediately before the
  kill was still there. So the clean-shutdown fix above buys tidiness and
  certainty, not the difference between working and losing data.
- **`docker kill` is not a crash.** The daemon flags it `HasBeenManuallyStopped`
  and *suppresses* the restart policy, so it stays down exactly like
  `docker stop`. Do not use it to test that the bot comes back. `kill -9 1` from
  *inside* the container is also a no-op, for the PID-namespace reason above.

## Smoke suite

`container-test.sh` is the repeatable version of everything below. Run it on the
unRAID box after the first deploy and after every upgrade:

```sh
cd /mnt/cache/marginalia/deploy
./container-test.sh                       # ~2 min, exits non-zero on any failure
```

It needs the image to exist (`docker compose up -d --build` first) and nothing
else. It never touches the live container or the live database: every case gets
its own throwaway directory under `/mnt/cache/appdata/.marginalia-smoketest.$$`
and its own container names, and the whole tree is removed on exit including on
failure. Override with `IMAGE=` and `DATA_ROOT=` if your paths differ.

Ten cases, in descending order of what they would cost you:

| | what it proves |
|---|---|
| 1 | **Restart survival.** Seeds a pending reminder, stops the container, advances the clock past `due_at`, starts it again, and the reminder is then delivered -- exactly once, marked `sent`. This is the whole reason reminders are rows. |
| 2 | **Data survives a `compose down` + `up`**, not just a restart. New container id, same rows, `integrity_check` ok. Also statically checks the shipped compose file bind-mounts a `/mnt/cache` host path and uses no `/mnt/user`. |
| 3 | **`docker stop` shuts down cleanly inside its 10s budget**, `db.close()` really runs, and a `-wal` left by a hard kill recovers on the next open. |
| 4 | **`restart: unless-stopped` works both directions.** A crash comes back; a deliberate `docker stop` stays down. |
| 5 | **The token never reaches a log line, the database, or anything in `docker inspect` except `Env`.** |
| 6 | **`migrate()` is idempotent across boots.** Three boots, identical `user_version`/table/index/trigger counts, no duplicate objects. |
| 7 | **No dev tooling in the image.** `import pytest` and `import ruff` fail; all 14 runtime imports succeed. |
| 8 | **Runtime zoneinfo, not just build-time.** Real DST offsets, `TZ` in effect, and libc agrees (`tzname == ('CST','CDT')`). |
| 9 | **An unwritable `/data` fails loudly** and leaves no half-made database. |
| 10 | **Two containers on one db file.** See the warning below. |

### What it cannot cover, and why nothing is skipped for it

**No case needs a Discord token.** `reminders.Poller` takes its clock and its
delivery callback as parameters, so case 1 drives the *real* `tick()` -- the real
claim `UPDATE`, the real `sent` write -- with an injected `now` and a callback
that records instead of sending. Startup is likewise exercised by a stand-in
(`hold.py`, written into the temp dir at runtime) that mirrors
`marginalia/__main__.py` exactly: `load()` at call time, `Database` ->
`connect()` -> `migrate()`, `asyncio.run`, `db.close()` in a `finally`,
`KeyboardInterrupt` caught, and no `SIGTERM` handler. Only
`await bot.start(token)` is replaced. Case 5 and one of case 6's three boots run
the genuine `python -m marginalia`.

So these are not *skipped* -- they are simply unreachable without a live gateway,
and the suite says so at the end of every run:

- `bot.deliver()`: the actual `channel.send`, thread creation, and the
  `allowed_mentions=roles=[...]` rule.
- `setup_hook()`: cog loading and `tree.sync()`.
- the `tasks.loop(seconds=30)` cadence and its `@poll_reminders.error` backoff.
- the heartbeat written by the *live* loop every 30s. Case 1 writes a real
  heartbeat row via the real `tick()`, so the watchdog's contract is covered;
  the cadence is not.

Only watching a real cohort in a real guild covers those.

### Two containers on one database: don't

Case 10 measures it rather than guessing. Two containers sharing
`/mnt/cache/appdata/marginalia` did **not** corrupt anything --
`integrity_check` stayed `ok`, and when both polled at the same instant only
**one** reminder was delivered, because `Poller`'s claim is
`UPDATE ... WHERE status='pending'` gated on `rowcount == 1` rather than on a
timestamp. But one of two concurrent unrelated writes lost with `SQLITE_BUSY` and
its data was gone. WAL gives you one writer at a time, not two.

So: run **one** container. If you clone the compose file for a second guild, give
it a different `container_name` **and** a different data directory. The design is
single-writer and the 5s `busy_timeout` is sized for one process.

### A wart worth knowing

When `/data` is unwritable the real error is
`sqlite3.OperationalError: unable to open database file`, which is correct and
loud -- but it is followed by a second, misleading
`RuntimeError: Event loop is closed` from aiosqlite's worker thread outliving the
loop. Read the *first* traceback. The suite prints the causal lines and labels
the second one as noise.

## Backup

```sh
sqlite3 /mnt/cache/appdata/marginalia/marginalia.db \
  ".backup /mnt/user/backups/marginalia-$(date +%F).db"
```

Not `cp`. A copy reads the file over a non-zero span, so it can capture pages
from before and after a concurrent write, and in WAL mode the committed state is
split between the db and its `-wal`. `.backup` takes a consistent snapshot.
(The *destination* on `/mnt/user` is fine -- it is a plain file nothing locks.)

## Rollback

```sh
docker compose down
cp /mnt/user/backups/marginalia-<date>.db /mnt/cache/appdata/marginalia/marginalia.db
rm -f /mnt/cache/appdata/marginalia/marginalia.db-wal \
      /mnt/cache/appdata/marginalia/marginalia.db-shm   # stale sidecars, db is whole
docker compose up -d
```

To roll back code instead: `git checkout <ref> && docker compose up -d --build`.
