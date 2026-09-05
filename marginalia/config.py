"""Environment -> frozen Config. The ONLY module in this project that reads os.environ."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

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

    def __repr__(self) -> str:
        return (
            f"Config(token=<redacted>, guild_id={self.guild_id}, "
            f"channel_id={self.channel_id}, db_path={self.db_path!r})"
        )


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
    )
