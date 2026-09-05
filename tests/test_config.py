import pytest

from marginalia.config import Config, ConfigError, load

TOKEN = "TEST_TOKEN_NOT_REAL"
ENV = {
    "DISCORD_TOKEN": TOKEN,
    "GUILD_ID": "1545535072151670824",
    "BOOK_CLUB_CHANNEL_ID": "42",
    "MARGINALIA_DB": "./data/marginalia.db",
}


def test_token_never_escapes_repr_or_str() -> None:
    cfg = load(ENV)
    for text in (repr(cfg), str(cfg), f"{cfg}"):
        assert TOKEN not in text
        assert "<redacted>" in text
    assert cfg.token == TOKEN  # still usable where it must be


def test_missing_variable_is_named() -> None:
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load({**ENV, "GUILD_ID": ""})


def test_snowflake_stays_an_exact_int() -> None:
    cfg = load(ENV)
    assert isinstance(cfg, Config)
    assert cfg.guild_id == 1545535072151670824
