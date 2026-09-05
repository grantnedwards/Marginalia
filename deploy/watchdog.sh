#!/bin/bash
# marginalia scheduler watchdog -- runs on the unRAID HOST via User Scripts,
# cron */5 * * * *. Safe and idempotent at any frequency.
#
# WHY THIS EXISTS: process liveness is not scheduler liveness. `restart:
# unless-stopped` only catches the process dying. The real failure is an
# asyncio task raising inside the reminder poller and never being awaited
# again: the task is dead, the container is still up, Discord is still
# connected, slash commands still answer, and no reminder is ever sent again.
# `docker ps` says healthy. Nobody notices until a checkpoint passes in silence.
#
# So we read the one thing that can tell the difference: heartbeat.beat_at,
# which marginalia/reminders.py upserts EVERY tick (30s) including zero-work
# ticks, for name='reminders'.
#
# READ-ONLY, always. A monitoring script must never be able to corrupt the
# thing it monitors.

set -uo pipefail

DB=/mnt/cache/appdata/marginalia/marginalia.db
NAME=marginalia
# bot.py gives up restarting the internal loop after RESTART_GIVE_UP=6
# consecutive failures BY DESIGN, with backoff 5+10+20+40+60 = ~135s, so it can
# let the heartbeat go stale and hand us a genuinely dead scheduler. 600s clears
# that ~2.2 minute window by 4x, and is 20 missed 30s beats -- far past noise.
STALE=600
STAMP=/tmp/marginalia-watchdog.stamp

log() { echo "$(date '+%F %T') watchdog: $*"; }

now=$(date +%s)

# Don't restart-storm: a fresh container needs a tick to write its first beat,
# and a still-broken one must not be bounced every cron run.
if [ -f "$STAMP" ] && [ $(( now - $(date -r "$STAMP" +%s) )) -lt "$STALE" ]; then
  log "restarted recently, holding off"; exit 0
fi

# A container an operator STOPPED is not a stale scheduler, it is a stopped
# container -- and its heartbeat goes stale by definition. Without this guard
# `docker stop marginalia` for maintenance is silently undone within 5 minutes,
# and keeping the bot down means remembering to disable the User Script too.
# Only a RUNNING container with a stale beat is the failure this script catches.
# `docker inspect` also exits non-zero when the container does not exist at all;
# that is likewise nothing to do, not an error worth logging as one.
if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
  log "$NAME is not running (stopped or absent) -- not our business, leaving it alone"; exit 0
fi

# Prefer the host sqlite3; fall back into the container if unRAID has none.
# -readonly / mode=ro means this script cannot write, checkpoint, or lock.
if command -v sqlite3 >/dev/null 2>&1; then
  beat=$(sqlite3 -readonly "$DB" \
    "SELECT beat_at FROM heartbeat WHERE name='reminders'" 2>/dev/null)
else
  beat=$(docker exec "$NAME" python -c "
import sqlite3
c = sqlite3.connect('file:/data/marginalia.db?mode=ro', uri=True)
r = c.execute(\"SELECT beat_at FROM heartbeat WHERE name='reminders'\").fetchone()
print(r[0] if r else '')" 2>/dev/null)
fi

case "$beat" in
  ''|*[!0-9]*) log "no heartbeat row yet (db absent, or bot has never ticked) -- nothing to do"; exit 0 ;;
esac

age=$(( now - beat ))
if [ "$age" -le "$STALE" ]; then
  log "ok, heartbeat ${age}s old"
  exit 0
fi

log "heartbeat ${age}s old (> ${STALE}s): scheduler is dead, restarting $NAME"
touch "$STAMP"
docker restart "$NAME"
