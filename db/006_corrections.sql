-- Every door-side field becomes correctable, not just four.
--
-- The old view coalesced `door_width`, `door_height`, `door_type` and
-- `door_material` and nothing else. A correction to any other field was
-- accepted, written down, and then never shown -- so `fire_rating` was a dead
-- input in the UI while being the field that decides which door gets bought.
--
-- Measured across six real sets, the columns the schedule simply does not
-- carry are the common case, not the exception:
--
--     fire_rating     absent from three of six
--     door_finish     absent from one, two-thirds missing on two more
--     frame_finish    absent from three
--
-- The four coalesced fields covered extraction being wrong. They did not cover
-- the drawing never carrying the column, which is what an estimator actually
-- hits.
--
--     psql "$SUPABASE_DB_URL" -f db/006_corrections.sql
--
-- ---------------------------------------------------------------------------
-- Compatibility
-- ---------------------------------------------------------------------------
-- Additive only. Everything the previous view returned is returned unchanged,
-- with the same name and the same type:
--
--     d.*                        the extracted values, still extracted
--     width, height,
--     type, material             the four existing corrected aliases
--     edited, edited_at
--
-- and one column is added:
--
--     corrected  jsonb           field -> value, only fields actually changed
--
-- A caller renders `corrected->>'fire_rating'` falling back to `fire_rating`,
-- and the four legacy aliases keep working untouched.
--
-- One behaviour does change, in the intended direction: `edited` and
-- `edited_at` now account for every correctable field, so a door corrected
-- only on `hw_set` finally reports as edited. It reported false before.

create or replace view doors_current as
with newest as (
    -- The current value of each field: last correction wins.
    select distinct on (document_id, door_tag, field)
           document_id, door_tag, field, "now", created_at
    from corrections
    order by document_id, door_tag, field, created_at desc
), applied as (
    -- One row per door instead of one per field, so the view joins once
    -- rather than once per correctable column. Adding a field below is then a
    -- single line here, not another left join.
    select document_id, door_tag,
           jsonb_object_agg(field, "now")  as fixes,
           max(created_at)                 as edited_at
    from newest
    where "now" is not null
    group by document_id, door_tag
)
-- Column ORDER matters here, and is not a style choice. `create or replace
-- view` may only append columns: it cannot rename, retype or reorder an
-- existing one. So the previous view's columns come first, in their original
-- order, and `corrected` goes last. Slipping it in after `d.*` -- where it
-- reads better -- fails with "cannot change name of view column width".
select
    d.*,
    -- The original four, unchanged, because the frontend already reads them.
    coalesce(a.fixes->>'door_width',     d.door_width)    as width,
    coalesce(a.fixes->>'door_height',    d.door_height)   as height,
    coalesce(a.fixes->>'door_type',      d.door_type)     as type,
    coalesce(a.fixes->>'door_material',  d.door_material) as material,
    (a.door_tag is not null)                              as edited,
    a.edited_at                                           as edited_at,
    -- New, and last. Every correction that applies to this door: a caller
    -- reads `corrected->>'<field>'` and falls back to the column beside it.
    coalesce(a.fixes, '{}'::jsonb)                        as corrected
from doors d
left join applied a on a.document_id = d.document_id
                   and a.door_tag = d.door_tag;

-- Reads with the caller's rights, not the migration runner's -- see
-- 004_view_security.sql.
alter view doors_current set (security_invoker = on);
grant select on doors_current to authenticated, service_role;
