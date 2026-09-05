-- Migration 1. Carried over from the previous build's DDL (executed and proven
-- against SQLite 3.51) minus `nomination_votes` and `reading_history`, which
-- duplicated Discord Poll state and a `cohorts.ended_at IS NOT NULL` WHERE
-- clause respectively. 13 logical tables, 15 indexes, 3 triggers.
--
-- There is deliberately no `PRAGMA user_version` here: db.migrate() sets it,
-- inside the same transaction as this script, so a crash cannot leave a
-- half-applied schema claiming to be complete.

-- ---------------------------------------------------------------- books ---
CREATE TABLE books (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  title            TEXT    NOT NULL,
  author           TEXT    NOT NULL DEFAULT '',
  isbn13           TEXT,
  olid             TEXT,
  page_count       INTEGER,
  chapter_count    INTEGER NOT NULL DEFAULT 0,
  word_count       INTEGER NOT NULL DEFAULT 0,
  char_count       INTEGER NOT NULL DEFAULT 0,
  is_public_domain INTEGER NOT NULL DEFAULT 0 CHECK (is_public_domain IN (0, 1)),
  source_sha256    TEXT,
  ingested_at      INTEGER,
  created_at       INTEGER NOT NULL DEFAULT (unixepoch())
);
-- Ingest idempotency key. Partial index so many not-yet-ingested books may
-- coexist with a NULL sha, but an ingested file can never be stored twice.
CREATE UNIQUE INDEX books_sha_uidx ON books (source_sha256) WHERE source_sha256 IS NOT NULL;

-- -------------------------------------------------------------- cohorts ---
CREATE TABLE cohorts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           INTEGER NOT NULL,             -- snowflake, > 32 bits
  channel_id         INTEGER NOT NULL,             -- snowflake
  book_id            INTEGER NOT NULL REFERENCES books (id) ON DELETE RESTRICT,
  cycle_month        TEXT    NOT NULL CHECK (cycle_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  role_id            INTEGER,                      -- snowflake, NULL until role created
  tz_id              TEXT    NOT NULL DEFAULT 'UTC',
  status             TEXT    NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','open','active','closed','archived')),
  meeting_at_utc     INTEGER,
  meeting_tz_id      TEXT,
  meeting_local_wall TEXT,
  signup_message_id  INTEGER,                      -- snowflake, persistent button host
  opened_at          INTEGER,
  closed_at          INTEGER,
  role_revoked_at    INTEGER,                      -- set when the role is stripped
  created_at         INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE (guild_id, cycle_month)                   -- one cohort per month per guild
);
CREATE INDEX cohorts_status_idx ON cohorts (guild_id, status);

-- ------------------------------------------------------- cohort_members ---
CREATE TABLE cohort_members (
  cohort_id    INTEGER NOT NULL REFERENCES cohorts (id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL,                   -- snowflake
  joined_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  left_at      INTEGER,
  role_granted INTEGER NOT NULL DEFAULT 0 CHECK (role_granted IN (0, 1)),
  dnf          INTEGER NOT NULL DEFAULT 0 CHECK (dnf IN (0, 1)),
  dnf_reason   TEXT,
  PRIMARY KEY (cohort_id, user_id)
);
CREATE INDEX cohort_members_user_idx ON cohort_members (user_id);

-- ---------------------------------------------------------- checkpoints ---
-- Stores the instant AND the zone AND the local wall time. All three.
-- The instant alone is correct for a one-shot and WRONG for a recurrence:
-- "every Sunday 19:00 America/Chicago" is not a fixed number of seconds.
CREATE TABLE checkpoints (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  cohort_id         INTEGER NOT NULL REFERENCES cohorts (id) ON DELETE CASCADE,
  idx               INTEGER NOT NULL,              -- 1-based order within cohort
  label             TEXT    NOT NULL,              -- e.g. 'Chapters 1-3'
  unit              TEXT    NOT NULL DEFAULT 'chapter' CHECK (unit IN ('chapter','page')),
  start_ref         INTEGER NOT NULL,              -- first chapter or page, inclusive
  end_ref           INTEGER NOT NULL,              -- last chapter or page, inclusive
  chapter_ceiling   INTEGER NOT NULL,              -- spoiler ceiling once unlocked
  due_at_utc        INTEGER NOT NULL,              -- the instant
  tz_id             TEXT    NOT NULL,              -- IANA zone, e.g. 'America/Chicago'
  local_wall        TEXT    NOT NULL,              -- 'YYYY-MM-DD HH:MM' in tz_id
  dst_note          TEXT    NOT NULL DEFAULT '',   -- '' | 'nonexistent...' | 'ambiguous...'
  is_meeting_anchor INTEGER NOT NULL DEFAULT 0 CHECK (is_meeting_anchor IN (0, 1)),
  thread_id         INTEGER,                       -- snowflake, set when thread created
  thread_created_at INTEGER,
  created_at        INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE (cohort_id, idx),
  CHECK (end_ref >= start_ref)
);
CREATE INDEX checkpoints_due_idx ON checkpoints (due_at_utc);
CREATE UNIQUE INDEX checkpoints_thread_uidx ON checkpoints (thread_id) WHERE thread_id IS NOT NULL;

-- ------------------------------------------------------------ reminders ---
-- UNIQUE(checkpoint_id, kind) makes duplicate scheduling STRUCTURALLY
-- impossible rather than merely avoided. Verified: the second insert of the
-- same (checkpoint_id, kind) raises IntegrityError.
-- grace_secs: a NEGATIVE value means INFINITE grace -- the reminder is never
-- skipped for lateness. 'unlock' uses -1 because a checkpoint must unlock
-- even if the bot was down for a week.
CREATE TABLE reminders (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  checkpoint_id INTEGER NOT NULL REFERENCES checkpoints (id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL
                CHECK (kind IN ('unlock','T-24h','T-1h','meeting-T-24h','meeting-T-1h')),
  due_at        INTEGER NOT NULL,                  -- unix seconds UTC
  grace_secs    INTEGER NOT NULL DEFAULT 3600,     -- NEGATIVE = infinite grace
  status        TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sending','sent','skipped','failed')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  claimed_at    INTEGER,
  sent_at       INTEGER,
  last_error    TEXT,
  payload       TEXT    NOT NULL DEFAULT '{}',     -- JSON object, never a quote body
  created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE (checkpoint_id, kind)
);
CREATE INDEX reminders_pending_idx ON reminders (status, due_at);
CREATE INDEX reminders_claimed_idx ON reminders (status, claimed_at);

-- ----------------------------------------------------- member_progress ---
-- APPEND-ONLY log. Never UPDATE a row; the latest reported_at wins. The log
-- is what makes pace calculation possible at all.
CREATE TABLE member_progress (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cohort_id     INTEGER NOT NULL REFERENCES cohorts (id) ON DELETE CASCADE,
  user_id       INTEGER NOT NULL,                  -- snowflake
  chapter_index INTEGER,
  page          INTEGER,
  percent       REAL CHECK (percent IS NULL OR (percent >= 0.0 AND percent <= 100.0)),
  reported_at   INTEGER NOT NULL DEFAULT (unixepoch()),
  source        TEXT    NOT NULL DEFAULT 'self' CHECK (source IN ('self','inferred')),
  CHECK (chapter_index IS NOT NULL OR page IS NOT NULL OR percent IS NOT NULL)
);
CREATE INDEX member_progress_latest_idx ON member_progress (cohort_id, user_id, reported_at DESC);

-- ------------------------------------------------------- channel_policy ---
-- Maps a channel OR thread id to its spoiler posture. The 'denied' kind is
-- an explicit hard refusal and outranks everything.
CREATE TABLE channel_policy (
  channel_id    INTEGER PRIMARY KEY,               -- snowflake, channel or thread
  guild_id      INTEGER NOT NULL,                  -- snowflake
  cohort_id     INTEGER REFERENCES cohorts (id) ON DELETE CASCADE,
  checkpoint_id INTEGER REFERENCES checkpoints (id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL DEFAULT 'club'
                CHECK (kind IN ('club','chapter_thread','spoiler_tolerant','denied')),
  max_chapter   INTEGER,                           -- pinned ceiling for chapter_thread
  updated_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  CHECK (kind <> 'chapter_thread' OR max_chapter IS NOT NULL)
);
CREATE INDEX channel_policy_cohort_idx ON channel_policy (cohort_id);

-- ---------------------------------------------------------- nominations ---
-- The native Discord Poll owns the votes: read answer_counts off the message
-- once is_finalized is true. poll_answer_id is the join back to this row.
CREATE TABLE nominations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id        INTEGER NOT NULL,                -- snowflake
  cycle_month     TEXT    NOT NULL CHECK (cycle_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  title           TEXT    NOT NULL,
  author          TEXT    NOT NULL DEFAULT '',
  isbn13          TEXT,
  olid            TEXT,
  page_count      INTEGER,
  nominated_by    INTEGER NOT NULL,                -- snowflake
  book_id         INTEGER REFERENCES books (id) ON DELETE SET NULL,
  poll_message_id INTEGER,                         -- snowflake, the native Poll message
  poll_answer_id  INTEGER,                         -- Poll answer id this nomination maps to
  status          TEXT    NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed','on_ballot','approved','rejected','withdrawn')),
  created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE (guild_id, cycle_month, title, author)
);
CREATE INDEX nominations_cycle_idx ON nominations (guild_id, cycle_month, status);

-- ------------------------------------------------------------- chapters ---
-- chapter_index is SPINE order, 1-based. from_toc records whether the ToC
-- supplied this cut point (1) or the spine document boundary did (0).
CREATE TABLE chapters (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id       INTEGER NOT NULL REFERENCES books (id) ON DELETE CASCADE,
  chapter_index INTEGER NOT NULL,
  title         TEXT    NOT NULL DEFAULT '',
  spine_idref   TEXT    NOT NULL DEFAULT '',
  epub_href     TEXT    NOT NULL DEFAULT '',
  from_toc      INTEGER NOT NULL DEFAULT 0 CHECK (from_toc IN (0, 1)),
  char_start    INTEGER NOT NULL DEFAULT 0,
  char_count    INTEGER NOT NULL DEFAULT 0,
  word_count    INTEGER NOT NULL DEFAULT 0,
  para_count    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (book_id, chapter_index)
);

-- ----------------------------------------------------------- paragraphs ---
-- (chapter_index, para_index) is the STABLE citation coordinate and is what
-- /quote prints. char_start is the offset from the start of the book and is
-- what percentage-through is computed from. epub_href + epub_anchor keep an
-- EPUB CFI possible later; we are NOT building CFI now.
CREATE TABLE paragraphs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id       INTEGER NOT NULL REFERENCES books (id) ON DELETE CASCADE,
  chapter_id    INTEGER NOT NULL REFERENCES chapters (id) ON DELETE CASCADE,
  chapter_index INTEGER NOT NULL,                  -- denormalised for the gate's WHERE
  para_index    INTEGER NOT NULL,                  -- 1-based within the chapter
  text          TEXT    NOT NULL,                  -- VERBATIM, never normalised for display
  word_count    INTEGER NOT NULL DEFAULT 0,
  char_start    INTEGER NOT NULL DEFAULT 0,        -- offset from book start
  char_count    INTEGER NOT NULL DEFAULT 0,
  epub_href     TEXT    NOT NULL DEFAULT '',
  epub_anchor   TEXT,
  UNIQUE (book_id, chapter_index, para_index)
);
CREATE INDEX paragraphs_coord_idx ON paragraphs (book_id, chapter_index, para_index);
CREATE INDEX paragraphs_chapter_idx ON paragraphs (chapter_id);

-- ------------------------------------------------------------- para_fts ---
-- EXTERNAL CONTENT fts5 table: it stores the index only and reads the text
-- back out of `paragraphs` on demand, so paragraph text is stored ONCE.
--
-- THE TRIGGERS BELOW DO NOT BACKFILL. They only see rows written while they
-- exist. Bulk ingest MUST finish with:
--     INSERT INTO para_fts(para_fts) VALUES('rebuild');
-- Verified: 'rebuild' is idempotent (3 consecutive rebuilds + integrity-check
-- all pass) and 'integrity-check' passes afterwards.
-- Verified: an ON DELETE CASCADE from books/chapters DOES fire paragraphs_ad,
-- so deleting a book leaves no stale index rows (para_fts count 0, and
-- 'integrity-check' passes). Do not hand-delete from para_fts.
--
-- THE TOKENIZER IS FROZEN AS WRITTEN: unicode61 with remove_diacritics 2,
-- and deliberately NO porter stemmer. Consequences, both measured:
--   * 'cafe' matches stored 'cafe' (diacritics folded) -- desirable.
--   * 'whales' does NOT match stored 'whale' (0 rows) -- accepted.
-- docs/ENV.md section 12 demonstrates tokenize='porter unicode61' and notes
-- stemming is nice for search. Do NOT "fix" the tokenizer to add porter:
-- changing it changes the on-disk index, which makes it a migration 2 with a
-- mandatory 'rebuild', not an edit to this line. /quote promises a verbatim
-- citation, and a stemmed index makes an exact-phrase feature impossible to
-- add later without a reindex.
CREATE VIRTUAL TABLE para_fts USING fts5(
  text,
  content='paragraphs',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER paragraphs_ai AFTER INSERT ON paragraphs BEGIN
  INSERT INTO para_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER paragraphs_ad AFTER DELETE ON paragraphs BEGIN
  INSERT INTO para_fts (para_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER paragraphs_au AFTER UPDATE ON paragraphs BEGIN
  INSERT INTO para_fts (para_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO para_fts (rowid, text) VALUES (new.id, new.text);
END;

-- ---------------------------------------------------------- quote_ledger ---
-- One row per quote served, per user, per book. This is the accounting
-- substrate for the rolling cumulative cap and the abutment rule. It stores
-- COUNTS AND COORDINATES, NEVER THE QUOTE TEXT.
CREATE TABLE quote_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,                  -- snowflake
  book_id       INTEGER NOT NULL REFERENCES books (id) ON DELETE CASCADE,
  cohort_id     INTEGER REFERENCES cohorts (id) ON DELETE SET NULL,
  chapter_index INTEGER NOT NULL,
  para_start    INTEGER NOT NULL,
  para_end      INTEGER NOT NULL,
  word_count    INTEGER NOT NULL,
  quote_count   INTEGER NOT NULL DEFAULT 1,
  public_domain INTEGER NOT NULL DEFAULT 0 CHECK (public_domain IN (0, 1)),
  channel_id    INTEGER,                           -- snowflake
  served_at     INTEGER NOT NULL DEFAULT (unixepoch()),
  CHECK (para_end >= para_start)
);
CREATE INDEX quote_ledger_user_book_idx ON quote_ledger (user_id, book_id, served_at DESC);
CREATE INDEX quote_ledger_user_time_idx ON quote_ledger (user_id, served_at DESC);
CREATE INDEX quote_ledger_span_idx
  ON quote_ledger (user_id, book_id, chapter_index, para_start, para_end);

-- ------------------------------------------------------------ heartbeat ---
-- Liveness for the durable poller. One row per named loop, upserted.
CREATE TABLE heartbeat (
  name       TEXT    PRIMARY KEY,                  -- e.g. 'reminders'
  beat_at    INTEGER NOT NULL,
  tick_count INTEGER NOT NULL DEFAULT 0,
  pid        INTEGER,
  detail     TEXT    NOT NULL DEFAULT ''
);
