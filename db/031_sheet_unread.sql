-- The parts of a sheet the vision pass was asked to read and did not.
--
--     psql "$SUPABASE_DB_URL" -f db/031_sheet_unread.sql
--
--   unread   [{page, sheet, x0, y0, x1, y1, reason}] -- rectangles as fractions
--            of the page. Written by the plan pass; a reprocess that reads a
--            region clears it. Empty when the whole sheet was read.
--
-- Until now a pass that stopped part-way said so in a warning and nothing
-- else: the part not read was nowhere, so no one could see which part it was
-- or have it read. Each region here can be sent back to
-- POST /documents/{id}/reprocess as one of `regions`.
--
-- The backend stores without it until this has been run -- the column is
-- written in its own call, which fails alone.

alter table sheets add column if not exists unread jsonb not null default '[]';
