-- Which version of the reader produced each stored reading.
--
--     psql "$SUPABASE_DB_URL" -f db/018_reader_version.sql
--
-- ---------------------------------------------------------------------------
-- Why a fingerprint is not enough
-- ---------------------------------------------------------------------------
-- A file dropped into a job twice is not read twice: `documents` is unique on
-- (project_id, sha256), and an upload whose bytes are already there returns
-- the stored reading. That is right while the reader stands still and wrong
-- the moment it improves. Every set read before 017 carries no table boxes,
-- and uploading it again handed back the same box-less tables -- the improved
-- reader never saw it. The same fingerprint can hold a reading the current
-- reader would no longer give.
--
-- So each reading records the reader that made it, and a stored reading is
-- reused only when it came from the reader running now. An older one is read
-- again; what a person put there -- corrections, hand-placed boxes, removals --
-- survives that, as it survives /reread.
--
-- Null on every document stored before this: those are read again the next
-- time their file arrives.

alter table documents
    add column if not exists reader_version int;

comment on column documents.reader_version is
    'pipeline.READER_VERSION of the reader that produced this document''s '
    'stored reading. A stored reading is reused for a repeat upload only when '
    'this matches the running reader; otherwise the set is read again.';
