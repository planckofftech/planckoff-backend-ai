-- Which part of the building a door belongs to.
--
--     psql "$SUPABASE_DB_URL" -f db/013_door_level.sql
--
-- RUN THIS BEFORE DEPLOYING THE CODE THAT GOES WITH IT. Every door row is
-- written with every field it has, so a `level` with no column to land in
-- fails the insert -- and `save_extraction` swallows that, returns nothing,
-- and the caller gets "Read the schedule but could not store it" after half a
-- minute of work with no reason given. That is exactly how a blank door number
-- took two days to find.
--
-- ---------------------------------------------------------------------------
-- Why a door needs one
-- ---------------------------------------------------------------------------
-- A tower numbers its doors per storey, so 001 on level 1 and 001 outside are
-- two doors. `doors` is unique on (document_id, door_tag), so the second was
-- dropped and the takeoff was short by however many the building repeated.
-- Measured on one residential set: five numbers repeated, and the level tells
-- all five apart -- 001 is LEVEL 1 and EXTERIOR, S1-0 and S2-0 are GARAGE and
-- LEVEL 1.
--
-- Stored as the drawing's own words rather than a tidied code. A set divides
-- its doors by whatever it likes -- "LEVEL 08", "P1 LEVEL", "GARAGE", "UNITS",
-- "LEVEL 09 CROSS-OVER FLOOR" -- and only two of those are storeys. Inventing
-- a scheme would mean deciding which of them count, and the drawing has
-- already decided.
--
-- Empty on most jobs, which is not a failure: a single-floor fit-out divides
-- nothing, and every door lands in one group as it should.

alter table doors
    add column if not exists level text not null default '';

-- One number may now appear once per level rather than once per document.
-- Dropped and rebuilt rather than altered, because the old constraint is what
-- was throwing the takeoff away.
alter table doors
    drop constraint if exists doors_document_id_door_tag_key;
alter table doors
    add constraint doors_document_id_door_tag_level_key
    unique (document_id, door_tag, level);

-- `doors_current` selects `d.*`, which Postgres expanded when the view was
-- created -- so `level` is invisible to it until it is rebuilt. See the note
-- at the top of 011, which is the same trap for the same reason.
drop view if exists doors_current;

create view doors_current as
with newest as (
    select distinct on (document_id, door_tag, field)
           document_id, door_tag, field, "now", created_at
    from corrections
    order by document_id, door_tag, field, created_at desc
), applied as (
    select document_id, door_tag,
           jsonb_object_agg(field, "now")  as fixes,
           max(created_at)                 as edited_at
    from newest
    where "now" is not null
    group by document_id, door_tag
)
select
    d.*,
    coalesce(a.fixes->>'door_width',     d.door_width)    as width,
    coalesce(a.fixes->>'door_height',    d.door_height)   as height,
    coalesce(a.fixes->>'door_type',      d.door_type)     as type,
    coalesce(a.fixes->>'door_material',  d.door_material) as material,
    (a.door_tag is not null)                              as edited,
    a.edited_at                                           as edited_at,
    coalesce(a.fixes, '{}'::jsonb)                        as corrected
from doors d
left join applied a on a.document_id = d.document_id
                   and a.door_tag = d.door_tag;

alter view doors_current set (security_invoker = on);
grant select on doors_current to authenticated, service_role;
