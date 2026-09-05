# Handoff: laptop -> unRAID

Built on a machine that **never held the Discord bot token**, deliberately. That
constraint shaped the architecture and is why this is a handoff rather than a
rewrite.

Read this, then run `.claude/workflows/live-verify.js`.

---

## The boundary

### PROVEN offline -- no token, no network, no gateway

Python 3.13.11, SQLite 3.51.2 (FTS5 in), discord.py 2.7.1. Every claim below is
a named test in `tests/`; run `.venv/bin/python -m pytest -q` for the output.

| Subsystem | What was actually verified |
|---|---|
| `timefmt.py` | Weekly cadence holds local wall clock across DST in several zones, spring-forward gap pushed past, fall-back ambiguity takes the earlier instant, Lord Howe's half-hour gap. **Negative control:** replacing `weekly()` with aware-datetime + `timedelta` fails 4 tests -- the classic silent-hour-slide bug cannot come back. Seconds not millis. |
| `schedule.py` | Checkpoint division covers the book exactly with no gap or overlap across many sizes; remainder spread to the EARLIEST weeks; degenerate input raises rather than coalescing; per-kind reminder offsets and grace; pace at the boundary and on a zero-length schedule. Pure -- no clock, no db, no discord. |
| `reminders.py` | **The atomic claim under concurrency:** two concurrent ticks deliver exactly once (`WHERE id=? AND status='pending'` + `rowcount==1`). Restart survival, grace expiry both signs, `reap` recovering a crashed send until attempts run out, `Forbidden`/`NotFound` never retried, backward clock jump does not resend (status column, not a timestamp comparison), heartbeat advances on a zero-work tick. |
| `db.py` | WAL in force, FK cascade, `UNIQUE(checkpoint_id, kind)`, idempotent migration, snowflake round-trip past 2^53, a write during a rolling-back `tx()` survives, overlapping `tx()` do not raise, FTS5 external-content triggers. |
| `epub.py` | Fragment-addressed spines, Calibre-split files, no-ToC fallback to headings, and **DRM detection distinguished from font obfuscation** (the second must still parse). |
| `library.py` | **The spoiler gate with positive controls both directions:** over-ceiling refuses AND under-ceiling serves, so an always-empty bug cannot pass as a working gate. Every fail-closed path returns chapter 0. Ceiling scoped to book + live cohort + due checkpoints. Thread pin does not outrank membership. FTS5 verbatim retrieval, the `rebuild` requirement, hostile query sanitization operator by operator, literal `\|\|` escaping. **The 2% / 30-day cap halts a sequential walk.** Per-quote AND per-response word caps, abutment and whole-chapter refusal. |
| `bot.py` + cogs | One `deliver` callback dispatches on all kinds; restart backoff escalates 5/10/20/40/60, caps, resets, and gives up at 6 (sum 135s, asserted `< 600` = `deploy/watchdog.sh` STALE). **All three cogs load against a real `Marginalia` with no token and no HTTP**, registering the real command count: 22 top-level, 24 walked, 23 runnable. `/cycle-open` refuses outside `#florilegium` before `create_role`, with a right-channel positive control. |
| Ephemerality (`tests/test_ephemeral.py`) | **What the cogs hand Discord, not what a helper returns.** A recording fake Interaction pins: every book-text reply carries `ephemeral=True`; a refusal is ephemeral and carries no query echo, chapter title or match count; `/roster`'s role mention passes `AllowedMentions(everyone=False, users=False, roles=[role])` and never `.all()` or the default; `/pace`, `/mystats`, `/progress show` are ephemeral; a public quote needs `share=True` per invocation and still keeps the locator OUTSIDE the bars. Mutation-checked: `ephemeral=False`, `AllowedMentions.all()` and `quiet=False` each fail it, and each used to pass the whole suite. |
| Invariants (7, AST-based) | Timestamp markup only in `timefmt`; book-text tables only in `library` + schema; `os.environ` only in `config`; every `Forbidden`/`NotFound` handler terminal; `mention_everyone` appears nowhere; `timefmt`/`schedule` import no discord; every module imports under a genuinely empty environment (`env={}` subprocess). |

### NEVER RUN -- this is the receiving agent's work queue

Nothing below is a known failure. Each is simply unproven because it needs a
live gateway or the unRAID box itself. The IMAGE is built and exercised -- see
"The container, already built" at the end of this section, and do not redo it.

**Gateway and registration**
1. Gateway login with a real token; the ready line.
2. The **Server Members Intent** is really granted -- privileged, and off it reads
   as an empty roster with no error anywhere.
3. Guild-scoped sync lands and all 22 top-level commands appear instantly.
4. `default_permissions()` really hides the 8 organizer commands in the client.

**Permissions and hierarchy (the silent killers)**
5. MANAGE_ROLES is granted -- labelled **"Manage Permissions"** in the client.
6. The Marginalia role is **above** every `Marginalia YYYY-MM` cohort role.
   Otherwise `add_roles` 403s and it presents as a missing permission.
7. `create_events` (bit 44) really is what event creation needs. discord.py's own
   docstring says `manage_events` and is stale. Never verifiable offline.
8. No channel-level overwrite on `#florilegium` denies what the guild grants.

**Behaviour that only exists on the wire**
9. A role mention from the **interaction** path actually notifies. Interactions
   default to parsing users only; the explicit `AllowedMentions` is the fix. Its
   SHAPE is now pinned offline (`tests/test_ephemeral.py`); whether the real API
   then delivers a ping is not. A silent no-ping is the whole reminder feature
   failing quietly.
10. An **ephemeral response really suppresses others' push notifications**. Every
    book-text reply is ephemeral and that is the actual spoiler defence -- bars
    alone do not stop a lock-screen preview. The flag now reaches
    `send_message` under test; only Discord's own push behaviour is unproven.
11. The persistent `DynamicItem` Join button survives a full restart.
12. Threads come out **public** (2.7.1 defaults to `private_thread`; we pass
    `type=` explicitly).
13. `/schedule` creates guild scheduled events, and the 100 scheduled-or-active
    cap in practice.
14. A real Poll with `multiple=True` behaves as approval voting, and
    `/ballot-result` reads final counts -- Discord's `is_finalized` timing is
    unknown offline. Our own limit validation runs client-side because
    discord.py enforces none of them.
15. `/meeting` end to end: the anchor checkpoint, its two reminders, and that a
    later `/schedule` re-run does not delete them (this was a CRITICAL judge
    finding; the fix and its test are in, but only offline).
16. `/progress show` pagination against real message-length limits.
17. Whether timestamp markup and spoiler bars render inside **embed** titles,
    footers and field names -- open in `docs/ENV.md`.

**The container and the box** -- the image itself is built and exercised; see
"The container, already built" below before you queue any of this.
18. `deploy/watchdog.sh` **on the real box**, from unRAID's User Scripts on
    `*/5 * * * *`. The script's own logic ran end to end elsewhere (6 cases);
    what is unproven is cron on unRAID invoking it, the real paths on your
    array, and a genuinely dead scheduler rather than a synthetic stale
    heartbeat.
19. Backup and **restore** -- an untested backup is not a backup.
20. Reboot survival. unRAID runs its OS from RAM off a USB stick, so verify
    `restart: unless-stopped` brings it back and a reminder that came due during
    the outage fires late-within-grace.
21. NTP on the host.

**Needs two humans, not an agent**
22. Two members in **different timezones** each confirming they see their own
    local time in one identical message.

### The container, already built -- do NOT redo this

Receipt: `.build/receipts/container_built.json` -- NOT in this repository (it stayed on the build laptop); the prose record with the commands is:
`deploy/README.md`, section "What has actually been verified". The docker runtime
used for it was installed for the run and removed afterwards, so there is no
daemon on the build machine now.

- **`linux/amd64` build PASSES.** 54.7 MB compressed, 156 MB on-disk. **Nothing
  compiled from source** -- all 14 pins, `lxml` included, resolved to prebuilt
  manylinux x86_64 wheels.
- **Build-time self-check passes**: `self-check OK: tz Jan-6/Jul-5,
  discord.py 2.7.1`. Jan-6/Jul-5 is CST/CDT, so the zoneinfo database is real
  and DST is live; a stubbed tzdata prints equal offsets and fails the build.
- **`python:3.13-slim` ALREADY ships tzdata** (`2026b-0+deb13u1`, 0 newly
  installed). The older claim that slim images carry no tzdb was wrong. The
  `apt-get install tzdata` line stays as a declared dependency, and the
  self-check, not the apt line, is the real guard.
- **No-config run exits 2**, one log line naming all four missing variables
  (`DISCORD_TOKEN`, `GUILD_ID`, `BOOK_CLUB_CHANNEL_ID`, `MARGINALIA_DB`), no
  traceback, token never echoed.
- **Migrations run in the container**: `user_version=1`, `journal_mode=wal`,
  `integrity_check=ok`, including the FTS5 shadow tables -- so FTS5 is compiled
  into the container's sqlite.
- **Non-root**: `uid=99 gid=100`, unRAID's `nobody:users`, writing files owned
  `99:100` on the host side of the bind mount.
- **Watchdog verified across 6 cases**, both read branches (host `sqlite3` and
  the `docker exec` fallback), including that it **no longer resurrects a
  deliberately stopped container** -- that was a real bug, found and fixed
  (`docker inspect -f '{{.State.Running}}'` guard at `watchdog.sh:47`).
- Not covered: the heartbeat WRITER was synthetic (no real token), so the live
  loop writing its own rows is still item 18's business.

A further container smoke suite was **in progress** as this was written. Its
results are not recorded here or in `deploy/README.md`; check for a newer receipt
under `.build/receipts/` on the build laptop before assuming this list is the whole of it. On the unRAID box itself the container was built and started by `deploy/unraid-install.sh` on 2026-09-05; see `deploy/README.md`.

---

## Getting the code there

`rsync -av --exclude .venv --exclude '*.db*' --exclude .env \
  marginalia-lean/ unraid:/mnt/cache/appdata/marginalia/`

Or clone from git if a remote exists. Either way `.env` must **never** travel --
it is gitignored and excluded above. There is no `.env` in this tree and none
must ever be created here.

Do NOT put the source on `/mnt/user/...`. See the next section.

---

## The database path is a data-loss hazard

`/mnt/cache/appdata/marginalia` -- a **direct** cache-SSD path. Never
`/mnt/user/appdata/...`.

`/mnt/user` is unRAID's shfs FUSE layer. SQLite in WAL mode takes byte-range
locks and keeps `-wal`/`-shm` sidecars beside the db; FUSE's locking semantics
are not that, and this is the widely documented cause of corrupt
Plex/Sonarr/Radarr databases. It also lands writes on the parity array, spinning
disks up for every small WAL write on a box that should be idling.

Get it wrong and you lose the reading history quietly, weeks later.
`deploy/README.md` is the authority; `deploy/docker-compose.yml` hardcodes the
correct bind mount and sets `MARGINALIA_DB=/data/marginalia.db` inside the
container (compose `environment:` overrides `env_file`, so a value in `.env` is
ignored).

---

## The token rule

`.env` on the unRAID box holds exactly one secret: `DISCORD_TOKEN`. Portal ->
Bot -> Reset Token; shown once. `chmod 600 .env`.

Never a chat, a log, a commit, a screenshot, or a command line -- the last one
lands in shell history *and* the process list. `Config.__repr__` redacts it and a
test asserts that; that is a safety net, not a licence. GitHub and Discord both
scan for leaked tokens and auto-invalidate.

If it leaks: Bot -> Reset Token. Immediate and total. Full procedure in
`docs/PORTAL_SETUP.md`.

---

## Running the verification

Invoke the workflow `marginalia-live-verify` with:

```json
{ "root": "/mnt/cache/appdata/marginalia",
  "guild_id": "<guild id>",
  "channel_id": "<#florilegium channel id>",
  "mode": "test" }
```

Use `mode: "test"` against a **throwaway guild first.** Every permission mistake
-- role hierarchy, event creation, the private-thread default -- surfaces there
for free and you can wreck it without touching the real club. Only then re-run
with `mode: "prod"`, which switches the agents into a no-destructive-changes
posture.

### How the workflow is shaped, and why

- **Preflight is parallel and Discord-free.** If the offline suite is red on the
  box, the run **halts before creating a single Discord object** -- otherwise you
  build guild state on a broken tree and have to tear it back down.
- **Connect is one gate.** It proves the members intent, the command sync and the
  role hierarchy *before* anything attempts a role assignment. Failed attempts
  count toward the 10,000-invalid-requests-per-10-minutes limit that ends in a
  Cloudflare IP ban, so it refuses calls it already knows will 403.
- **Live checks are strictly sequential** -- a plain awaited loop, not
  `parallel()`. One bot, one guild: concurrent agents would collide on shared
  state and share a single rate-limit bucket, gaining nothing and inviting 429s.
- **Diagnosis is parallel**, because it is read-only -- code, logs and docs, no
  API calls.
- **Ops is skipped if a live check hard-failed** -- bringing a broken bot up under
  `restart: unless-stopped` with a watchdog just makes it fail forever on a
  schedule.
- Every live check cleans up after itself; objects are prefixed `verify-`.

---

## Known-unverified, stated honestly

One item is neither proven nor believed broken, and the receiving agent should
settle it rather than assume:

1. **`docs/ENV.md` was carried over from the first build.** Its measured
   discord.py 2.7.1 facts still hold (re-checked: the permission integers, the
   `multiple=` keyword, the `private_thread` default), but it also describes
   modules and commands that do not exist in this tree. Treat its *measurements*
   as authoritative and its *architecture* as stale. `docs/SPEC.md` is this
   tree's design of record.

---

## What no agent can do

Two members in different timezones confirming they each see their own local time
in the same message. Everything else about timestamps is machine-checkable; this
part needs two humans.
