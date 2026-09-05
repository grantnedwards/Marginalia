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
#   3. resolves the four settings, PRESERVING whatever is already configured
#   4. writes the unRAID Docker-tab template with those values baked in
#   5. recreates the container to match
#   6. adds it to unRAID's autostart list
#   7. installs the watchdog (*/5) and daily backup crons, persisted via /boot/config/go
#   8. refreshes the Docker tab's cache
#
# The token never has to touch this script or a command line. On a first install the
# container is created with an EMPTY token and left stopped; fill it in from the
# Docker tab (marginalia -> Edit) and click Apply. Nothing here prints the token.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=marginalia
IMAGE=marginalia:latest
DATA="$REPO/data"
TEMPLATE_DIR=/boot/config/plugins/dockerMan/templates-user
TEMPLATE="$TEMPLATE_DIR/my-marginalia.xml"
SRC_TEMPLATE="$REPO/deploy/unraid/my-marginalia.xml"
ICON=https://raw.githubusercontent.com/selfhst/icons/main/png/discord.png
GO=/boot/config/go
CRON_TAG="# === marginalia (added by deploy/unraid-install.sh) ==="

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

case "$REPO" in
  /mnt/cache/*) ;;
  *) die "The checkout is at $REPO. It must live on /mnt/cache (direct disk), not /mnt/user:
SQLite WAL on the shfs FUSE layer is the documented cause of corrupt databases." ;;
esac

# --- 1. image --------------------------------------------------------------------
say "building $IMAGE"
DOCKER_BUILDKIT=1 docker build -f "$REPO/deploy/Dockerfile" -t "$IMAGE" "$REPO" \
  || die "build failed"

# --- 2. data dir ----------------------------------------------------------------
say "data directory $DATA"
mkdir -p "$DATA" || die "cannot create $DATA"
chown 99:100 "$DATA"

# --- 3. resolve the settings ------------------------------------------------------
# Precedence: what is already on the container -> deploy/.env -> what is already in
# the flash template -> a default. The template is a real source, because unRAID's
# own "Apply" writes the values you type in the UI back into it, and the container
# is deleted and recreated below.
env_of() {  # env_of VAR -> the existing container's value for VAR, or ''
  docker container inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    2>/dev/null | sed -n "s/^$1=//p" | head -1
}
tpl_of() {  # tpl_of VAR -> the flash template's configured value for VAR, or ''
  [ -f "$TEMPLATE" ] || return 0
  awk -v t="$1" '
    $0 ~ "Target=\"" t "\"" {
      i = index($0, "</Config>"); if (i == 0) next
      head = substr($0, 1, i - 1); j = length(head)
      while (j > 0 && substr(head, j, 1) != ">") j--
      print substr(head, j + 1); exit
    }' "$TEMPLATE"
}
resolve() {  # resolve VAR DEFAULT -> exports VAR, first non-empty source wins
  local var="$1" def="${2:-}" val="${!1:-}"
  [ -n "$val" ] || val="$(env_of "$var")"
  [ -n "$val" ] || val="$(tpl_of "$var")"
  [ -n "$val" ] || val="$def"
  export "$var=$val"
}

if [ -f "$REPO/deploy/.env" ]; then
  set -a; . "$REPO/deploy/.env"; set +a
fi
resolve DISCORD_TOKEN
resolve GUILD_ID
resolve BOOK_CLUB_CHANNEL_ID
resolve CLUB_TZ America/Los_Angeles
resolve TZ "$CLUB_TZ"
export MARGINALIA_DB=/data/marginalia.db

# Optional Calibre library, mounted READ-ONLY. /mnt/user is deliberate here and is not
# the database rule: this is Calibre's library, not ours, we only ever read it, and
# /mnt/user is what calibre-web-automated mounts -- so both see exactly the same books.
CALIBRE_HOST="${CALIBRE_HOST:-$(tpl_of /calibre-library)}"
CALIBRE_HOST="${CALIBRE_HOST:-/mnt/user/appdata/calibre/Calibre Library}"
mounts=(-v "$DATA:/data")
if [ -f "$CALIBRE_HOST/metadata.db" ]; then
  say "Calibre library: $CALIBRE_HOST (read-only)"
  mounts+=(-v "$CALIBRE_HOST:/calibre-library:ro")
  export CALIBRE_LIBRARY=/calibre-library
else
  say "no Calibre library at $CALIBRE_HOST -- /ingest-library will say so and /ingest still works"
  export CALIBRE_LIBRARY=""
  CALIBRE_HOST=""
fi

# --- 4. template ------------------------------------------------------------------
# Render the repo's template with the resolved values, so the Docker tab shows what
# the container actually has. awk, not sed: no delimiter or backreference to escape.
say "unRAID template $TEMPLATE"
[ -f "$SRC_TEMPLATE" ] || die "missing $SRC_TEMPLATE"
mkdir -p "$TEMPLATE_DIR"
awk -v tok="$DISCORD_TOKEN" -v gid="$GUILD_ID" -v cid="$BOOK_CLUB_CHANNEL_ID" \
    -v ctz="$CLUB_TZ" -v tz="$TZ" -v mdb="$MARGINALIA_DB" \
    -v cal="$CALIBRE_LIBRARY" -v calhost="$CALIBRE_HOST" -v data="$DATA" '
function put(val,   i, head, j) {
  i = index($0, "</Config>"); if (i == 0) return
  head = substr($0, 1, i - 1); j = length(head)
  while (j > 0 && substr(head, j, 1) != ">") j--
  $0 = substr(head, 1, j) val "</Config>"
}
/Target="DISCORD_TOKEN"/        { put(tok) }
/Target="GUILD_ID"/             { put(gid) }
/Target="BOOK_CLUB_CHANNEL_ID"/ { put(cid) }
/Target="CLUB_TZ"/              { put(ctz) }
/Target="TZ"/                   { put(tz)  }
/Target="MARGINALIA_DB"/        { put(mdb) }
/Target="CALIBRE_LIBRARY"/      { put(cal) }
/Target="\/calibre-library"/    { put(calhost) }
/Target="\/data"/               { put(data) }
{ print }
' "$SRC_TEMPLATE" > "$TEMPLATE.tmp" || die "could not render the template"
# cp then rm, never mv, onto the vfat flash.
cp "$TEMPLATE.tmp" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"

# --- 5. container ---------------------------------------------------------------
# The values are passed as -e VAR with no value on the command line: docker reads
# them from THIS process's environment, so the token never reaches `ps` or history.
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
  -e CALIBRE_LIBRARY \
  "${mounts[@]}" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.icon=$ICON" \
  "$IMAGE" >/dev/null || die "could not create the container"

configured=true
for v in DISCORD_TOKEN GUILD_ID BOOK_CLUB_CHANNEL_ID; do
  [ -n "${!v}" ] || configured=false
done
if $configured; then
  say "starting $NAME"
  docker start "$NAME" >/dev/null
else
  missing=""
  for v in DISCORD_TOKEN GUILD_ID BOOK_CLUB_CHANNEL_ID; do
    [ -n "${!v}" ] || missing="$missing $v"
  done
  say "NOT starting $NAME: still empty:$missing"
  $was_running && echo "(it was running before; it is stopped now)"
fi

# --- 6. autostart ---------------------------------------------------------------
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

# --- 7. crons -------------------------------------------------------------------
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

# --- 8. Docker tab cache --------------------------------------------------------
INC=/usr/local/emhttp/plugins/dynamix.docker.manager/include
if [ -d "$INC" ]; then
  say "refreshing the Docker tab"
  (cd "$INC" && php -r '$docroot="/usr/local/emhttp"; require_once("DockerClient.php"); (new DockerTemplates())->getAllInfo(true);' >/dev/null 2>&1) || true
fi

say "done"
docker ps -a --filter "name=^${NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
$configured || cat <<EOF

Next: open the unRAID Docker tab -> marginalia -> Edit, fill in what is listed above,
and click Apply. Then: docker logs -f marginalia
EOF
