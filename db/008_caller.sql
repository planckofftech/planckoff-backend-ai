-- Let `created_by` hold a real person.
--
--     psql "$SUPABASE_DB_URL" -f db/008_caller.sql
--
-- Six tables carry `created_by uuid references auth.users(id)`, written when
-- the plan was for Supabase Auth to own identity. It does not: the frontend
-- has its own session-cookie auth, and its people live in `team_members`.
--
-- So the foreign key points at a table that will never hold them, and any
-- insert carrying a real user id fails on it. Every one of those columns has
-- been null since the schema was written, for exactly this reason.
--
-- The columns stay and keep their type. Only the constraint goes -- the id is
-- still a uuid, it just belongs to somebody else's table now. Add a foreign key
-- back if `team_members` ever moves into this database.

alter table projects              drop constraint if exists projects_created_by_fkey;
alter table documents             drop constraint if exists documents_created_by_fkey;
alter table corrections           drop constraint if exists corrections_created_by_fkey;
alter table run_log               drop constraint if exists run_log_created_by_fkey;
alter table manual_detections     drop constraint if exists manual_detections_created_by_fkey;
alter table detection_tombstones  drop constraint if exists detection_tombstones_created_by_fkey;

-- Who touched a job, answerable without scanning every table.
create index if not exists corrections_by_person
    on corrections (created_by, created_at desc);
