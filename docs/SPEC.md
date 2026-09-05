# Marginalia — lean build spec

Ponytail rebuild. Same features as `../marginalia`, a fraction of the code.
That tree is 11,802 module lines + 19,473 test lines with four cogs over 1,000
lines each. It works and its tests pass; it is simply far more code than this
app needs. **Target here: under 2,000 module lines and under 1,000 test lines.**

Reuse, don't re-derive: the DDL was carried over from that build, where it was
executed and proven against SQLite 3.51; `marginalia/schema.sql` is the executed
artifact here. `docs/ENV.md` holds MEASURED discord.py 2.7.1 facts and
**overrides every docstring and doc page**. `.venv` is symlinked — same pins,
same platform, no reason to rebuild it.

## The build graph

Edges are real import dependencies. Everything on a level runs concurrently;
a level starts only when the level above it is on disk and importable.

```
L0  timefmt      db          epub        config          (no deps)
      |           |           |            |
      +-----+-----+-----+-----+------------+
            |           |
L1      schedule    library     reminders                (import L0)
            |           |           |
            +-----+-----+-----+-----+
                  |
L2        bot   cogs/club   cogs/reading   cogs/quote     (import L0 + L1)
                  |
L3            integrate                                   (suite, prune, README)
```

| Node | File(s) | Budget | Owns |
|---|---|---|---|
| `timefmt` | `marginalia/timefmt.py` | ~90 | `<t:>` markup, local wall → UTC, DST |
| `db` | `marginalia/db.py`, `marginalia/schema.sql` | ~130 | aiosqlite, pragmas, migrations |
| `epub` | `marginalia/epub.py` | ~200 | spine+ToC → chapters/paragraphs |
| `config` | `marginalia/config.py` | ~45 | env → frozen dataclass |
| `schedule` | `marginalia/schedule.py` | ~110 | checkpoint + reminder planning, pace |
| `library` | `marginalia/library.py` | ~280 | ingest, FTS5, **the spoiler gate**, quote budget |
| `reminders` | `marginalia/reminders.py` | ~150 | durable poller, claim, reap, heartbeat |
| `bot` | `marginalia/bot.py`, `__main__.py`, `__init__.py` | ~100 | client, intents, `setup_hook` |
| `cogs/club` | `marginalia/cogs/club.py` | ~220 | join/leave/roster, nominate, poll, book identity |
| `cogs/reading` | `marginalia/cogs/reading.py` | ~240 | schedule/meeting/next/pace/progress set\|show, threads |
| `cogs/quote` | `marginalia/cogs/quote.py` | ~130 | quote/find, ingest, purge |
| `cogs/_views` | `marginalia/cogs/_views.py` | ~206 | every embed and view, all three cogs. No client, no db, no I/O -- which is what makes the card tests client-free. Merged 2026-09-04 from two modules two agents built in parallel; the surviving `cover_file()` DERIVES the attachment filename from the stored media type, because a PNG served as `cover.jpg` renders inconsistently. |

### What was merged, and why

- **`search.py` folded into `library.py`.** It existed only so `spoiler.py`
  could wrap it — an interface with exactly one consumer, which ponytail
  forbids. One module now owns *every* SQL statement that touches book text,
  which is strictly more auditable than two: the grep test becomes "no other
  file mentions `paragraphs` or `para_fts`."
- **`quotebudget.py` folded in too.** Same data, same boundary.
- **7 cogs → 3**, grouped by user intent: joining and picking the book,
  reading it, quoting it. Discord's command picker fuzzy-searches, so file
  grouping is invisible to members.
- **`metadata.py` dropped from v1, and so is the Open Library call.** The
  organizer types title and author into the `/nominate` modal, so enrichment
  buys a cover thumbnail in exchange for a network call on the interaction path,
  a rate-limit concern, and a `Config` field. Rung 1: it does not need to exist.
  `cogs/club.py` makes **no HTTP request of its own.** If someone wants covers
  later, the field goes in `config.py` — never a second `os.environ` reader.

  ADDED 2026-09-04: the EPUB's OWN cover needs none of that, because it is
  already inside the file ingest opens. `epub.parse()` extracts it — EPUB3
  `properties="cover-image"`, then EPUB2 `<meta name="cover">`, then a
  `cover.jpg`/`cover.png` href, then nothing — and migration 2 stores it as
  `books.cover` BLOB plus `books.cover_mime`. jpeg/png only, magic-byte
  verified because mislabelled manifest entries are common, and capped at 2 MiB
  checked against `getinfo().file_size` BEFORE `read()` so a zip-bombed entry is
  never decompressed. `library.cover(db, book_id)` hands it out. **No cover is
  normal and can never fail an ingest** — that swallow-and-continue guard is
  mutation-pinned. Calibre is still not involved and still not needed.

  Migration files live at `marginalia/migrations/`, NOT root-level, because the
  Dockerfile only does `COPY marginalia` — a root-level `migrations/` would not
  ship in the image and migration 2 would fail on the server and nowhere else.
  `schema.sql` IS migration 1; do not add later columns to it, or a fresh
  install replays 1 then 2 and the ALTER fails with "duplicate column name".

### Schema: 15 tables → 13

Start from the previous build's proven DDL. Two deletions, both rung 1 (does
this need to exist at all):

- **`nomination_votes` — delete.** A native Discord Poll owns the votes. Read
  `answer_counts` once `is_finalized` is true. Storing them duplicates
  Discord's state and invites drift.
- **`reading_history` — delete.** It is `cohorts` rows with `closed_at IS NOT
  NULL` (`close_cycle` sets that alongside `status='closed'`). A `WHERE` clause,
  not a table. CORRECTION 2026-09-04: this spec originally said `ended_at`,
  a column that does not exist in `schema.sql`. The `club` agent caught it and
  reported rather than guessing. `shortlist()` uses `c.closed_at IS NOT NULL`.

Keep everything else. `channel_policy` is the per-thread ceiling mechanism,
`quote_ledger` is the anti-reconstruction control, `heartbeat` is one row the
watchdog reads. All load-bearing.

## Interfaces

Signatures are binding — a renamed parameter silently breaks the level below.

### L0 `timefmt` — must NOT import discord

```python
STYLES = "tTdDfFR"                       # 7. Discord documents these; s/S unused.

def unix(dt: datetime) -> int            # aware only; raises ValueError on naive
def ts(dt: datetime | int, style: str = "F") -> str      # "<t:1788...:F>"
def when(dt: datetime | int) -> str      # f"{ts(x,'F')} ({ts(x,'R')})"

@dataclass(frozen=True)
class Wall:
    instant: datetime                    # aware UTC
    note: str | None                     # "gap: pushed forward" | "ambiguous: took earlier"

def resolve_wall(d: date, t: time, tz: str) -> Wall
def weekly(first: Wall, count: int, tz: str) -> list[Wall]
```

**DST policy, non-negotiable.** Advance the *naive* local calendar, then
resolve to UTC. Never add a `timedelta` to an aware datetime — that is
wall-clock arithmetic and silently shifts the hour across a boundary. A
nonexistent local time (spring-forward gap) is pushed forward past the gap; an
ambiguous one (fall-back) takes the **earlier** instant (`fold=0`) so reminders
fire early rather than late.

### L0 `db`

```python
class Database:
    def __init__(self, path: str)
    async def connect(self) -> None      # WAL, synchronous=NORMAL, foreign_keys=ON,
                                         # busy_timeout=5000, row_factory=aiosqlite.Row
    async def migrate(self) -> int       # PRAGMA user_version ladder; returns version
    async def close(self) -> None
    def tx(self) -> AsyncContextManager   # transaction
    async def one(self, sql, *a) -> aiosqlite.Row | None
    async def all(self, sql, *a) -> list[aiosqlite.Row]
    async def run(self, sql, *a) -> aiosqlite.Cursor
```

`foreign_keys` is OFF by default in SQLite — that is why it is explicit.
`user_version` cannot be parameterized; validate it is an `int` and
interpolate. Bump it inside the same transaction as its DDL.

### L0 `epub`

```python
@dataclass(frozen=True)
class Para:  chapter_index: int; para_index: int; text: str; char_start: int; href: str; anchor: str | None
@dataclass(frozen=True)
class Chapter: index: int; title: str; alt_title: str | None
@dataclass(frozen=True)
class Book:  title: str; author: str; chapters: list[Chapter]; paras: list[Para]; total_chars: int

class EncryptedEpubError(Exception): ...

def parse(path: str | Path) -> Book
```

stdlib `zipfile` + `lxml` only. **Not** `ebooklib`/PyMuPDF (AGPL — a bot is a
network service, so §13 reaches it) and not `html2text` (GPL, and it flattens
to Markdown, destroying the paragraph indices the citations depend on).

**The resolving rule: the spine is ground truth for text order; the ToC is a
set of labelled cut points into it.** Never build the chapter list from either
alone. Flatten the linear spine (skip `linear="no"`) keeping block indices —
never to one string — then resolve each ToC `href#frag` onto that order.

Handle, in one pass each, no separate code path per case: many chapters in one
spine file addressed by fragment; Calibre `*_split_NNN.html`; front/back matter
that must not become chapter 1; `<span>` ToC entries with no href (labels, not
destinations); dangling fragments; `linear="no"`; NCX `playOrder` disagreeing
with document order (trust document order); ToC title ≠ in-document heading
(keep both); no usable ToC (fall back to `h1`/`h2`, then one chapter per spine
file).

DRM: read `META-INF/encryption.xml` but distinguish benign IDPF font
obfuscation (`idpf.org/2008/embedding`) from real encryption. Raise
`EncryptedEpubError`. Never attempt circumvention.

Two user-visible chapter-list rules, both pinned by characterization tests
added 2026-09-04 before `epub.py` was refactored:

- A short part/book/volume/section heading FOLDS into its first child chapter,
  keeping the part's start position and taking the child's label. "Short" means
  the part page's OWN text before that first child is under `_MERGE_WORDS`
  (250) — **not** that the child chapter is short. CAVEAT: in the common shape
  (`<h1>Part Two</h1>` immediately followed by the chapter heading) that sum is
  the 2 words of the heading itself, so the threshold never blocks the merge; a
  25,000-word child folds the same as a 250-word one. It only blocks when the
  part page carries a real blurb. The behaviour is what we want; the number is
  close to decorative. Do not "fix" it into measuring the child — that would
  start emitting empty "Part Two" chapters.
- The per-spine-file LAST RESORT (no usable ToC, no headings) titles chapters via
  `_stem_title()`: a stem that is only filename noise plus digits
  (`ch_split_000`, `part0004`, `index_split_002`) becomes a positional
  `Chapter N` from the cut index; any other stem is tidied and title-cased
  (`my-intro` -> `My Intro`). Before 2026-09-04 the raw stem shipped, so a
  Calibre book could yield a chapter literally called `ch_split_000` in `/next`,
  in quote citations and in thread names. `_SPLIT` still only skips
  `_split_NNN` for NNN >= 1. Cosmetic, in a path most books never reach.

### L0 `config`

```python
@dataclass(frozen=True)
class Config:
    token: str; guild_id: int; channel_id: int; db_path: str
    def __repr__(self) -> str            # MUST redact token

class ConfigError(Exception): ...
def load(env: Mapping[str, str] | None = None) -> Config
```

The only module permitted to read `os.environ`. Match `../.env.example`.

### L1 `schedule` — pure, no I/O, no discord, no db

```python
@dataclass(frozen=True)
class Checkpoint: seq: int; label: str; through_chapter: int | None
                  page_from: int | None; page_to: int | None; due: Wall; kind: str
@dataclass(frozen=True)
class Reminder:   kind: str; due: datetime; grace: int   # grace < 0 == infinite

GRACE = {"unlock": -1, "T-24h": 6*3600, "T-1h": 45*60}

def plan(total: int, weeks: int, weekday: int, at: time, tz: str,
         by: str = "pages", *, start: date) -> list[Checkpoint]
# `start` is required and keyword-only. ACCEPTED DRIFT, 2026-09-04: the original
# signature omitted it, but a weekday alone does not name a week, so the module
# would have had to call date.today() -- a clock read inside a module this spec
# declares PURE, and the thing that makes it exhaustively testable. Callers pass
# the cycle's start date explicitly. A spec-literal call now raises TypeError
# rather than silently scheduling from "today".
def reminders_for(cp: Checkpoint) -> list[Reminder]
def reminders_for_meeting(due: Wall) -> list[Reminder]   # meeting-T-24h, meeting-T-1h;
                                                         # same bounded GRACE, no unlock twin
def pace(total: int, done: int, start: datetime, end: datetime, now: datetime) -> tuple[float, int]
```

Cover the whole book exactly once — no gaps, no overlaps, no empty checkpoint;
spread the remainder into the EARLIEST parts so the last week is never
double-length.

Two behaviours moved here from deleted docstrings (comment budget):

- `plan()` RAISES on degenerate input rather than coalescing. Coalescing would
  have to either change the cadence the organizer asked for, or emit an empty
  checkpoint that unlocks nothing — both worse than a clear error.
- `pace()` treats a zero-length or inverted schedule as `frac = 1.0` once
  `now >= end`, else `0.0`. That is what avoids `ZeroDivisionError` without a
  special case. `unlock` gets infinite
grace because it is a durable state transition; "starts in 1 hour" delivered
three hours late is actively false, hence the bounded windows.

### Book identity — ONE `books` row per book

RESOLVED 2026-09-04 (H7). `library.ingest` creates the row; `/cycle-open` takes
an optional `book:` id — the one `/ingest` prints — and opens the cohort ON that
already-ingested row, validating `ingested_at IS NOT NULL` first (organizer-typed
input at a trust boundary; a wrong id silently rebuilds the bug below).

Before this, `open_cycle` created its own row and `ingest` created a different
one, so with the DEFAULT `by="pages"` the cohort's `books.chapter_count` was 0 →
every checkpoint ceiling floored to 0 → every thread pinned to 0 → **quoting
refused everywhere**. The ebook half of the product was dead on arrival, and the
orphan row was also what made the C1 leak reachable via autocomplete.

With no `book:`, the cohort gets a metadata-only row from the nomination and
everything except quoting works — reminders, unlock threads, roster, nomination,
ballot. Quoting stays refused because `ceiling()` requires `ingested_at` and
`chapter_count > 0`, which is correct (no text exists) and leaks nothing.

Ordering caveat: pages-mode ceilings are pro-rated from `books.chapter_count`, so
attach the book BEFORE `/schedule`. Attaching later needs a `/schedule` re-run.

CORRECTION 2026-09-04: this spec previously called that re-run "harmless." It was
not. `apply_plan`'s pending-reminder purge was scoped to the COHORT rather than to
the weekly checkpoints it rewrites, so a re-run silently deleted the meeting's
`meeting-T-24h`/`meeting-T-1h` rows while leaving the anchor and
`cohorts.meeting_at_utc` intact — every surface still said the meeting was
scheduled and no reminder ever fired. The purge is now scoped with
`AND is_meeting_anchor = 0`. Do not widen it back.

Shortening a cycle CASCADEs the swept weeks' `channel_policy` rows away, so an
already-unlocked thread becomes UNMAPPED and quoting in it fails closed
(`Ceiling(0, 'fail_closed')`). Deliberate, and pinned by a test. Re-mapping an
orphaned thread is not the small fix it looks like: it needs a policy answer for
which surviving checkpoint a week-6 thread should point at, and every candidate
RAISES a pin on an already-open public thread — which is precisely judge 3's
C3-1 failure class. Trading a silent safe refusal for a possible leak is the
wrong direction. If the silence is the problem, make the refusal message name the
cause; do not re-map.

RESOLVED (M-2): a re-plan now re-syncs an existing thread's pinned
`max_chapter` from `apply_plan`, not from `unlock` — `unlock` early-returns on
`thread_id`, so it never runs again after a re-plan and an upsert there would be
dead code. Two invariants that keep this from becoming a spoiler leak:

- `channel_policy.max_chapter` is only ever written from the OWNING checkpoint's
  `chapter_ceiling` — in `unlock` at create time, or in `apply_plan` on re-plan.
  The `WHERE checkpoint_id = ?` keeps another checkpoint's or cohort's value out
  of reach.

  CORRECTION 2026-09-04 (judge 3, C3-1): this bullet previously concluded "so a
  pin can never exceed what that week unlocks." That is the FALSE half. A pin
  cannot exceed its own week's ceiling — but **a re-plan can move that week's due
  date into the FUTURE**, so the pin is not self-limiting. Before M-2 the
  due-ness came free: `unlock`'s `ON CONFLICT DO NOTHING` meant a pin was always
  a snapshot of a checkpoint that had already fired, so `pin ⊆ due-set` held by
  construction and nothing had to enforce it.

  `ceiling()` therefore CLAMPS at read time:
  `min(max_chapter, MAX(chapter_ceiling) over DUE checkpoints)`. Do not remove
  that `min()` — it is now the only thing making `pin ⊆ due-set` true, and
  without it a thread named "Chapters 1-5" serves chapter 17 verbatim after an
  organizer re-runs `/schedule` with `start` left blank. Clamping at the READ is
  deliberate: it holds no matter how the row was written.

  Note the thread pin is clamped by the due set but is NOT `min()`'d with the
  member's own reported progress. The cohort branch does that; the thread branch
  returns first.

  And that `min()` applies only to a CHAPTERS-mode plan. `mine` reads
  `member_progress.chapter_index`, and a pages plan records only `page` — so in
  the DEFAULT pages mode a member's own report does not narrow their ceiling at
  all. Inert by design, not by accident: a page number says nothing reliable
  about which chapter someone reached, and converting one into an approximate
  chapter is exactly the guess that ends up WIDENING a ceiling. Rounding up
  hands out an unreached chapter; flooring re-creates the H3-1 brick more
  quietly, since per-edition pagination and front matter mean a member 90%
  through can be capped by an estimate page counts cannot support.

  `mine` is therefore explicitly gated on the cohort's live plan being in
  chapters, via an `EXISTS` over `checkpoints`. That gate MUST exclude the
  meeting anchor: `set_meeting` writes the literal `unit = 'chapter'` with
  `is_meeting_anchor = 1`, so an unfiltered `EXISTS` would find it and keep a
  stale chapter cap alive in a pages cohort — a no-op for precisely the cohorts
  that have a wrap-up meeting. `unit_of()` filters the anchor for the same
  reason. Both are mutation-pinned.
- `unlock` keeps its `ON CONFLICT DO NOTHING` deliberately. A blind upsert there
  could re-pin a channel whose policy row belongs to a different checkpoint.

### The wrap-up meeting

RESOLVED 2026-09-04 (H5). The once-per-cycle wrap-up meeting is a checkpoint row
with `is_meeting_anchor=1` at the reserved `idx 0`, covering nothing and
unlocking nothing (`chapter_ceiling = 0`), so reminders can hang off it —
`reminders.checkpoint_id` is NOT NULL — and `apply_plan`'s surplus sweep
(`DELETE ... idx > len(cps)`) cannot remove it. `bot.deliver()` needs no new
branch: no `unlock` kind means no thread, so it announces in the cohort channel.

The `chapter_ceiling = 0` matters for safety, not tidiness: `ceiling()` takes
`MAX(chapter_ceiling)` over DUE checkpoints, so a meeting dated early in the
`/progress set` REFUSES before `/schedule` has run: there is no unit to record
against, and guessing `'chapter'` permanently caps a pages-mode member — they
could neither see nor clear the stale row, and `/progress show` would report
"Nothing reported yet" while the cap held all month. `unit_of()` returns
`str | None` so a future caller forwarding a guessed unit fails loudly (a
`KeyError` on the column map) rather than bricking someone quietly.

cycle would otherwise unlock the whole book. `unit_of`/`pace_of` also exclude the
anchor so it cannot corrupt pace math.

### L1 `library` — the single choke point for book text

```python
async def ingest(db: Database, path: str) -> int          # idempotent on sha256; deletes source
@dataclass(frozen=True)
class Ceiling: chapter: int; reason: str
async def ceiling(db, guild_id, channel_id, user_id, book_id) -> Ceiling
@dataclass(frozen=True)
class Hit: chapter: int; chapter_title: str; para: int; text: str; snippet: str; pct: int
async def search(db, book_id, q: str, ceil: Ceiling, k: int = 3) -> list[Hit]
def locator(h: Hit, book_title: str, author: str) -> str
def spoiler(locator: str, text: str) -> str               # escapes literal ||
async def budget(db, user_id, book_id, words, paras) -> str | None   # None == allowed
```

**No other module may name `paragraphs` or `para_fts`.** A test greps for it.

Gate in SQL — `AND c.chapter_index <= :ceiling` — never in a prompt, never as a
Python post-filter. **Fail closed** — unknown user, unmapped channel, no live
cohort, missing book, `None` or negative → chapter 0 and refuse.

`ceiling()` precedence, first match wins: explicit `denied` → refuse;
spoiler-tolerant channel → no gate; **not a member of the scoping cohort →
refuse**; thread with a pinned `max_chapter` → that; else cohort
`checkpoint_chapter`, `min()`'d with the member's own progress for an ephemeral
reply.

RESOLVED 2026-09-04 (M8): a chapter thread is PUBLIC by design, so its pin must
NOT outrank membership — otherwise anyone in the guild who never joined the
cohort gets book text there. "Fail closed on unknown user" beats "thread pin
wins." `spoiler_tolerant` is the one branch above the membership check: that row
IS an operator's explicit opt-out of the gate, and nothing in the codebase ever
writes one.

Also scoped 2026-09-04 (C1): a thread pin applies only to the book its live
cohort is actually reading, and only `status IN ('open','active')` cohorts scope
anything. Without the first, `/quote book:<other>` in a chapter-5 thread returned
another book's chapters 1-5; without the second, last month's final thread
(pinned at 34) unlocked chapter 34 of the new book on day one.

FTS5: `bm25()` returns **negative** scores so plain ascending `ORDER BY` is
best-first; `snippet()`'s token count must be 1–64; keep `detail=full`; triggers
do **not** backfill, so bulk ingest must run
`INSERT INTO para_fts(para_fts) VALUES('rebuild')`. Sanitize user queries —
`(a OR b) c` is a syntax error in FTS5 — so a malformed query returns nothing
rather than raising.

Budget: 75 words/quote, 3/response, 10/hour, 40/day, and the one that actually
matters — **2% of the book per user per rolling 30 days**, plus refusal of a
range overlapping *or abutting* one served in the last 24h (1–10 then 11–20
then 21–30 reconstructs a chapter while each request looks innocent). Never a
whole chapter verbatim. Public-domain fast path skips the caps.

### L1 `reminders`

```python
class Poller:
    def __init__(self, db: Database, deliver: Callable[[Row], Awaitable[None]])
    async def tick(self, now: int) -> tuple[int, int]     # (sent, skipped)
    async def reap(self, now: int) -> int
```

Take the clock as a parameter and the delivery as an injected callback — that
is what makes the whole loop testable with no Discord and no token. A
`tasks.loop(seconds=30)` wrapper lives in `bot.py`, not here.

Claim atomically: `UPDATE reminders SET status='sending' … WHERE id=? AND
status='pending'`, then require `rowcount == 1`. Reap rows stranded in
`sending` (`claimed_at < now-600`) back to `pending`, or to `failed` at
`attempts >= 3`. Write the heartbeat every tick including a zero-work one —
process liveness is not scheduler liveness. `discord.Forbidden` and `NotFound`
are **never** retried; the invalid-request ceiling is 10,000 per 10 minutes and
ends in a Cloudflare IP ban. At-least-once is accepted: a duplicate ping is
cheaper than a missed checkpoint.

### The poller restart backoff

Moved here from an 11-line docstring in `bot.py` (comment budget). The tension:
a TRANSIENT fault must recover, because a dead poller silently stops reminding
and systemd/Docker cannot help — the process is perfectly healthy, only the task
died. But a PERSISTENT fault must not spin: restart-forever floods the log and
can burn the 10,000-invalid-requests-per-10-minutes ceiling into a Cloudflare IP
ban on the host.

Hence `5, 10, 20, 40, 60` seconds capped, `RESTART_RESET=600` so an old fault is
forgiven rather than slowing tomorrow, and after `RESTART_GIVE_UP=6` the loop
stays dead ON PURPOSE — the stale heartbeat is what hands `deploy/watchdog.sh`
(`STALE=600`) a genuinely dead scheduler instead of a bot that looks fine
forever. Better visibly dead in two minutes than invisibly broken for a week.

The `135s < 600s` relation between give-up and the watchdog threshold is asserted
in `tests/test_bot.py`, not just written in a shell comment. Raising either
constant fails the suite.

Cog loading FAILS FAST (resolved 2026-09-04). It used to swallow
`commands.ExtensionError` and log a warning — a build-order accommodation from
when the cogs did not exist yet. That meant a broken cog let `setup_hook` sync a
REDUCED command tree: eight commands silently absent from Discord, bot green.
A bot that refuses to boot is fixed in minutes; a bot missing eight commands is
debugged for an evening.

### L2 `bot`

Intents: `guilds`, `members`, `guild_scheduled_events` only. Explicitly
`message_content = False`, `presences = False`.

`setup_hook` does everything: load cogs, `add_view` for persistent views,
`add_dynamic_items(Class)` — the class, not an instance — and the guild-scoped
sync. **Never `on_ready`**: it fires on every reconnect and the cap is 200
command creates per day per guild. Load cogs defensively so a partial tree
boots. Owns the `tasks.loop` wrapper around `Poller.tick`.

### L2 cogs

Only TWO code paths mention a role, and both pass `AllowedMentions` explicitly:
`club._reply(..., role=)` and `bot.deliver`. `cogs/reading.py` mentions no role
at all, which is why it has no `_reply` helper and passes no `AllowedMentions` —
harmless today, but it means the first person to add a role mention there gets a
reply that renders as blue text and pings NOBODY, with nothing failing. If you
add one, route it through a helper the way `club.py` does.

Every user-visible time goes through `timefmt.when()`. Every role mention
passes `AllowedMentions(everyone=False, users=False, roles=[role])` —
interactions and webhooks default to parsing users only, so a role mention from
an interaction otherwise pings nobody. Anything derived from book text is
ephemeral by default: an ephemeral reply generates no push notification for
anyone else, which is what actually defeats mobile lock-screen spoiler leaks.
Never echo an out-of-range question publicly — the refusal itself spoils.
Organizer commands gate with `default_member_permissions="0"`. There are 24
commands in the tree (measured by loading the cogs, not counted): the
organizer-gated set is `/cycle-open`, `/cycle-close`, `/ballot`,
`/ballot-result`, `/schedule`, `/meeting`, `/ingest`, `/purge_book` and
`/status`.

`/status` added 2026-09-04. It is the only way to see whether the bot is
WORKING rather than merely running — before it, that meant opening a shell and
querying SQLite. Ephemeral, organizer-gated, one embed coloured by worst state:
red on any failed reminder or a heartbeat older than `STALE_BEAT=180`s, yellow
on any skipped reminder, green otherwise. Fields: heartbeat age, cohort,
schedule (fired/total, next due), the reminder queue as pending|sent|skipped|
failed, and whether a book is ingested. The 180s threshold deliberately sits
BELOW `deploy/watchdog.sh`'s `STALE=600`, so an organizer sees a stalled
scheduler before the watchdog restarts the container out from under them.

PRESENTATION, 2026-09-04. A served passage, a `/find` citation list and every
refusal are EMBEDS — barred text goes in the DESCRIPTION, which is the only
place Discord renders spoiler bars, with each locator outside and above its own
bar. Never put barred text in a title or a footer. One-line confirmations
(`/progress set`, `/join`, `/leave`, `/cycle-close`) stay plain strings on
purpose: a titled embed around `Noted: chapter 42.` reads slower, not richer.
Two traps worth keeping in mind — an embed CANNOT ping, so a role mention moved
into one silently stops notifying; and `_fit()` takes the cap as an argument
because 2000 is right for message content and 4096 for a description, while the
whole-block truncation rule must survive either way (clipping inside `||...||`
leaves the bars unclosed and the tail renders in plaintext).

Threads: **always pass `type=ChannelType.public_thread`** — the library
defaults to `private_thread`. `auto_archive_duration` ∈ {60, 1440, 4320, 10080}
— use 10080.

Poll: the keyword is **`multiple=`**, not `allow_multiselect=` (a hard
`TypeError`). discord.py validates **none** of Poll's limits, so check ours
before sending or take an HTTP 400: ≤10 answers, question ≤300 chars, answers
≤55. Wait for `is_finalized` before reading counts; absent results means
unknown, not zero. `create_scheduled_event` has **no `recurrence_rule`** in
2.7.1 — generate discrete events. Creation needs `create_events` (bit 44); the
docstring saying `manage_events` is stale.

## Tests: one runnable check per non-trivial thing

The old tree has 845 test functions. That is not diligence, it is volume.
Ponytail: non-trivial logic leaves **one** check behind — the smallest thing
that fails if the logic breaks. Trivial one-liners get none.

Each node writes `tests/test_<node>.py` itself. **Budget ~60 lines of test per
node, and spend it where a bug is silent:**

- `timefmt` — the DST boundary, and the **negative control**: prove
  `aware + timedelta(weeks=1)` gives the wrong wall time and that we don't do
  it. Without that control the positive test can pass by accident.
- `db` — FK cascade fires, `UNIQUE(checkpoint_id, kind)` raises, `migrate()`
  twice is a no-op, snowflakes past 2^53 round-trip exactly.
- `epub` — three synthetic EPUBs built with `zipfile`: fragment-addressed,
  Calibre-split, no-ToC. Plus DRM raises.
- `schedule` — coverage is exact for several sizes; wall time holds across DST.
- `library` — **ceiling 5 hides a chapter-20 phrase, ceiling 34 reveals it.**
  Both halves, or the test proves nothing. Plus `rebuild` is required, `||` is
  escaped, the 2% cap halts a sequential walk, abutting ranges refused.
- `reminders` — two concurrent `tick()`s deliver exactly once; restart
  survival; reap recovers a crashed send; `Forbidden` is terminal.
- cogs — state transitions only, no Discord client.

Do not test getters, dataclass construction, or that a constant equals itself.

## Invariants (a test greps each)

1. `"<t:"` appears only in `timefmt.py`.
2. `paragraphs` / `para_fts` — the TEXT tables — named only in `library.py` and
   `schema.sql`. Scope clarified 2026-09-04: this invariant is about ungated
   access to book *text*, which is what can spoil. `books` metadata (title,
   author, counts) carries no spoiler risk, so `cogs/quote.py` querying it
   directly for autocomplete, the locator byline, and `/purge_book` is
   in-bounds. Wrapping those three in `library` would be three functions with
   one consumer each. Check this invariant by matching SQL identifiers, not
   words — `epub.py` says "ordered paragraphs" in prose and `timefmt.py`'s
   docstring says "does NOT import discord", both of which fool a word grep.
3. `os.environ` read only in `config.py`.
4. No `except` for `Forbidden`/`NotFound` retries — every handler terminal.
5. `mention_everyone` appears nowhere.
6. `timefmt.py` and `schedule.py` do not import `discord`.
7. Every module imports with no token and no Discord connection.

## Ponytail rules for every node

Stop at the first rung that holds: does it need to exist; does it already exist
here; does the stdlib do it; does a native platform feature cover it; does an
installed dep solve it; can it be one line; only then write the minimum.

No abstraction with one implementation. No dependency beyond the four already
pinned. No boilerplate for later. Deletion over addition, boring over clever,
fewest files. Mark a deliberate corner-cut with a known ceiling as
`# ponytail: <ceiling>, <upgrade path>`.

**Not lazy about:** understanding the problem before picking a rung; input
validation at trust boundaries; the spoiler gate; DST; the reminder claim;
anything explicitly requested. A small diff in the wrong place is a second bug,
not laziness.
