-- Editing a detected box no longer throws away what was measured about it.
--
--     psql "$SUPABASE_DB_URL" -f db/032_detection_edits_keep.sql
--
-- detections.kind_by_hand
--     true where a person set the door's type (kind / swing). A type change now
--     updates the detected box in place -- same id, source, tag position and
--     swing arc -- instead of turning it into a hand-placed box. The flag is
--     how the type survives a re-read: the next pass rebuilds the detections,
--     and carries a type a person chose onto the same door.
--
-- manual_detections.tag_x0 / tag_y0 / tag_x1 / tag_y1 / tag_shape
--     Where the door's number is printed, as on `detections`. A box moved by
--     hand says where the door is, not where its number is, so the tag
--     position is now carried across when a detected box is moved.
--
-- The backend writes without these until this has been run -- each is written
-- in a way that falls back when the column is missing.

alter table detections
    add column if not exists kind_by_hand boolean not null default false;

alter table manual_detections
    add column if not exists tag_x0 real,
    add column if not exists tag_y0 real,
    add column if not exists tag_x1 real,
    add column if not exists tag_y1 real,
    add column if not exists tag_shape text;
