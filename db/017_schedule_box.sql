-- Where each schedule sits on its page, so a viewer can outline it.
--
--     psql "$SUPABASE_DB_URL" -f db/017_schedule_box.sql
--
-- ---------------------------------------------------------------------------
-- Why the numbers were not reaching anyone
-- ---------------------------------------------------------------------------
-- The geometry has always existed. `table_locator.table_bounds` measures the
-- caption, headings and rows off the page's own rulings, and `render_preview`
-- has drawn that rectangle into a PNG since the preview was written.
--
-- It stopped there. A caller drawing its own box -- its own canvas, its own
-- zoom, a square it can make clickable -- had nothing to draw: a stored
-- schedule was `page`, `title`, `headers`, `field_map`, `row_count` and
-- nothing about position. The box was added to `ExtractionResult.tables[]`
-- first, which is the response from reading a PDF then and there, and that is
-- not where a viewer looks: it opens a document that was read last week. So
-- the numbers were computed, drawn into an image, and discarded.
--
-- ---------------------------------------------------------------------------
-- Fractions, not points
-- ---------------------------------------------------------------------------
-- The same choice `detections` makes, and for the same reason: the viewer
-- renders the page at whatever dpi suits its screen, and a fraction is the one
-- form that survives that. Multiply by the page size -- `sheets.width` and
-- `sheets.height`, or the image's own -- to get pixels.
--
-- ---------------------------------------------------------------------------
-- One row per schedule, and why that matters here
-- ---------------------------------------------------------------------------
-- `schedules` already holds one row per table found, because a sheet routinely
-- carries several: a main door schedule, then residential units, then
-- guestrooms. AT&T's reissue prints one schedule as two halves side by side,
-- doors 11-44 on the left and 45-127B on the right, and both are real. A box
-- per document could only ever point at one of them; a box per row points at
-- each.
--
-- ---------------------------------------------------------------------------
-- Null is a real answer
-- ---------------------------------------------------------------------------
-- The vision tier reads a page as an image and returns text, never geometry,
-- so there is no grid to measure and these stay null. A viewer skips the
-- outline rather than drawing a rectangle nobody measured -- the same rule the
-- preview follows, where boxing a page the finder never passed once outlined
-- the MATERIAL KEY legend and labelled it DOOR SCHEDULE.

alter table schedules
    add column if not exists box_x0 real,
    add column if not exists box_y0 real,
    add column if not exists box_x1 real,
    add column if not exists box_y1 real,
    add column if not exists box_source text;

comment on column schedules.box_x0 is
    'Left edge of this schedule on its page, as a fraction of the page width. '
    'The box spans the caption, the headings and the rows.';
comment on column schedules.box_source is
    '''text'' when measured off the page''s own rulings and text layer, '
    '''model'' when the vision tier estimated it from the image. Null '
    'together with the box where nothing could be measured.';

-- `schedules` is read as a table, not through a view, so nothing needs
-- rebuilding here -- unlike `doors`, where `doors_current` expands `d.*` at
-- creation and cannot see a column added later. See the note atop 011.
