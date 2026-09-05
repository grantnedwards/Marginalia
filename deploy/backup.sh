#!/bin/bash
# marginalia database backup -- runs on the unRAID HOST from root's crontab, daily.
#
# `sqlite3 .backup`, never `cp`: a copy reads the file over a non-zero span and can
# capture pages from before and after a concurrent write, and in WAL mode the committed
# state is split between the db and its -wal sidecar. .backup takes a consistent
# snapshot through SQLite's own API while the bot keeps running.
#
# The DESTINATION may live on /mnt/user (the array): it is a plain file that nothing
# locks. Only the LIVE database must stay on /mnt/cache.
#
# Usage: backup.sh [destination dir]      Env: KEEP=30 (days of backups to keep)

set -euo pipefail

DB=/mnt/cache/appdata/marginalia/data/marginalia.db
DEST="${1:-/mnt/user/backups/marginalia}"
KEEP="${KEEP:-30}"

log() { echo "$(date '+%F %T') backup: $*"; }

if [ ! -f "$DB" ]; then
  log "no database at $DB yet -- nothing to back up"; exit 0
fi
command -v sqlite3 >/dev/null 2>&1 || { log "sqlite3 not found on the host"; exit 1; }

mkdir -p "$DEST"
out="$DEST/marginalia-$(date +%F).db"
sqlite3 "$DB" ".backup '$out.tmp'"
# Prove the snapshot opens and is whole BEFORE it replaces today's copy.
if [ "$(sqlite3 "$out.tmp" 'PRAGMA integrity_check')" != "ok" ]; then
  rm -f "$out.tmp"; log "integrity check FAILED on the snapshot; kept nothing"; exit 1
fi
mv -f "$out.tmp" "$out"
log "wrote $out ($(du -h "$out" | cut -f1))"

# Retention: keep the newest $KEEP, delete the rest.
ls -1t "$DEST"/marginalia-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
