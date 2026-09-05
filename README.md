# Marginalia

A Discord book-club bot for one guild: pick a book, split it into weekly
checkpoints, remind the cohort, and quote the text without spoiling anyone.

Discord.py 2.7.1 + aiosqlite + lxml. No LLM, no web scraping, no HTTP of its
own. ~2,300 module lines, ~900 of tests, one SQLite file.

## Features

- **Monthly cycles** — nominate books, pick one with a native Discord approval
  poll, cohort role granted on join and stripped at close. Membership rows are
  kept so `/library` and DNF stats survive the role.
- **Checkpoint schedules** — weekly plan over pages or chapters, DST-safe local
  times, discrete scheduled events, a public thread per checkpoint.
- **Durable reminders** — rows, not timers: T-24h, T-1h and unlock survive a
  restart, are claimed atomically (one send, even with two processes), reaped
  after a crash, and heartbeat every tick.
- **Spoiler-gated quoting** — `/quote` searches FTS5 under a per-member chapter
  ceiling enforced in SQL. Ephemeral by default; a refusal never echoes the
  question. Quote budget caps at 2% of a book per reader per 30 days and refuses
  ranges that abut one served in the last 24h.
- **Takedown in one command** — `/purge_book` deletes the text, its index and
  its ledger. Ingest deletes the uploaded EPUB either way.

## Quickstart

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in DISCORD_TOKEN, GUILD_ID, BOOK_CLUB_CHANNEL_ID, MARGINALIA_DB
.venv/bin/python -m pytest -q # 202 passed, no token needed
.venv/bin/python -m marginalia
```

The two setup steps no error message will tell you:

1. **Enable the SERVER MEMBERS intent** (Developer Portal -> Bot). Without it
   `guild.members` reads empty with no error, so the roster reports zero.
2. **Drag the bot's integration role ABOVE the cohort roles** (Server Settings
   -> Roles). Discord refuses role edits at or below the bot's own highest role
   and the 403 never says so. The permission you want is `MANAGE_ROLES`, which
   the client UI labels **"Manage Permissions"**.

## Commands

| | |
|---|---|
| Club | `/join` `/leave` `/roster` `/nominate` |
| Reading | `/next` `/pace` `/progress set` `/library` `/dnf` `/mystats` |
| Quoting | `/quote` `/find` `/passage` |
| Organizer | `/cycle-open` `/cycle-close` `/ballot` `/ballot-result` `/schedule` `/ingest` `/purge_book` |

Organizer commands ship with `default_member_permissions = 0`: invisible to
members until you grant them in Server Settings -> Integrations.

## Modules

```
timefmt  db  epub  config        no deps; timefmt owns ALL calendar arithmetic
   schedule  library  reminders   pure planning / the spoiler gate / the poller
      bot  cogs.club  cogs.reading  cogs.quote
```

`bot.py` owns the client, `setup_hook` and the `tasks.loop` wrapper around
`reminders.Poller.tick`, including the single `deliver` callback (it dispatches
on reminder kind; an unlock's thread and ceiling are `cogs/reading.py`'s).
`library.py` is the only module that may name the book-text tables, so the
spoiler boundary is one grep. Cogs are Discord surface over plain
`async def f(db, ...)` functions, which is why the tests need no token.

Design rationale, measured discord.py facts and the schema: `docs/`.
