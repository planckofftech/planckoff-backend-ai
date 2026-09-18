-- Where a door's number is printed, and the shape it is drawn in.
--
--     psql "$SUPABASE_DB_URL" -f db/016_detection_tag.sql
--
-- ---------------------------------------------------------------------------
-- Why these are columns and not a nested object
-- ---------------------------------------------------------------------------
-- A detection is stored flat -- `x0..y1` for the box, `hinge_x..end_deg` for
-- the swing -- and read back with `select *`. `DetectedDoorOut` nests the same
-- values under `location` and `arc`, but that shape belongs to the audit
-- response; nothing reading a stored document has ever seen it. Adding these
-- to the response alone put them where no caller looks.
--
-- ---------------------------------------------------------------------------
-- What they are for
-- ---------------------------------------------------------------------------
-- A viewer wants to highlight the number rather than box the door: on a
-- crowded plan the number is what a person looks for, and a far better click
-- target than a nine-point rectangle.
--
-- `x0..y1` is the door -- the arc, where one was measured. These are the label
-- pointing at it, which used to be discarded the moment an arc was found, so a
-- measured door kept nothing saying where its number was printed.
--
-- `tag_shape` is read off the drawing by binning the angles of the edges
-- around the glyph, the same way wall tags are found: a hexagon is 0/60/120, a
-- circle hits every angle there is. Empty is a real answer and a common one --
-- measured across two sets, one bubbles all 204 of its numbers (139 circles)
-- and the other prints 62 of 63 bare against the wall. A viewer traces the
-- shape when there is one and falls back to a plain highlight when there is
-- not.
--
-- Null on a door found with no number at all, which is what the vision pass
-- returns and is the case worth looking at rather than hiding.

alter table detections
    add column if not exists tag_x0 real,
    add column if not exists tag_y0 real,
    add column if not exists tag_x1 real,
    add column if not exists tag_y1 real,
    add column if not exists tag_shape text;

comment on column detections.tag_x0 is
    'Where the door number is printed, as a fraction of the page. The box in '
    'x0..y1 is the door itself.';
comment on column detections.tag_shape is
    'circle, square, hexagon, diamond -- or empty where the number is printed '
    'bare, which is common and not a failure.';

-- `detections` is read as a table, not through a view, so nothing needs
-- rebuilding here -- unlike `doors`, where `doors_current` expands `d.*` at
-- creation and cannot see a column added later. See the note atop 011.
