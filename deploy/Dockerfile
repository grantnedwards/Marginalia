# syntax=docker/dockerfile:1
FROM python:3.13-slim

# tzdata is NOT optional: the whole scheduler is IANA-zone arithmetic via
# zoneinfo, so without a zoneinfo database ZoneInfo("America/Chicago") raises
# LookupError whenever the next checkpoint is computed. Measured 2026-09-04:
# python:3.13-slim ALREADY ships it (apt reports tzdata 2026b-0+deb13u1 already
# newest, 0 newly installed), so this line currently adds no layer bytes. It
# stays as a declared dependency -- the base image is free to slim it out, and
# the self-check below is what would then fail the build instead of a reminder
# firing an hour wrong. TZ is set too so naive local-time logging agrees.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*
ENV TZ=America/Chicago \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# requirements.txt, not pyproject: the pins are exact there. audioop-lts is a
# HARD requirement -- Python 3.13 removed stdlib audioop and discord/player.py
# imports it unconditionally, so `import discord` dies without it. Do not prune.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY marginalia ./marginalia

# BUILD-TIME SELF-CHECK. Fails the build rather than failing at 03:00.
#
# 1. tzdata is really usable. A LookupError here, or matching Jan/Jul offsets,
#    means the zoneinfo database is missing or stubbed -- and the entire
#    scheduler is IANA-zone arithmetic, so it would silently fire reminders an
#    hour wrong for half the year with nothing in the logs.
# 2. `import discord` works, which is the audioop-lts check from the comment
#    above, made non-theoretical.
# 3. The bot's own modules import with no token and no network.
RUN python - <<'EOF'
from datetime import datetime
from zoneinfo import ZoneInfo
jan = datetime(2026, 1, 15, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
jul = datetime(2026, 7, 15, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
assert jan != jul, f"tzdata missing or stubbed: Jan {jan} == Jul {jul}"
import discord
assert discord.__version__ == "2.7.1", discord.__version__
import marginalia.bot, marginalia.library, marginalia.reminders, marginalia.timefmt
h = lambda d: f"{d.total_seconds() / 3600:+g}"  # timedelta's own repr is "-1 day, 18:00:00"
print(f"self-check OK: tz Jan{h(jan)}/Jul{h(jul)}, discord.py {discord.__version__}")
EOF

# unRAID passes PUID/PGID to containers by convention. This image does NOT
# honour them: no entrypoint, no gosu, no chown-on-boot. It runs as the fixed
# numeric 99:100, which IS unRAID's nobody:users -- already the owner of
# /mnt/cache/appdata. So the common case needs no chown. If you moved appdata
# somewhere with a different owner, chown the bind-mounted directory to 99:100
# (or override `user:` in the compose file). Explicit choice: fixed uid, because
# a PUID entrypoint means starting as root to drop privileges again.
USER 99:100

CMD ["python", "-m", "marginalia"]
