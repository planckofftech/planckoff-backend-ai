-- Make the project list stop multiplying its tables together.
--
--     psql "$SUPABASE_DB_URL" -f db/015_project_summary_fast.sql
--
-- ---------------------------------------------------------------------------
-- What was wrong
-- ---------------------------------------------------------------------------
-- `project_summary` joined documents, doors, detections, corrections and
-- run_log in one statement and then undid the damage with `count(distinct ...)`
-- on each. Those tables are unrelated to each other -- a door is not a row of
-- run_log -- so the join is their product: a document with 105 doors and 204
-- detections, in a project with 141 runs, is three million rows on its own.
--
-- Measured, listing nine projects with 623 doors between them:
--
--     Test_project_Over      ~1,673,847 join rows
--     test project dev         ~594,781
--     Test Project1 Prod       ~266,112
--     ---------------------------------
--     total                  ~2,635,830   to return nine rows
--
-- It survived because the counts were small. At around two and a half million
-- it passed Postgres's statement timeout, and `GET /api/v1/projects` began
-- returning 500 after nine seconds -- which took every screen down with it,
-- since the project list is how a job is resolved. A `limit 1` still answered
-- instantly, because one group can be built and returned early; `limit 5` did
-- not.
--
-- Counting each thing separately makes the work proportional to the rows that
-- exist rather than to their product. Same columns, same order, same values.
--
-- Nothing here depends on the data being small, so this does not need doing
-- again when the next set lands.

drop view if exists project_summary;

create view project_summary as
with per_document as (
    -- One row per document, each count answered from that document's own rows.
    select
        doc.id,
        doc.project_id,
        doc.created_at,
        (select count(*) from doors dr
          where dr.document_id = doc.id)                as doors,
        (select count(*) from detections det
          where det.document_id = doc.id
            and det.is_primary
            and det.radius is not null)                 as swings_measured
    from documents doc
), rolled as (
    select
        project_id,
        count(*)            as documents,
        sum(doors)          as doors,
        sum(swings_measured) as swings_measured,
        max(created_at)     as last_upload
    from per_document
    group by project_id
), corrected as (
    -- Distinct across the project, not per document, exactly as before: the
    -- same door corrected in two documents is one corrected door.
    select doc.project_id, count(distinct c.door_tag) as doors_corrected
    from corrections c
    join documents doc on doc.id = c.document_id
    group by doc.project_id
), spend as (
    select project_id, sum(cost_usd) as spent_usd
    from run_log
    group by project_id
)
select
    p.id,
    p.name,
    p.code,
    p.status,
    coalesce(r.documents, 0)        as documents,
    coalesce(r.doors, 0)            as doors,
    coalesce(r.swings_measured, 0)  as swings_measured,
    coalesce(x.doors_corrected, 0)  as doors_corrected,
    coalesce(s.spent_usd, 0)        as spent_usd,
    r.last_upload
from projects p
left join rolled    r on r.project_id = p.id
left join corrected x on x.project_id = p.id
left join spend     s on s.project_id = p.id;

-- Reads with the caller's rights, not the migration runner's.
alter view project_summary set (security_invoker = on);
grant select on project_summary to authenticated, service_role;
