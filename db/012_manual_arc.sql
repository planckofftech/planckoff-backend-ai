-- Let a hand-moved box keep the swing that was measured for it.
--
--     psql "$SUPABASE_DB_URL" -f db/012_manual_arc.sql
--
-- Moving a box converts it: the new position is stored as a hand-placed box
-- and the original is recorded as removed so the next audit cannot undo the
-- move. That was right, and it had a hole in it -- `manual_detections` had
-- nowhere to put an arc, so dragging a measured door threw the measurement
-- away. Hand of door and measured width went with it, permanently, because the
-- tombstone stops the audit ever finding it again.
--
-- Dragging a box usually means "right measurement, wrong place". The shape of
-- the swing is not in dispute; where we put it is. So the arc moves with the
-- box -- same radius, same angles, hinge shifted by however far the box went
-- -- and nothing is lost.
--
-- Where the measurement itself is wrong, the user deletes the box instead.
-- That already tombstones it, which is the correct outcome for a bad fit.

alter table manual_detections
    -- The swing, in PDF points, exactly as `detections` records it.
    add column if not exists hinge_x real,
    add column if not exists hinge_y real,
    add column if not exists radius real,
    add column if not exists start_deg real,
    add column if not exists end_deg real,
    -- True when this box began as one we measured and a person moved it. The
    -- arc is then a real measurement carried across, not a guess -- worth
    -- telling apart from a box drawn freehand, which has no arc at all.
    add column if not exists from_measured boolean not null default false;
