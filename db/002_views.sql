-- Views: the three questions this data exists to answer.
--
-- A view stores nothing. It is a saved query that behaves like a table, so the
-- definition of "the current schedule" lives in one place instead of being
-- written out by every caller that needs it -- which is how two callers come to
-- disagree about what current means.

-- 1. What does the schedule say, after the people who know better have had
--    their say?
--
-- Both values stay: `door_width` is what was read off the drawing, `width` is
-- what to price, and `edited` says a person intervened. Where they differ that
-- is a fact worth seeing, not a discrepancy to hide.
create or replace view doors_current as
with newest as (
    select distinct on (document_id, door_tag, field)
           document_id, door_tag, field, "now", created_by, created_at
    from corrections
    order by document_id, door_tag, field, created_at desc
)
select
    d.*,
    coalesce(w."now", d.door_width)    as width,
    coalesce(h."now", d.door_height)   as height,
    coalesce(t."now", d.door_type)     as type,
    coalesce(m."now", d.door_material) as material,
    (w.door_tag is not null or h.door_tag is not null
     or t.door_tag is not null or m.door_tag is not null) as edited,
    greatest(w.created_at, h.created_at, t.created_at, m.created_at)
                                       as edited_at
from doors d
left join newest w on w.document_id = d.document_id
                  and w.door_tag = d.door_tag and w.field = 'door_width'
left join newest h on h.document_id = d.document_id
                  and h.door_tag = d.door_tag and h.field = 'door_height'
left join newest t on t.document_id = d.document_id
                  and t.door_tag = d.door_tag and t.field = 'door_type'
left join newest m on m.document_id = d.document_id
                  and m.door_tag = d.door_tag and m.field = 'door_material';

-- 2. Did the change I shipped last night help or hurt?
--
-- Straight off the log, so it costs nothing and cannot disagree with the doors
-- table. Read it grouped by filename to see one project's history, or by
-- app_version to compare two builds across every project at once.
create or replace view run_scorecard as
select
    l.created_at,
    p.name        as project,
    d.filename,
    d.revision,
    l.kind,
    l.status,
    l.method,
    l.app_version,
    l.doors_scheduled,
    l.doors_located,
    l.swings_measured,
    case when coalesce(l.doors_scheduled, 0) > 0
         then round(100.0 * l.doors_located / l.doors_scheduled)
    end as located_pct,
    l.duration_ms,
    l.cost_usd,
    jsonb_array_length(l.warnings) as warnings
from run_log l
left join documents d on d.id = l.document_id
left join projects  p on p.id = l.project_id
order by l.created_at desc;

-- 3. What is in this project, and what did it cost?
--
-- One row per project: how many drawing sets, how many doors, how much of the
-- work is actually measured rather than assumed, and the spend to date.
create or replace view project_summary as
select
    p.id,
    p.name,
    p.code,
    p.status,
    count(distinct doc.id)                                   as documents,
    count(distinct dr.id)                                    as doors,
    count(distinct det.id) filter (where det.is_primary
                                     and det.radius is not null)
                                                             as swings_measured,
    count(distinct c.door_tag)                               as doors_corrected,
    coalesce(sum(l.cost_usd), 0)                             as spent_usd,
    max(doc.created_at)                                      as last_upload
from projects p
left join documents   doc on doc.project_id = p.id
left join doors       dr  on dr.document_id = doc.id
left join detections  det on det.document_id = doc.id
left join corrections c   on c.document_id = doc.id
left join run_log     l   on l.project_id = p.id
group by p.id, p.name, p.code, p.status;
