-- Migration 2. Append-only: this file and schema.sql are never edited once
-- shipped, so a fresh database replays 1 then 2 and lands where an upgraded one
-- already is. Adding these columns to schema.sql too would make the ALTERs below
-- fail with "duplicate column name" on every fresh install.
--
-- The cover ships INSIDE the EPUB, so it costs nothing to keep; a BLOB rather
-- than a file on disk means `sqlite3 .backup` already covers it and there is no
-- image directory to get the permissions wrong on. Both nullable: no cover is
-- the normal case for a plain EPUB and must never fail an ingest.
ALTER TABLE books ADD COLUMN cover BLOB;
ALTER TABLE books ADD COLUMN cover_mime TEXT;
