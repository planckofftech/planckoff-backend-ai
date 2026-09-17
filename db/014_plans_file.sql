-- A set whose schedule and drawings arrive as two files.
--
--     psql "$SUPABASE_DB_URL" -f db/014_plans_file.sql
--
-- ---------------------------------------------------------------------------
-- Why a document may have two files
-- ---------------------------------------------------------------------------
-- Plenty of jobs do not ship one PDF. The schedule comes as `Door Schedule.pdf`
-- and the drawings as `Architectural.pdf`, or the schedule is a single sheet
-- pulled out of a set that was too big to send. `audit()` has always been able
-- to locate doors on one file using rows read from another -- that is what its
-- `rows` argument is for -- and nothing exposed it.
--
-- The alternative was to store the two files as two documents and join them in
-- the browser, and it does not work: `save_audit` hangs sheets and detections
-- on one document id, so the schedule would sit on one row and the door
-- locations on another, and every screen would have to reconcile them. A
-- takeoff is one thing. It may simply have come in two envelopes.
--
-- `source_uri` stays the schedule's file, so nothing that reads it changes.
-- `plans_uri` is the drawings, and is null on the ordinary single-file job --
-- where the schedule file *is* the drawings and `source_uri` answers both
-- questions.

alter table documents
    add column if not exists plans_uri text;

comment on column documents.plans_uri is
    'The drawings, when they arrived as a separate file from the schedule. '
    'Null on a single-file set, where source_uri is both.';
