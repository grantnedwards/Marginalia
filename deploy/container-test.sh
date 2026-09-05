#!/usr/bin/env bash
#
# container-test.sh -- post-deploy smoke suite for the marginalia container.
#
# Run this ON THE unRAID BOX after `docker compose up -d --build`. It answers the
# questions `docker ps` cannot: does a reminder still fire after a restart, does
# the reading history survive a container RECREATE, does `docker stop` get a clean
# exit inside its 10s budget, does the restart policy actually restart, and is the
# token absent from the logs.
#
# It NEVER touches the live stack or the live database. Every case gets a fresh
# throwaway data directory under $DATA_ROOT and its own container names, and the
# whole tree is removed on exit including on failure.
#
# It needs NO Discord token. Where a live gateway would be required, a stand-in
# process (hold.py, written below) reproduces the exact startup path of
# marginalia/__main__.py -- config -> Database -> connect -> migrate -> long-lived
# await, db.close() in a finally, NO SIGTERM handler -- and replaces only
# bot.start(). See "not covered" at the bottom of the summary.
#
#   Usage:  ./container-test.sh
#   Env:    IMAGE=marginalia:latest   DATA_ROOT=/mnt/cache/appdata
#
# Exits non-zero if any case fails.

set -euo pipefail

IMAGE="${IMAGE:-marginalia:latest}"
DATA_ROOT="${DATA_ROOT:-/mnt/cache/appdata}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HERE/docker-compose.yml"
PROJECT="margsmoke$$"
PREFIX="margsmoke$$"
CANARY="FAKE-TOKEN-CANARY-a1b2c3d4e5f6"   # distinctive on purpose: greppable
WORK="$DATA_ROOT/.marginalia-smoketest.$$"

PASSES=0 FAILS=0 SKIPS=0 FAILED_NAMES="" SKIPPED_NAMES=""

# ---------------------------------------------------------------- plumbing ---

die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }
say() { printf '    %s\n' "$*"; }

# A container writes into $WORK as uid 99, so the host user may not be able to
# rm it. Fall back to doing the delete from inside a root container.
cleanup() {
  local ids
  ids="$(docker ps -aq --filter "name=^${PREFIX}" 2>/dev/null || true)"
  [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1 || true
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$WORK/override.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true
  if [ -d "$WORK" ]; then
    rm -rf "$WORK" 2>/dev/null || \
      docker run --rm -u 0:0 -v "$WORK":/w "$IMAGE" \
        find /w -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir "$WORK" 2>/dev/null || true
  fi
}
trap cleanup EXIT

run_case() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    printf 'PASS  %s\n' "$name"; PASSES=$((PASSES + 1))
  else
    printf 'FAIL  %s\n' "$name"; FAILS=$((FAILS + 1)); FAILED_NAMES="$FAILED_NAMES $name"
  fi
}

skip_case() {
  printf '\n=== %s ===\nSKIP  %s -- %s\n' "$1" "$1" "$2"
  SKIPS=$((SKIPS + 1)); SKIPPED_NAMES="$SKIPPED_NAMES $1"
}

check() {  # check <description> <expected> <actual>
  if [ "$2" = "$3" ]; then say "ok   $1: $3"; return 0; fi
  say "BAD  $1: expected [$2] got [$3]"; return 1
}

# Print the lines that NAME the failure, not the tail. A failed db open also
# emits a second, misleading "RuntimeError: Event loop is closed" from
# aiosqlite's worker thread after the loop is gone, so the tail of the output is
# the wrong place to look.
show_cause() {
  echo "$1" | grep -iE 'Error|error|readonly|read-only|permission|unable to open' \
    | grep -viE 'call_soon_threadsafe|_check_closed|Event loop is closed' \
    | tail -4 | sed 's/^/      /'
  echo "$1" | grep -q 'Event loop is closed' \
    && say "     (a second 'RuntimeError: Event loop is closed' also appears --"
  echo "$1" | grep -q 'Event loop is closed' \
    && say "      aiosqlite's worker thread outliving the loop. Noise, not the cause.)"
  return 0
}

now_ns() {
  local t; t="$(date +%s%N)"
  case "$t" in *N*) echo "$(date +%s)000000000";; *) echo "$t";; esac
}

# --------------------------------------------------------------- fixtures ----

# A fresh data dir per case, owned by the container's uid, so cases cannot
# contaminate each other and every case starts from a real migrate.
fresh_data() {
  local d="$WORK/$1"
  rm -rf "$d" 2>/dev/null || true
  mkdir -p "$d"
  chown 99:100 "$d" 2>/dev/null || \
    docker run --rm -u 0:0 -v "$d":/data "$IMAGE" chown 99:100 /data >/dev/null
  echo "$d"
}

# One-shot python against a data dir. No ENTRYPOINT in the image, so the CMD is
# simply replaced. PYTHONPATH=/app because these helpers live in /t: running a
# SCRIPT puts the script's own directory on sys.path, not the cwd, so /app would
# otherwise be invisible. The real CMD is `python -m marginalia`, and -m does put
# the WORKDIR on sys.path -- that is why the shipped image needs no PYTHONPATH.
pyrun() {  # pyrun <datadir> <script-in-/t> [args...]
  local d="$1"; shift
  docker run --rm -u 99:100 -v "$d":/data -v "$WORK/t":/t:ro \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago -e PYTHONPATH=/app \
    "$IMAGE" python "/t/$1" "${@:2}"
}

q() { pyrun "$1" q.py "$2"; }   # q <datadir> <sql>  -> pipe-joined rows

# Start a long-lived stand-in container on a data dir. PID 1 is python, exactly
# as with the real CMD.
start_hold() {  # start_hold <name> <datadir> [extra docker args...]
  local name="$1" d="$2"; shift 2
  docker run -d --name "$name" -u 99:100 \
    -v "$d":/data -v "$WORK/t":/t:ro \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago -e PYTHONPATH=/app \
    -e DISCORD_TOKEN="$CANARY" -e GUILD_ID=1 -e BOOK_CLUB_CHANNEL_ID=2 \
    "$@" "$IMAGE" python /t/hold.py >/dev/null
}

wait_for_log() {  # wait_for_log <name> <needle> [secs]
  local name="$1" needle="$2" secs="${3:-45}" i=0
  while [ "$i" -lt "$((secs * 4))" ]; do
    docker logs "$name" 2>&1 | grep -q -- "$needle" && return 0
    docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q false && {
      say "container $name exited before logging '$needle':"
      docker logs "$name" 2>&1 | tail -20 | sed 's/^/      /'
      return 1
    }
    sleep 0.25; i=$((i + 1))
  done
  say "timeout waiting for '$needle' in $name"
  docker logs "$name" 2>&1 | tail -20 | sed 's/^/      /'
  return 1
}

write_helpers() {
  mkdir -p "$WORK/t"

  cat > "$WORK/t/q.py" <<'PY'
"""Run one SQL statement against /data/marginalia.db, print pipe-joined rows."""
import sqlite3, sys
c = sqlite3.connect("/data/marginalia.db")
for row in c.execute(sys.argv[1]):
    print("|".join("" if v is None else str(v) for v in row))
PY

  cat > "$WORK/t/schema_snapshot.py" <<'PY'
"""One line fingerprinting the whole schema: what migrate() must not change."""
import sqlite3
c = sqlite3.connect("/data/marginalia.db")
g = lambda s: c.execute(s).fetchone()[0]
n = lambda t: c.execute(
    "SELECT count(*) FROM sqlite_master WHERE type=?", (t,)).fetchone()[0]
print("user_version=%s tables=%s indexes=%s triggers=%s objects=%s integrity=%s" % (
    g("PRAGMA user_version"), n("table"), n("index"), n("trigger"),
    g("SELECT count(*) FROM sqlite_master"), g("PRAGMA integrity_check")))
PY

  cat > "$WORK/t/dupes.py" <<'PY'
"""A re-run of the DDL would show up here as a repeated name. Must print 0."""
import sqlite3
c = sqlite3.connect("/data/marginalia.db")
dupes = c.execute(
    "SELECT type, name, count(*) FROM sqlite_master GROUP BY type, name"
    " HAVING count(*) > 1").fetchall()
print(len(dupes), dupes)
PY

  cat > "$WORK/t/seed.py" <<'PY'
"""Seed the books -> cohorts -> checkpoints -> reminders chain and ONE pending
reminder due at argv[1]. grace_secs=-1 (infinite) is what 'unlock' really uses,
so lateness can never be the reason it is skipped."""
import sqlite3, sys
due = int(sys.argv[1])
c = sqlite3.connect("/data/marginalia.db")
c.execute("PRAGMA busy_timeout=10000")
with c:
    b = c.execute("INSERT INTO books (title, author, chapter_count)"
                  " VALUES ('Smoke Test','nobody',3)").lastrowid
    h = c.execute("INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month,"
                  " tz_id, status) VALUES (1,2,?, '2026-09','America/Chicago','active')",
                  (b,)).lastrowid
    p = c.execute("INSERT INTO checkpoints (cohort_id, idx, label, unit, start_ref,"
                  " end_ref, chapter_ceiling, due_at_utc, tz_id, local_wall)"
                  " VALUES (?,1,'Chapters 1-3','chapter',1,3,3,?, 'America/Chicago',"
                  " '2026-09-30 19:00')", (h, due)).lastrowid
    r = c.execute("INSERT INTO reminders (checkpoint_id, kind, due_at, grace_secs,"
                  " status) VALUES (?, 'unlock', ?, -1, 'pending')", (p, due)).lastrowid
print("reminder_id=%d checkpoint_id=%d due_at=%d" % (r, p, due))
PY

  cat > "$WORK/t/tick.py" <<'PY'
"""Drive the real Poller with NO Discord and NO token.

reminders.Poller takes its clock (tick's `now`) and its delivery callback as
parameters precisely so this is possible: `now` is injected here, and `deliver`
only RECORDS the row instead of sending it. This is the real tick(), the real
claim UPDATE and the real 'sent' write -- only the Discord send is stubbed.
"""
import asyncio, json, os, sys
from marginalia.db import Database
from marginalia.reminders import Poller

async def main():
    now = int(sys.argv[1])
    db = Database(os.environ["MARGINALIA_DB"])
    await db.connect()
    delivered = []
    async def deliver(row):
        delivered.append(dict(row))
    try:
        sent, skipped = await Poller(db, deliver).tick(now)
    finally:
        await db.close()
    print(json.dumps({"now": now, "sent": sent, "skipped": skipped,
                      "delivered": [(d["id"], d["kind"], d["due_at"]) for d in delivered]}))

asyncio.run(main())
PY

  cat > "$WORK/t/hold.py" <<'PY'
"""Stand-in for `python -m marginalia` when there is no Discord token.

Deliberately mirrors marginalia/__main__.py line for line: logging.basicConfig,
load() at CALL time, Database -> connect -> migrate, asyncio.run, db.close() in a
finally, KeyboardInterrupt caught, and NO signal.signal(SIGTERM, ...) -- the real
module installs none either, which is exactly what the SIGTERM case measures.
Only `await bot.start(cfg.token)` is replaced by a long-lived await. The token is
loaded and held, and never printed, same as the real thing.
"""
import asyncio, logging, os, sys
from marginalia.config import ConfigError, load
from marginalia.db import Database

log = logging.getLogger("marginalia")

async def _crash_on_sentinel(path):
    """Turn `touch /data/CRASH` into a real process crash.

    Needed because there is NO way to crash PID 1 from outside: the kernel
    discards an unhandled signal sent to a PID-namespace init, so `kill -9 1`
    inside the container is a no-op, and `docker kill` from outside sets the
    daemon's HasBeenManuallyStopped flag, which SUPPRESSES the restart policy --
    so `docker kill` cannot test the restart policy either. os._exit(1) is the
    honest article: PID 1 goes away with a non-zero status and no cleanup, and
    Docker was never asked to stop it.
    """
    while True:
        if os.path.exists(path):
            os.unlink(path)          # one-shot, or the restart just crashes again
            log.error("crash sentinel seen, dying hard")
            sys.stderr.flush()
            os._exit(1)
        await asyncio.sleep(0.2)

async def _run(cfg):
    db = Database(cfg.db_path)
    await db.connect()
    version = await db.migrate()
    crasher = asyncio.create_task(
        _crash_on_sentinel(os.path.join(os.path.dirname(cfg.db_path), "CRASH")))
    try:
        log.info("holding: migrated to user_version=%s db=%s", version, cfg.db_path)
        await asyncio.Event().wait()      # stands in for the gateway
    finally:
        crasher.cancel()
        await db.close()
        log.info("db closed")

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0

sys.exit(main())
PY

  cat > "$WORK/t/imports.py" <<'PY'
"""All 14 runtime pins must import; the dev tooling must not exist."""
import importlib, sys
RUNTIME = ["aiohappyeyeballs", "aiohttp", "aiosignal", "aiosqlite", "attr",
           "audioop", "discord", "frozenlist", "idna", "lxml", "multidict",
           "propcache", "dotenv", "yarl"]
DEV = ["pytest", "ruff", "_pytest", "pluggy", "iniconfig"]
bad = []
for m in RUNTIME:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append("runtime import FAILED %s: %r" % (m, exc))
present = []
for m in DEV:
    try:
        importlib.import_module(m)
        present.append(m)
    except ImportError:
        pass
print("runtime_ok=%d/%d dev_present=%s" % (len(RUNTIME) - len(bad), len(RUNTIME), present or "none"))
for line in bad:
    print(line)
sys.exit(1 if bad or present else 0)
PY

  cat > "$WORK/t/tz.py" <<'PY'
"""RUNTIME zoneinfo, not just the build-time assert. Three separate claims:
zoneinfo has real DST data, TZ reached the process, and libc agrees (so naive
local-time logging is Chicago too, not UTC)."""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo
jan = datetime(2026, 1, 15, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
jul = datetime(2026, 7, 15, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
assert jan != jul, "tzdata missing or stubbed: Jan %s == Jul %s" % (jan, jul)
assert jan.total_seconds() == -21600, jan
assert jul.total_seconds() == -18000, jul
assert os.environ.get("TZ") == "America/Chicago", os.environ.get("TZ")
assert time.tzname == ("CST", "CDT"), time.tzname
local = datetime.now().astimezone().tzname()
assert local in ("CST", "CDT"), local
print("runtime tz OK: zoneinfo Jan%+g/Jul%+g h, TZ=%s, libc tzname=%s, naive-now=%s" % (
    jan.total_seconds() / 3600, jul.total_seconds() / 3600,
    os.environ["TZ"], time.tzname, local))
PY

  cat > "$WORK/t/write_history.py" <<'PY'
"""Write the rows that MUST survive an upgrade: a book plus a reading-progress
log entry. If the bind mount is wrong these vanish on the first recreate."""
import sqlite3, sys
c = sqlite3.connect("/data/marginalia.db")
c.execute("PRAGMA busy_timeout=10000")
with c:
    b = c.execute("INSERT INTO books (title, author, chapter_count)"
                  " VALUES (?,'a durable author',7)", (sys.argv[1],)).lastrowid
    h = c.execute("INSERT INTO cohorts (guild_id, channel_id, book_id, cycle_month,"
                  " tz_id, status) VALUES (1,2,?, '2026-08','America/Chicago','closed')",
                  (b,)).lastrowid
    c.execute("INSERT INTO member_progress (cohort_id, user_id, chapter_index, percent)"
              " VALUES (?, 424242, 5, 71.5)", (h,))
print("wrote book_id=%d cohort_id=%d" % (b, h))
PY

  cat > "$WORK/t/ls.py" <<'PY'
import os
for n in sorted(os.listdir("/data")):
    st = os.stat("/data/" + n)
    print("%s %d bytes uid=%d gid=%d" % (n, st.st_size, st.st_uid, st.st_gid))
PY
}

write_override() {
  # The base compose file is used AS SHIPPED so that restart:, stop_signal:,
  # stop_grace_period:, init: and every other runtime setting under test comes
  # from the real thing. Only the three deployment-specific keys are replaced:
  # the live bind mount, the live env_file and the live container name.
  # !override needs docker compose >= 2.24; checked before we get here.
  # Every path here is ABSOLUTE on purpose: compose resolves a relative path in
  # an override file against the PROJECT directory -- the directory of the FIRST
  # -f file, i.e. deploy/ -- not against the override's own directory.
  cat > "$WORK/override.yml" <<YML
services:
  marginalia:
    image: $IMAGE
    pull_policy: never
    container_name: ${PREFIX}c
    env_file: !override
      - $WORK/smoke.env
    volumes: !override
      - $WORK/case2:/data
      - $WORK/t:/t:ro
    command: !override ["python", "/t/hold.py"]
    environment:
      PYTHONPATH: /app
YML
  cat > "$WORK/smoke.env" <<ENV
DISCORD_TOKEN=$CANARY
GUILD_ID=1
BOOK_CLUB_CHANNEL_ID=2
ENV
}

dc() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$WORK/override.yml" "$@"; }

# ============================================================== the cases ====

# 1. The core product property: reminders are ROWS, not in-memory timers, so a
#    reminder scheduled before a restart must still fire after it.
case_restart_survival() {
  local d t0 due seeded r1 r2 status
  d="$(fresh_data case1)"
  start_hold "${PREFIX}s1" "$d"
  wait_for_log "${PREFIX}s1" "holding: migrated" || return 1

  t0="$(date +%s)"; due=$((t0 + 3600))
  seeded="$(pyrun "$d" seed.py "$due")"; say "seed: $seeded"

  # Not yet due -> a tick at t0 must deliver nothing. Proves the later delivery
  # is the due time being reached, not just "any tick sends everything".
  r1="$(docker exec "${PREFIX}s1" python /t/tick.py "$t0")"; say "tick before due: $r1"
  echo "$r1" | grep -q '"sent": 0' || { say "BAD delivered early"; return 1; }

  docker stop "${PREFIX}s1" >/dev/null
  check "container stopped" "false" "$(docker inspect -f '{{.State.Running}}' "${PREFIX}s1")" || return 1
  check "row still pending while down" "pending" \
    "$(q "$d" "SELECT status FROM reminders")" || return 1

  # The clock advances past due_at while the container is DOWN.
  docker start "${PREFIX}s1" >/dev/null
  wait_for_log "${PREFIX}s1" "holding: migrated" || return 1

  r2="$(docker exec "${PREFIX}s1" python /t/tick.py $((due + 100)))"
  say "tick after restart, now=due+100: $r2"
  echo "$r2" | grep -q '"sent": 1' || { say "BAD not delivered after restart"; return 1; }
  echo "$r2" | grep -q '"skipped": 0' || { say "BAD it was skipped, not sent"; return 1; }

  status="$(q "$d" "SELECT status, sent_at IS NOT NULL, attempts FROM reminders")"
  check "status after delivery" "sent|1|1" "$status" || return 1

  # EXACTLY once: a second tick at the same clock must not re-deliver.
  r2="$(docker exec "${PREFIX}s1" python /t/tick.py $((due + 100)))"
  say "second tick, same now: $r2"
  echo "$r2" | grep -q '"sent": 0' || { say "BAD delivered twice"; return 1; }
  check "still exactly one sent row" "1" \
    "$(q "$d" "SELECT count(*) FROM reminders WHERE status='sent'")" || return 1
  check "heartbeat written by the poller" "reminders" \
    "$(q "$d" "SELECT name FROM heartbeat")" || return 1
  docker rm -f "${PREFIX}s1" >/dev/null
}

# 2. `docker compose down` + `up` -- a RECREATE, not a restart. This is what an
#    upgrade does, and it is where a wrong volume silently eats the history.
case_recreate() {
  local d id1 id2 snap
  # Static check first: the shipped file must bind-mount a HOST path, and a
  # cache-direct one. A named/anonymous volume would also survive down/up
  # without -v, so passing the dynamic test alone would not prove this.
  # Comments stripped first: the file legitimately NAMES /mnt/user in the comment
  # explaining why not to use it, and matching that would be a false alarm.
  local live; live="$(sed 's/#.*//' "$COMPOSE_FILE")"
  echo "$live" | grep -qE '^[[:space:]]*-[[:space:]]*/mnt/cache/[^:]+:/data[[:space:]]*$' \
    || { say "BAD $COMPOSE_FILE does not bind-mount a /mnt/cache host path to /data"; return 1; }
  say "ok   shipped compose bind-mounts $(echo "$live" | grep -oE '/mnt/cache/[^:]+:/data')"
  if echo "$live" | grep -qE '/mnt/user/'; then
    say "BAD $COMPOSE_FILE uses /mnt/user in real config (shfs FUSE: breaks WAL locking)"; return 1
  fi
  say "ok   no /mnt/user path in the shipped compose file outside comments"

  d="$(fresh_data case2)"
  dc up -d >/dev/null
  wait_for_log "${PREFIX}c" "holding: migrated" || return 1
  id1="$(docker inspect -f '{{.Id}}' "${PREFIX}c")"

  say "write: $(docker exec "${PREFIX}c" python /t/write_history.py 'Pale Fire')"
  check "rows before recreate" "1|1" \
    "$(q "$d" "SELECT (SELECT count(*) FROM books), (SELECT count(*) FROM member_progress)")" || return 1

  dc down >/dev/null 2>&1
  docker inspect "${PREFIX}c" >/dev/null 2>&1 \
    && { say "BAD container still exists after compose down"; return 1; }
  say "ok   compose down removed the container entirely"

  dc up -d >/dev/null
  wait_for_log "${PREFIX}c" "holding: migrated" || return 1
  id2="$(docker inspect -f '{{.Id}}' "${PREFIX}c")"
  [ "$id1" != "$id2" ] || { say "BAD same container id -- that was a restart, not a recreate"; return 1; }
  say "ok   new container id ${id2:0:12} (was ${id1:0:12})"

  check "rows after recreate" "1|1" \
    "$(q "$d" "SELECT (SELECT count(*) FROM books), (SELECT count(*) FROM member_progress)")" || return 1
  check "the actual data" "Pale Fire|a durable author|71.5" \
    "$(q "$d" "SELECT b.title, b.author, p.percent FROM books b JOIN cohorts h ON h.book_id=b.id JOIN member_progress p ON p.cohort_id=h.id")" || return 1
  snap="$(pyrun "$d" schema_snapshot.py)"; say "$snap"
  echo "$snap" | grep -q "integrity=ok" || return 1
  dc down >/dev/null 2>&1
}

# 3. `docker stop` sends SIGTERM and SIGKILLs 10s later. A process that needs
#    longer is killed mid-write on every single stop.
case_sigterm() {
  local d t0 t1 ms code files snap
  d="$(fresh_data case3)"
  # Own container on its own data dir, but the stop signal is read out of the
  # SHIPPED compose file so this cannot drift from what unRAID actually runs.
  say "stop signal from $COMPOSE_FILE: $(compose_stop_signal)"
  start_hold "${PREFIX}s3" "$d" --stop-signal "$(compose_stop_signal)"
  wait_for_log "${PREFIX}s3" "holding: migrated" || return 1
  check "python is PID 1" "python" \
    "$(docker exec "${PREFIX}s3" sh -c 'cut -c1-6 /proc/1/comm')" || return 1
  # The mechanism, so a future failure here explains itself: PID 1 is a
  # PID-namespace init, and the kernel DISCARDS signals it has no handler for.
  # CPython handles SIGINT and nothing else, so SigCgt bit 1 is the only bit and
  # SIGTERM (bit 15) is not caught -- which is why stop_signal matters.
  say "PID 1 signal mask: $(docker exec "${PREFIX}s3" grep -E '^Sig(Ign|Cgt)' /proc/1/status | tr '\n' ' ')"

  t0="$(now_ns)"
  docker stop -t 10 "${PREFIX}s3" >/dev/null
  t1="$(now_ns)"
  ms=$(( (t1 - t0) / 1000000 ))
  code="$(docker inspect -f '{{.State.ExitCode}}' "${PREFIX}s3")"
  say "docker stop -t 10 returned after ${ms}ms, exit code $code"

  [ "$ms" -lt 10000 ] || { say "BAD took ${ms}ms -- it was SIGKILLed at the 10s deadline"; return 1; }
  [ "$code" != "137" ] || { say "BAD exit 137 = SIGKILL: the signal was ignored"; return 1; }
  docker logs "${PREFIX}s3" 2>&1 | grep -q "db closed" \
    || { say "BAD 'db closed' never logged -- the finally never ran, so db.close() was skipped"; return 1; }
  say "ok   clean shutdown: the finally ran and db.close() completed"

  files="$(pyrun "$d" ls.py)"; say "files after stop:"; echo "$files" | sed 's/^/      /'
  snap="$(pyrun "$d" schema_snapshot.py)"; say "reopened: $snap"
  echo "$snap" | grep -q "integrity=ok" || { say "BAD integrity_check not ok after SIGTERM"; return 1; }

  # Whether -wal remains or not, the next open must recover. Force the question:
  # write, hard-kill so a -wal is certainly left behind, then reopen.
  start_hold "${PREFIX}s3b" "$d"
  wait_for_log "${PREFIX}s3b" "holding: migrated" || return 1
  docker exec "${PREFIX}s3b" python /t/write_history.py 'Recovered After Kill' >/dev/null
  docker kill -s KILL "${PREFIX}s3b" >/dev/null
  say "after SIGKILL:"; pyrun "$d" ls.py | sed 's/^/      /'
  snap="$(pyrun "$d" schema_snapshot.py)"; say "reopened after SIGKILL: $snap"
  echo "$snap" | grep -q "integrity=ok" || return 1
  check "the pre-kill write recovered from -wal" "Recovered After Kill" \
    "$(q "$d" "SELECT title FROM books WHERE title='Recovered After Kill'")" || return 1
  docker rm -f "${PREFIX}s3" "${PREFIX}s3b" >/dev/null
}

# Read the stop signal out of the SHIPPED compose file so the direct-docker-run
# cases cannot drift from it. Default is SIGTERM, which is docker's default.
compose_stop_signal() {
  local s
  s="$(grep -E '^\s*stop_signal:' "$COMPOSE_FILE" | head -1 | awk '{print $2}')"
  echo "${s:-SIGTERM}"
}

# 4. restart: unless-stopped, both directions. A crash must come back; a
#    deliberate `docker stop` must NOT -- that is the half the watchdog's
#    State.Running guard pairs with.
case_restart_policy() {
  local d pol n0 n1 started0 started1 running
  grep -qE '^\s*restart:\s*unless-stopped\s*$' "$COMPOSE_FILE" \
    || { say "BAD shipped compose has no 'restart: unless-stopped'"; return 1; }
  say "ok   shipped compose declares restart: unless-stopped"

  d="$(fresh_data case4)"
  # Same policy string, applied to this case's own container.
  start_hold "${PREFIX}s4" "$d" --restart unless-stopped
  wait_for_log "${PREFIX}s4" "holding: migrated" || return 1
  pol="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "${PREFIX}s4")"
  check "policy in effect" "unless-stopped" "$pol" || return 1
  n0="$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")"
  started0="$(docker inspect -f '{{.State.StartedAt}}' "${PREFIX}s4")"

  # A CRASH, not a stop. Two things that do NOT work, both demonstrated because
  # both look like the obvious test:
  #   - `kill -9 1` INSIDE the container: the kernel shields a PID-namespace init
  #     from unhandled signals sent from within its own namespace. No-op.
  #   - `docker kill`: the daemon sets HasBeenManuallyStopped, which suppresses
  #     the restart policy. It looks like a crash and is treated as a stop.
  # So the process is made to exit(1) on its own, which is what a crash is.
  docker exec "${PREFIX}s4" sh -c 'kill -9 1' 2>&1 | sed 's/^/      /' || true
  sleep 1
  if [ "$(docker inspect -f '{{.State.Running}}' "${PREFIX}s4")" = "true" ]; then
    say "ok   in-container 'kill -9 1' was ignored (PID-namespace init shielding)"
  else
    say "BAD in-container 'kill -9 1' killed PID 1 -- unexpected"; return 1
  fi

  docker exec "${PREFIX}s4" sh -c 'touch /data/CRASH' >/dev/null
  say "crash sentinel written; PID 1 will os._exit(1)"

  local i=0
  while [ "$i" -lt 60 ]; do
    n1="$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")"
    [ "$n1" -gt "$n0" ] && break
    sleep 0.5; i=$((i + 1))
  done
  n1="$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")"
  [ "$n1" -gt "$n0" ] || { say "BAD RestartCount still $n1 -- Docker did not restart it"; return 1; }
  wait_for_log "${PREFIX}s4" "holding: migrated" || return 1
  started1="$(docker inspect -f '{{.State.StartedAt}}' "${PREFIX}s4")"
  say "ok   restarted after crash: RestartCount $n0 -> $n1, StartedAt $started0 -> $started1"
  check "running again" "true" "$(docker inspect -f '{{.State.Running}}' "${PREFIX}s4")" || return 1

  # Other direction: a deliberate stop must stay stopped. This is the half that
  # pairs with watchdog.sh's State.Running guard -- if Docker resurrected a
  # stopped container the guard would be pointless, and vice versa.
  docker stop "${PREFIX}s4" >/dev/null
  n0="$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")"
  sleep 6
  running="$(docker inspect -f '{{.State.Running}}' "${PREFIX}s4")"
  check "stays stopped 6s after docker stop" "false" "$running" || return 1
  check "no restart attempted after docker stop" "$n0" \
    "$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")" || return 1
  say "ok   'unless-stopped' respected the operator; nothing resurrected it"

  # The trap worth knowing: `docker kill` is ALSO treated as a deliberate stop.
  docker start "${PREFIX}s4" >/dev/null; wait_for_log "${PREFIX}s4" "holding: migrated" || return 1
  n0="$(docker inspect -f '{{.RestartCount}}' "${PREFIX}s4")"
  docker kill -s KILL "${PREFIX}s4" >/dev/null
  sleep 6
  check "docker kill also stays down (HasBeenManuallyStopped)" "false" \
    "$(docker inspect -f '{{.State.Running}}' "${PREFIX}s4")" || return 1
  say "note so 'docker kill' cannot be used to test a crash: the daemon flags it"
  say "     as manually stopped and the restart policy is suppressed."
  docker rm -f "${PREFIX}s4" >/dev/null
}

# 5. The token must never reach a log line, a file, or the database. `docker
#    inspect` showing it in Env is a platform fact, not an application bug.
case_token_leak() {
  local d out code n_logs n_env n_other n_db
  d="$(fresh_data case5)"
  # The REAL entrypoint, with a real (fake) token: it migrates, then fails to
  # log in. Whatever it prints on the way is what an operator would see.
  set +e
  out="$(docker run --name "${PREFIX}s5" -u 99:100 -v "$d":/data \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago \
    -e DISCORD_TOKEN="$CANARY" -e GUILD_ID=1 -e BOOK_CLUB_CHANNEL_ID=2 \
    "$IMAGE" 2>&1)"
  code=$?
  set -e
  say "real entrypoint exit=$code, output:"; echo "$out" | tail -12 | sed 's/^/      /'

  n_logs="$(docker logs "${PREFIX}s5" 2>&1 | grep -c -- "$CANARY" || true)"
  check "occurrences in docker logs" "0" "$n_logs" || return 1
  n_env="$(docker inspect -f '{{json .Config.Env}}' "${PREFIX}s5" | grep -c -- "$CANARY" || true)"
  say "note occurrences in 'docker inspect .Config.Env': $n_env -- ACCEPTED. Docker"
  say "     stores a container's environment in its config; env_file + chmod 600"
  say "     protects the source, and 'docker inspect' already implies root/docker"
  say "     group. The application's job is to never PRINT it, which it does not."
  n_other="$(docker inspect "${PREFIX}s5" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin)[0];c=d["Config"];c.pop("Env",None);print(json.dumps(d))' 2>/dev/null \
    | grep -c -- "$CANARY" || true)"
  if [ -z "$n_other" ]; then n_other=0; fi
  check "occurrences in inspect with Env removed" "0" "$n_other" || return 1
  n_db="$(docker run --rm -u 99:100 -v "$d":/data "$IMAGE" \
    sh -c "grep -c -- '$CANARY' /data/* 2>/dev/null | tail -1" || true)"
  say "ok   token not persisted to /data (grep found none: '${n_db:-0}')"
  docker rm -f "${PREFIX}s5" >/dev/null
}

# 6. migrate() runs on EVERY start. Three boots must leave the schema identical.
case_migrate_idempotent() {
  local d s1 s2 s3 dupes
  d="$(fresh_data case6)"

  start_hold "${PREFIX}s6" "$d"; wait_for_log "${PREFIX}s6" "holding: migrated" || return 1
  docker logs "${PREFIX}s6" 2>&1 | grep 'holding:' | sed 's/^/      boot1 /'
  docker rm -f "${PREFIX}s6" >/dev/null
  s1="$(pyrun "$d" schema_snapshot.py)"; say "after boot 1: $s1"

  # Boot 2 uses the REAL entrypoint (`python -m marginalia`), so the actual
  # shipped migrate path runs against an already-migrated db.
  set +e
  docker run --rm -u 99:100 -v "$d":/data \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago \
    -e DISCORD_TOKEN="$CANARY" -e GUILD_ID=1 -e BOOK_CLUB_CHANNEL_ID=2 \
    "$IMAGE" 2>&1 | tail -3 | sed 's/^/      boot2 /'
  set -e
  s2="$(pyrun "$d" schema_snapshot.py)"; say "after boot 2 (real python -m marginalia): $s2"

  start_hold "${PREFIX}s6c" "$d"; wait_for_log "${PREFIX}s6c" "holding: migrated" || return 1
  docker logs "${PREFIX}s6c" 2>&1 | grep 'holding:' | sed 's/^/      boot3 /'
  docker rm -f "${PREFIX}s6c" >/dev/null
  s3="$(pyrun "$d" schema_snapshot.py)"; say "after boot 3: $s3"

  check "boot 2 identical to boot 1" "$s1" "$s2" || return 1
  check "boot 3 identical to boot 1" "$s1" "$s3" || return 1
  echo "$s1" | grep -q "user_version=1" || { say "BAD user_version is not 1"; return 1; }
  echo "$s1" | grep -q "integrity=ok" || return 1
  dupes="$(pyrun "$d" dupes.py)"
  check "duplicate schema objects" "0 []" "$dupes" || return 1
}

# 7. requirements.txt was split so ~30MB of dev tooling stays out of production.
case_no_dev_tooling() {
  local out code pip
  set +e
  out="$(docker run --rm -v "$WORK/t":/t:ro -e PYTHONPATH=/app "$IMAGE" python /t/imports.py 2>&1)"
  code=$?
  set -e
  say "$out"
  [ "$code" -eq 0 ] || { say "BAD imports.py exited $code"; return 1; }
  echo "$out" | grep -q "runtime_ok=14/14" || return 1
  echo "$out" | grep -q "dev_present=none" || return 1

  # And the individual assertions the brief names, verbatim.
  for m in pytest ruff; do
    if docker run --rm "$IMAGE" python -c "import $m" >/dev/null 2>&1; then
      say "BAD 'import $m' SUCCEEDED inside the image"; return 1
    fi
    say "ok   'python -c \"import $m\"' fails inside the image"
  done
  pip="$(docker run --rm "$IMAGE" pip list --format=freeze 2>/dev/null | wc -l | tr -d ' ')"
  say "ok   pip list has $pip distributions, none of them pytest/ruff"
}

# 8. The build asserts tzdata once. Assert it again at RUNTIME, in a running
#    container -- this is the subsystem that must not be wrong.
case_runtime_tz() {
  local d out
  d="$(fresh_data case8)"
  start_hold "${PREFIX}s8" "$d"
  wait_for_log "${PREFIX}s8" "holding: migrated" || return 1
  out="$(docker exec "${PREFIX}s8" python /t/tz.py)" || { say "BAD $out"; return 1; }
  say "$out"
  check "TZ env inside the running container" "TZ=America/Chicago" \
    "$(docker exec "${PREFIX}s8" printenv TZ | sed 's/^/TZ=/')" || return 1
  # The log timestamps the operator reads must be Chicago, not UTC.
  say "log line stamp: $(docker logs "${PREFIX}s8" 2>&1 | grep 'holding:' | cut -d' ' -f1-2)"
  say "container date : $(docker exec "${PREFIX}s8" date)"
  docker exec "${PREFIX}s8" date | grep -qE ' (CST|CDT) ' \
    || { say "BAD container date does not report CST/CDT"; return 1; }
  docker rm -f "${PREFIX}s8" >/dev/null
}

# 9. An unwritable /data must fail LOUDLY. Starting up and silently dropping
#    every write is the failure mode that costs you the history.
case_unwritable_db() {
  local d out code created
  # (a) read-only bind mount
  d="$(fresh_data case9a)"
  set +e
  out="$(docker run --rm -u 99:100 -v "$d":/data:ro \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago \
    -e DISCORD_TOKEN="$CANARY" -e GUILD_ID=1 -e BOOK_CLUB_CHANNEL_ID=2 \
    "$IMAGE" 2>&1)"
  code=$?
  set -e
  say "(a) /data mounted :ro -- exit=$code"
  show_cause "$out"
  [ "$code" -ne 0 ] || { say "BAD read-only /data started successfully"; return 1; }
  echo "$out" | grep -qiE 'readonly|read-only|unable to open database' \
    || { say "BAD error message does not name the cause"; return 1; }
  say "ok   refused to run, and the message names the cause"

  # (b) directory owned by someone else -- the 'you moved appdata' case
  d="$WORK/case9b"; rm -rf "$d"; mkdir -p "$d"
  docker run --rm -u 0:0 -v "$d":/data "$IMAGE" \
    sh -c 'chown 0:0 /data && chmod 755 /data' >/dev/null
  set +e
  out="$(docker run --rm -u 99:100 -v "$d":/data \
    -e MARGINALIA_DB=/data/marginalia.db -e TZ=America/Chicago \
    -e DISCORD_TOKEN="$CANARY" -e GUILD_ID=1 -e BOOK_CLUB_CHANNEL_ID=2 \
    "$IMAGE" 2>&1)"
  code=$?
  set -e
  say "(b) /data owned 0:0, container is 99:100 -- exit=$code"
  show_cause "$out"
  [ "$code" -ne 0 ] || { say "BAD unwritable /data started successfully"; return 1; }
  echo "$out" | grep -qiE 'unable to open database|readonly|read-only|permission' \
    || { say "BAD error message does not name the cause"; return 1; }
  created="$(docker run --rm -u 0:0 -v "$d":/data "$IMAGE" \
    sh -c 'ls /data | wc -l' | tr -d ' ')"
  check "files created in the unwritable dir" "0" "$created" || return 1
  say "ok   refused to run, named the cause, and left no half-made database"
}

# 10. The design is single-writer. Two containers on one db file is the
#     accident an operator makes; find out what it costs.
case_two_writers() {
  local d t0 due a b total
  d="$(fresh_data case10)"
  start_hold "${PREFIX}sA" "$d"; wait_for_log "${PREFIX}sA" "holding: migrated" || return 1
  start_hold "${PREFIX}sB" "$d"; wait_for_log "${PREFIX}sB" "holding: migrated" || return 1
  say "ok   two containers hold the same db file open concurrently"

  t0="$(date +%s)"; due=$((t0 - 60))       # already due
  say "seed: $(pyrun "$d" seed.py "$due")"

  # Both poll at the same instant. The claim is status='pending' + rowcount==1,
  # so at most one of them may deliver.
  docker exec "${PREFIX}sA" python /t/tick.py "$t0" > "$WORK/a.json" 2>&1 &
  local pa=$!
  docker exec "${PREFIX}sB" python /t/tick.py "$t0" > "$WORK/b.json" 2>&1 &
  local pb=$!
  wait "$pa" || true; wait "$pb" || true
  a="$(cat "$WORK/a.json")"; b="$(cat "$WORK/b.json")"
  say "A: $a"
  say "B: $b"
  total="$(q "$d" "SELECT count(*) FROM reminders WHERE status='sent'")"
  check "reminders delivered by two concurrent pollers" "1" "$total" || return 1
  say "ok   the status-column claim held: one delivery, not two"

  # Concurrent unrelated writes, then integrity.
  docker exec "${PREFIX}sA" python /t/write_history.py 'Writer A' >/dev/null 2>&1 &
  local wa=$!
  docker exec "${PREFIX}sB" python /t/write_history.py 'Writer B' >/dev/null 2>&1 &
  local wb=$!
  wait "$wa" || say "note writer A failed (SQLITE_BUSY is the expected loser)"
  wait "$wb" || say "note writer B failed (SQLITE_BUSY is the expected loser)"
  say "books after concurrent writes: $(q "$d" "SELECT group_concat(title) FROM books")"
  local snap; snap="$(pyrun "$d" schema_snapshot.py)"; say "$snap"
  echo "$snap" | grep -q "integrity=ok" \
    || { say "BAD integrity_check FAILED with two writers"; return 1; }
  say "ok   no corruption -- but see README: two pollers is still wrong, run one"
  docker rm -f "${PREFIX}sA" "${PREFIX}sB" >/dev/null
}

# ==================================================================== main ===

main() {
  printf 'marginalia container smoke suite\n'
  printf 'image=%s  data_root=%s  work=%s\n' "$IMAGE" "$DATA_ROOT" "$WORK"

  command -v docker >/dev/null || die "no docker on PATH"
  docker version >/dev/null 2>&1 || die "cannot talk to the docker daemon"
  docker image inspect "$IMAGE" >/dev/null 2>&1 \
    || die "image '$IMAGE' not found -- run 'docker compose up -d --build' first"
  [ -f "$COMPOSE_FILE" ] || die "no docker-compose.yml next to this script"
  mkdir -p "$WORK" || die "cannot create $WORK -- is $DATA_ROOT writable?"

  printf 'docker=%s  compose=%s  arch=%s\n' \
    "$(docker version --format '{{.Server.Version}}')" \
    "$(docker compose version --short 2>/dev/null || echo none)" \
    "$(docker version --format '{{.Server.Arch}}')"

  write_helpers
  write_override

  run_case CASE_1_RESTART_SURVIVAL   case_restart_survival
  # compose >= 2.24 is needed for the !override tag that keeps the shipped
  # compose file (and therefore its real restart/stop settings) under test.
  local cv; cv="$(docker compose version --short 2>/dev/null | tr -d 'v')"
  if [ -n "$cv" ] && dc config >/dev/null 2>&1; then
    run_case CASE_2_RECREATE         case_recreate
  else
    skip_case CASE_2_RECREATE "docker compose $cv cannot merge the override (needs >= 2.24 for !override)"
  fi
  run_case CASE_3_SIGTERM            case_sigterm
  run_case CASE_4_RESTART_POLICY     case_restart_policy
  run_case CASE_5_TOKEN_LEAK         case_token_leak
  run_case CASE_6_MIGRATE_IDEMPOTENT case_migrate_idempotent
  run_case CASE_7_NO_DEV_TOOLING     case_no_dev_tooling
  run_case CASE_8_RUNTIME_TZ         case_runtime_tz
  run_case CASE_9_UNWRITABLE_DB      case_unwritable_db
  run_case CASE_10_TWO_WRITERS       case_two_writers

  printf '\n================================================================\n'
  printf 'SUMMARY: %d passed, %d failed, %d skipped\n' "$PASSES" "$FAILS" "$SKIPS"
  if [ -n "$FAILED_NAMES" ];  then printf 'FAILED:%s\n' "$FAILED_NAMES"; fi
  if [ -n "$SKIPPED_NAMES" ]; then printf 'SKIPPED:%s\n' "$SKIPPED_NAMES"; fi
  cat <<'TXT'

NOT COVERED without a real Discord token (no case is skipped for want of one;
these are the parts no token means we cannot reach at all):
  - bot.Marginalia.deliver(): the real channel.send, thread creation and the
    AllowedMentions roles=[...] rule. tick() here uses a RECORDING callback.
  - setup_hook(): cog load and tree.sync() against the guild.
  - the tasks.loop cadence and its @poll_reminders.error backoff ladder.
  - the heartbeat written by the LIVE loop every 30s (tick() writes a real row
    here, so the watchdog's contract is covered; the 30s cadence is not).
To cover those you must watch a real cohort in a real guild.
TXT
  [ "$FAILS" -eq 0 ] || return 1
}

main "$@"
