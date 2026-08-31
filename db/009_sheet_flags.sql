-- Why a sheet shows no doors.
--
--     psql "$SUPABASE_DB_URL" -f db/009_sheet_flags.sql
--
-- `sheets` recorded which sheet leads its storey and nothing about the others,
-- so a viewer listing eight floor plans showed five of them as "0 doors" with
-- no way to tell a deliberate skip from a failure. Both of these are already
-- worked out during the audit; they were simply not being written down.
--
--     scanned         a door number was actually found on this sheet. False on
--                     an overall plan at 1/16" whose doors the partial plans
--                     already carry at 1/8" -- read, and correctly empty.
--
--     is_enlargement  the sheet blows up one part of the building rather than
--                     drawing the whole floor. Worth showing -- it is how a
--                     person finds one door on a plan too big to read -- but
--                     not where the count comes from.

alter table sheets
    add column if not exists scanned boolean not null default false,
    add column if not exists is_enlargement boolean not null default false;

-- Listing a document's sheets in page order, leads first.
create index if not exists sheets_by_document_page
    on sheets (document_id, page);
