# marginalia on unRAID

Docker, not systemd: unRAID runs its OS from RAM off a USB stick, so
`/etc/systemd/system/` does not survive a reboot. Containers do, and so does
`/boot/config/go`, which is where the two cron jobs below are re-installed at boot.

Stock unRAID has Docker but **no `docker compose`** and **no host `python3`**. So the
deploy here is a plain `docker build` + `docker create` done by
[`unraid-install.sh`](unraid-install.sh), plus a Docker-tab template so the container
can be edited and restarted from the web UI like every other one. The compose file in
this directory is for other hosts and is kept in sync, but nothing on unRAID uses it.

## READ THIS FIRST: the database path

The checkout, and the database inside it, live on `/mnt/cache/appdata/marginalia/`.
A **direct** cache-SSD path. Never `/mnt/user/appdata/...`.

1. `/mnt/user` is unRAID's shfs FUSE layer. SQLite in WAL mode takes byte-range
   locks and keeps `-wal` and `-shm` sidecars next to the db, so the mount has
   to behave like a real filesystem. FUSE's locking semantics are not that, and
   this is the widely-documented cause of corrupt Plex/Sonarr/Radarr databases.
2. `/mnt/user` can land writes on the parity array, which spins disks up for
   every small frequent write on a box that should be idling.

Get this wrong and you lose the reading history quietly, weeks later.
`unraid-install.sh` refuses to run from anywhere but `/mnt/cache`.

## Layout on the box

```
/mnt/cache/appdata/marginalia/            the git checkout (this repo)
/mnt/cache/appdata/marginalia/data/       bind-mounted to /data: marginalia.db + -wal/-shm
                                          owned 99:100 (nobody:users), gitignored
/boot/config/plugins/dockerMan/templates-user/my-marginalia.xml   the Docker-tab template
/mnt/user/backups/marginalia/             daily .backup snapshots (a plain file: /mnt/user is fine here)
/var/log/marginalia-watchdog.log          } cron output
/var/log/marginalia-backup.log            }
```

## Install, and every upgrade

```sh
git clone https://github.com/grantnedwards/Marginalia.git /mnt/cache/appdata/marginalia
/mnt/cache/appdata/marginalia/deploy/unraid-install.sh
```

The script is idempotent. It:

1. builds `marginalia:latest` from the checkout (BuildKit, native amd64, ~1 min cold);
2. creates `data/` owned `99:100`;
3. copies the template into `templates-user/` so **marginalia** shows in the Docker tab
   with an icon, an Edit page and a WebUI-less card (there is no web UI: the gateway
   connection is outbound);
4. recreates the container with `--restart unless-stopped --stop-signal SIGINT`, the
   `/data` bind mount, and the `net.unraid.docker.managed` label, carrying the existing
   container's `DISCORD_TOKEN` / `GUILD_ID` / `BOOK_CLUB_CHANNEL_ID` / `CLUB_TZ` forward;
5. adds `marginalia` to `/var/lib/docker/unraid-autostart`;
6. installs two root crons and appends them to `/boot/config/go` so they survive a reboot;
7. refreshes the Docker tab's cache so the new entry shows without a page-cache lag.

On a **first** install the token is empty, so the container is created and left
**stopped**. Fill it in from the UI:

> Docker tab -> **marginalia** -> Edit -> `DISCORD_TOKEN`, `GUILD_ID`,
> `BOOK_CLUB_CHANNEL_ID` (and `CLUB_TZ` if the club is not in `America/Los_Angeles`)
> -> **Apply**

Apply recreates the container from the template, which is exactly what the script
built, so the two never fight. After that, upgrades are `git pull` + re-run the script.

If you would rather keep the token out of the template XML on the flash drive, put it
in `deploy/.env` (`chmod 600`) before running the script; the script exports it into
the container's environment and never prints it. Either way the value ends up in
`docker inspect`, as it does for every container on the box.

Leave `ports` alone: nothing listens.

### Building elsewhere

On unRAID the build is native x86_64 and needs no flags. **Building from an
Apple-Silicon Mac is the trap**: the default build would produce an arm64 image that
cannot run on unRAID. Cross-build explicitly:

```sh
docker buildx build --platform linux/amd64 --provenance=false \
  -f deploy/Dockerfile -t marginalia:latest .
```

The `RUN python - <<'EOF'` self-check needs BuildKit for its heredoc. `# syntax=docker/dockerfile:1`
is line 1 of the Dockerfile; Docker 23+ uses BuildKit by default.

Measured 2026-09-04: linux/amd64 54.7 MB compressed, 156 MB on disk; all 14 runtime pins
are prebuilt manylinux wheels, lxml included, so nothing compiles.

## Verify it is SCHEDULING, not merely RUNNING

`docker ps` cannot answer this. A poller task can die while the container stays
up, Discord stays connected and slash commands keep working -- and no reminder
is ever sent again. In Discord, `/status` (organizer-only) shows the same thing in
colour. From the shell:

```sh
sqlite3 -readonly /mnt/cache/appdata/marginalia/data/marginalia.db \
  "SELECT strftime('%s','now') - beat_at AS age_secs, tick_count, detail
     FROM heartbeat WHERE name='reminders'"
```

Under 60 is healthy (the loop ticks every 30s). Over ~600 means the scheduler
is dead regardless of what `docker ps` says.

## Watchdog

`watchdog.sh` runs from root's crontab every 5 minutes (installed by the script; it is
**not** a User Scripts job, that plugin is not installed here). It reads the heartbeat
read-only and `docker restart marginalia` if it is older than 600s. Idempotent, safe at
any frequency. The threshold is deliberately generous: `bot.py` stops restarting its own
loop after 6 consecutive failures (~135s of backoff) *by design*, so the heartbeat can go
stale and prove the scheduler is genuinely dead. A shorter threshold would just fight that.

It tries the host `sqlite3` first (unRAID ships one) and falls back to `docker exec`
into the container. Both branches are exercised and working.

It restarts **only a container that is currently running**. `docker stop marginalia`
for maintenance is respected and stays stopped -- a stopped container's heartbeat goes
stale by definition, and without that guard the cron would quietly undo you within five
minutes. A container that does not exist at all is the same non-event.

It also never judges staleness from a *missing* heartbeat row: the row must exist
first. That is what makes first boot safe -- between container start and the loop's
first tick there is no row, which reads as "nothing to do" rather than as infinitely
stale.

Log: `/var/log/marginalia-watchdog.log`. Run it by hand any time:
`bash /mnt/cache/appdata/marginalia/deploy/watchdog.sh`.

## Backup and restore

`backup.sh` runs daily at 04:10 from the same crontab. It takes a `sqlite3 .backup`
snapshot (never `cp`: a copy can capture pages from before and after a concurrent
write, and in WAL mode the committed state is split across two files), runs
`PRAGMA integrity_check` on the snapshot before keeping it, and prunes to the newest 30.
Destination `/mnt/user/backups/marginalia/`; log `/var/log/marginalia-backup.log`.

Restore:

```sh
docker stop marginalia
cp /mnt/user/backups/marginalia/marginalia-<date>.db /mnt/cache/appdata/marginalia/data/marginalia.db
rm -f /mnt/cache/appdata/marginalia/data/marginalia.db-wal \
      /mnt/cache/appdata/marginalia/data/marginalia.db-shm   # stale sidecars, db is whole
chown 99:100 /mnt/cache/appdata/marginalia/data/marginalia.db
docker start marginalia
```

To roll back code instead: `git checkout <ref>` and re-run `unraid-install.sh`.

## What was verified on the build machine (2026-09-04)

Built and run on a linux/amd64 image against a real ext4 directory owned by `99:100`
and bind-mounted to `/data`. Not inferred:

- **Build self-check passes.** `self-check OK: tz Jan-6/Jul-5, discord.py 2.7.1`: the
  zoneinfo database is real and DST is live. A stub tzdata would fail the build.
- **No config fails loudly and safely.** With no environment the container exits **2**
  with `missing or empty environment variables: DISCORD_TOKEN, GUILD_ID,
  BOOK_CLUB_CHANNEL_ID, MARGINALIA_DB` -- one line, no traceback, the token never echoed.
- **Migrations run before the gateway.** `user_version` = 2, `journal_mode` = `wal`,
  `integrity_check` = ok, FTS5 shadow tables present. A bad token still leaves a correct
  schema behind.
- **Non-root really is non-root.** `uid=99 gid=100` inside; files on the host are `99:100`.
- **WAL sidecars land on the host** while live and are absent after a clean stop, which is
  correct.
- **Watchdog, six cases, both read branches**: stale + running restarts; fresh does not;
  a repeat inside the window holds off; stopped stays stopped; absent is a no-op; a
  missing heartbeat row is a no-op. The db's mtime is unchanged by a watchdog read.
- **`docker stop` needs SIGINT.** PID 1 is `python`, and the kernel discards a signal a
  PID-namespace init has no handler for; CPython handles SIGINT and nothing else. With
  the default SIGTERM, `docker stop` took 9.9s and exited 137 with `db.close()` never
  reached. With SIGINT: 74ms, exit 0, `db closed`. `init: true` does not fix it.
- **A hard kill is survivable anyway.** After `docker kill -s KILL`, the next open
  recovered the `-wal`, `integrity_check` was ok, and the last row was present.

On the unRAID box itself, on 2026-09-05: the image built natively, the no-config
container exited 2 with the expected line, and the watchdog reported the container
correctly. The live-gateway checks (`docs/HANDOFF.md`, the 22-item queue) need the
token and the club's server, which only the owner has.

## Smoke suite

`container-test.sh` is the repeatable version of the list above (ten cases: restart
survival of a due reminder, data surviving a recreate, clean stop, the restart policy in
both directions, no token in any log, idempotent migrations, no dev tooling in the image,
runtime zoneinfo, an unwritable `/data`, and two containers on one db). **It needs
`docker compose`**, so it does not run on stock unRAID; run it on any Docker host with
compose, against the same image:

```sh
cd deploy && ./container-test.sh        # ~2 min, exits non-zero on any failure
```

It never touches a live container or database: every case uses its own throwaway
directory under `DATA_ROOT` and its own container names.

### Two containers on one database: don't

Case 10 measures it. Two containers sharing one `data/` did **not** corrupt anything,
and only **one** delivered a simultaneously-due reminder (the claim is
`UPDATE ... WHERE status='pending'` gated on `rowcount == 1`), but one of two concurrent
unrelated writes lost with `SQLITE_BUSY`. WAL gives you one writer at a time. Run
**one** container; a second guild needs its own container name **and** its own data
directory.
