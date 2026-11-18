-- Two columns door schedules print that had nowhere to go.
--
--     psql "$SUPABASE_DB_URL" -f db/033_door_thickness_frame_type.sql
--
--   door_thickness   1 3/4", 1 3/8" -- DOOR THICKNESS, THK, SIZE THK. Read on
--                    every set that prints it and kept only in `extra`, under
--                    a different key on each, so nothing could show it and no
--                    one could enter it where the schedule left it out (ESC).
--   frame_type       the frame's elevation or type -- A, F1, HM-1, W02. AT&T's
--                    FRAME ELEVATION, ESC's FRAME DESIGN TYPE, The Cannon's
--                    RAIL/FRAME TYPE. Testers reported frame elevations "given
--                    in the schedule but not detected"; they were in `extra`.
--
-- Both correctable like every other door field (POST .../corrections).
--
-- The backend stores without them until this has been run: their values go
-- into `extra` instead, as before, and the save does not fail.
--
-- doors_current is rebuilt because it selects d.* -- a view expands that when
-- it is created, so new columns stay invisible through it until it is made
-- again (see 011). Same definition as 013.

alter table doors
    add column if not exists door_thickness text,
    add column if not exists frame_type text;

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
