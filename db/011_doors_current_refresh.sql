-- Rebuild `doors_current` so it can see the wall-type columns.
--
--     psql "$SUPABASE_DB_URL" -f db/011_doors_current_refresh.sql
--
-- ---------------------------------------------------------------------------
-- The trap, worth knowing before adding another column to `doors`
-- ---------------------------------------------------------------------------
-- `doors_current` selects `d.*`. Postgres expands that star **when the view is
-- created** and stores the resulting column list. A column added to `doors`
-- afterwards is therefore invisible to the view, and to every caller reading
-- through it.
--
-- That is what happened with 010: `wall_type`, `wall_type_options` and
-- `wall_type_source` were added to the table, the API kept reading the view,
-- and the fields came back not as null but *absent* -- so the frontend had a
-- column it could never populate and no error to explain why.
--
-- `create or replace view` cannot repair it: the new columns expand inside
-- `d.*`, ahead of the aliases below, and replacing a view may only append
-- columns, never reorder them. So the view is dropped and rebuilt.
--
-- Any future column on `doors` needs this file run again. The alternative --
-- naming every column explicitly -- trades this trap for a longer one, where a
-- new column is silently missing until somebody notices.

drop view if exists doors_current;

create view doors_current as
with newest as (
    -- The current value of each field: last correction wins.
    select distinct on (document_id, door_tag, field)
           document_id, door_tag, field, "now", created_at
    from corrections
    order by document_id, door_tag, field, created_at desc
), applied as (
    -- One row per door rather than one per field, so the view joins once
    -- instead of once per correctable column.
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
    -- Every correction that applies to this door. A caller reads
    -- `corrected->>'<field>'` and falls back to the column beside it.
    coalesce(a.fixes, '{}'::jsonb)                        as corrected
from doors d
left join applied a on a.document_id = d.document_id
                   and a.door_tag = d.door_tag;

-- Reads with the caller's rights, not the migration runner's.
alter view doors_current set (security_invoker = on);
grant select on doors_current to authenticated, service_role;
