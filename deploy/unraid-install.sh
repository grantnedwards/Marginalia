#!/bin/bash
# Build and (re)create the marginalia container on an unRAID box that has Docker but
# no docker compose -- which is the default unRAID install. Run it for the first
# deploy and again after every `git pull`. Idempotent.
#
#   /mnt/cache/appdata/marginalia/deploy/unraid-install.sh
#
# What it does, in order:
#   1. builds marginalia:latest from the checkout (BuildKit, native amd64)
#   2. makes sure data/ exists and is owned by 99:100 (the container's uid)
#   3. installs the unRAID Docker-tab template so the container is editable in the UI
#   4. recreates the container, carrying its existing env (token, ids) forward
#   5. adds it to unRAID's autostart list
#   6. installs the watchdog (*/5) and daily backup crons, persisted via /boot/config/go
#   7. refreshes the Docker tab's cache
#
# The token never has to touch this script: on a first install the container is
# created with an EMPTY token and left stopped. Fill it in from the Docker tab
# (marginalia -> Edit) and click Apply, or put it in deploy/.env and re-run this.
# Nothing here reads or prints the token.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=marginalia
IMAGE=marginalia:latest
DATA="$REPO/data"
TEMPLATE_DIR=/boot/config/plugins/dockerMan/templates-user
TEMPLATE="$TEMPLATE_DIR/my-marginalia.xml"
ICON=https://raw.githubusercontent.com/selfhst/icons/main/png/discord.png
GO=/boot/config/go
CRON_TAG="# === marginalia (added by deploy/unraid-install.sh) ==="

say() { printf '\n==> %s\n' "$*"; }

case "$REPO" in
  /mnt/cache/*) ;;
  *) echo "The checkout is at $REPO. It must live on /mnt/cache (direct disk), not /mnt/user:"
     echo "SQLite WAL on the shfs FUSE layer is the documented cause of corrupt databases."
     exit 1 ;;
esac

# --- 1. image --------------------------------------------------------------------
say "building $IMAGE"
DOCKER_BUILDKIT=1 docker build -f "$REPO/deploy/Dockerfile" -t "$IMAGE" "$REPO"

# --- 2. data dir ----------------------------------------------------------------
say "data directory $DATA"
mkdir -p "$DATA"
chown 99:100 "$DATA"

# --- 3. template ----------------------------------------------------------------
say "unRAID template $TEMPLATE"
mkdir -p "$TEMPLATE_DIR"
cp "$REPO/deploy/unraid/my-marginalia.xml" "$TEMPLATE"   # cp, never mv, onto the flash

# --- 4. container ---------------------------------------------------------------
# Existing env wins, then deploy/.env (if present), then empty. The values are passed
# to docker create as -e VAR (no value on the command line): docker reads them from
# THIS process's environment, so the token never appears in `ps` or shell history.
env_of() {  # env_of VAR -> the running/created container's value for VAR, or ''
  docker container inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n "s/^$1=//p" | head -1
}
if [ -f "$REPO/deploy/.env" ]; then
  set -a; . "$REPO/deploy/.env"; set +a
fi
export DISCORD_TOKEN="${DISCORD_TOKEN:-$(env_of DISCORD_TOKEN)}"
export GUILD_ID="${GUILD_ID:-$(env_of GUILD_ID)}"
export BOOK_CLUB_CHANNEL_ID="${BOOK_CLUB_CHANNEL_ID:-$(env_of BOOK_CLUB_CHANNEL_ID)}"
export CLUB_TZ="${CLUB_TZ:-$(env_of CLUB_TZ)}"; export CLUB_TZ="${CLUB_TZ:-America/Los_Angeles}"
export TZ="${TZ:-$CLUB_TZ}"
export MARGINALIA_DB=/data/marginalia.db

was_running=false
if docker container inspect "$NAME" >/dev/null 2>&1; then
  [ "$(docker container inspect -f '{{.State.Running}}' "$NAME")" = "true" ] && was_running=true
  say "removing the old container (data is in $DATA, untouched)"
  docker rm -f "$NAME" >/dev/null
fi

say "creating $NAME"
docker create --name "$NAME" \
  --restart unless-stopped --stop-signal SIGINT \
  -e DISCORD_TOKEN -e GUILD_ID -e BOOK_CLUB_CHANNEL_ID -e CLUB_TZ -e TZ -e MARGINALIA_DB \
  -v "$DATA:/data" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.icon=$ICON" \
  "$IMAGE" >/dev/null

configured=true
for v in DISCORD_TOKEN GUILD_ID BOOK_CLUB_CHANNEL_ID; do
  [ -n "${!v}" ] || configured=false
done
if $configured; then
  say "starting $NAME"
  docker start "$NAME" >/dev/null
else
  say "NOT starting $NAME: DISCORD_TOKEN, GUILD_ID or BOOK_CLUB_CHANNEL_ID is still empty"
  $was_running && echo "(it was running before; it is stopped now)"
fi

# --- 5. autostart ---------------------------------------------------------------
AUTOSTART=/var/lib/docker/unraid-autostart
if [ -f "$AUTOSTART" ] && ! grep -qx "$NAME" "$AUTOSTART"; then
  say "adding $NAME to unRAID autostart"
  echo "$NAME" >> "$AUTOSTART"
fi
PREFS=/boot/config/plugins/dockerMan/userprefs.cfg
if [ -f "$PREFS" ] && ! grep -q "=\"$NAME\"" "$PREFS"; then
  next=$(( $(sed -n 's/^\([0-9]*\)=.*/\1/p' "$PREFS" | sort -n | tail -1) + 1 ))
  printf '%s="%s"\n' "$next" "$NAME" >> "$PREFS"
fi

# --- 6. crons -------------------------------------------------------------------
CRON_LINES="$CRON_TAG
*/5  * * * * /bin/bash $REPO/deploy/watchdog.sh >> /var/log/marginalia-watchdog.log 2>&1
10 4 * * * /bin/bash $REPO/deploy/backup.sh >> /var/log/marginalia-backup.log 2>&1"
say "crontab (watchdog every 5 min, backup daily 04:10)"
( crontab -l 2>/dev/null | grep -v "marginalia" ; printf '%s\n' "$CRON_LINES" ) | crontab -
# unRAID's root filesystem is a RAM disk; only /boot/config/go survives a reboot.
if [ -f "$GO" ] && ! grep -qF "$CRON_TAG" "$GO"; then
  say "persisting the crons in $GO"
  {
    echo
    echo "$CRON_TAG"
    echo "(crontab -l 2>/dev/null; cat <<'CRONS'"
    printf '%s\n' "$CRON_LINES" | tail -n +2
    echo "CRONS"
    echo ") | crontab - 2>/dev/null"
  } >> "$GO"
fi

# --- 7. Docker tab cache --------------------------------------------------------
INC=/usr/local/emhttp/plugins/dynamix.docker.manager/include
if [ -d "$INC" ]; then
  say "refreshing the Docker tab"
  (cd "$INC" && php -r '$docroot="/usr/local/emhttp"; require_once("DockerClient.php"); (new DockerTemplates())->getAllInfo(true);' >/dev/null 2>&1) || true
fi

say "done"
docker ps -a --filter "name=^${NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
$configured || cat <<EOF

Next: open the unRAID Docker tab -> marginalia -> Edit, fill in DISCORD_TOKEN, GUILD_ID
and BOOK_CLUB_CHANNEL_ID, and click Apply. Then: docker logs -f marginalia
EOF
