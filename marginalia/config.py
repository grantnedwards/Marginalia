"""Environment -> frozen Config. The ONLY module in this project that reads os.environ."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

_REQUIRED = ("DISCORD_TOKEN", "GUILD_ID", "BOOK_CLUB_CHANNEL_ID", "MARGINALIA_DB")


class ConfigError(Exception):
    """Bad or absent environment. Never carries the token in its message."""


@dataclass(frozen=True, repr=False)
class Config:
    token: str
    guild_id: int
    channel_id: int
    db_path: str
    # The club's home zone: every cohort is opened in it, so "/schedule at:19:00" means
    # 19:00 here. Discord renders each reader's own local time from the same message.
    tz: str = "UTC"
    # Optional read-only Calibre library. Empty disables /ingest-library entirely, so a
    # club with no Calibre keeps the upload path and nothing else changes.
    calibre_library: str = ""

    def __repr__(self) -> str:
        return (
            f"Config(token=<redacted>, guild_id={self.guild_id}, "
            f"channel_id={self.channel_id}, db_path={self.db_path!r}, tz={self.tz!r}, "
            f"calibre_library={self.calibre_library!r})"
        )


def _zone(env: Mapping[str, str]) -> str:
    """CLUB_TZ, else the container's TZ, else UTC -- validated, so a typo fails at boot
    rather than the first time a checkpoint is planned."""
    tz = (env.get("CLUB_TZ") or env.get("TZ") or "UTC").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"CLUB_TZ must be an IANA zone such as America/Chicago, got {tz!r}"
        ) from None
    return tz


def load(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    missing = [k for k in _REQUIRED if not env.get(k, "").strip()]
    if missing:
        raise ConfigError("missing or empty environment variables: " + ", ".join(missing))
    ids: dict[str, int] = {}
    for k in ("GUILD_ID", "BOOK_CLUB_CHANNEL_ID"):
        try:
            ids[k] = int(env[k].strip())
        except ValueError:
            raise ConfigError(f"{k} must be a decimal snowflake id, got {env[k]!r}") from None
    return Config(
        token=env["DISCORD_TOKEN"].strip(),
        guild_id=ids["GUILD_ID"],
        channel_id=ids["BOOK_CLUB_CHANNEL_ID"],
        # expanduser only: the container pins WORKDIR, so a relative
        # path here means exactly one file.
        db_path=os.path.expanduser(env["MARGINALIA_DB"].strip()),
        tz=_zone(env),
        # Not validated here: an optional integration must not stop the bot booting if
        # a bind mount is late or gone. /ingest-library reports it instead.
        calibre_library=env.get("CALIBRE_LIBRARY", "").strip(),
    )
