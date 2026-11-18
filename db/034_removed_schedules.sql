-- Schedules a person removed from a document, so a re-read does not bring
-- them back.
--
--     psql "$SUPABASE_DB_URL" -f db/034_removed_schedules.sql
--
--   removed_schedules   [{page, title}] -- the page and the caption as printed.
--                       Written by DELETE /documents/{id}/schedules/{schedule_id};
--                       cleared by DELETE /documents/{id}/removed-schedules.
--
-- Safari Kid's window schedule was read as twelve doors, and the only way to
-- be rid of them was to delete them one at a time -- after which reading the
-- set again put all twelve back. A removal that does not survive a re-read
-- is not one.
--
-- Until this has been run a schedule can still be removed; it is just not
-- remembered, and the response says so.

alter table documents
    add column if not exists removed_schedules jsonb not null default '[]';
