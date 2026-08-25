-- Archiving, so that removing something is not the same as destroying it.
--
-- A takeoff costs eighty seconds and real money to produce, and a person
-- clicking "delete" on the wrong row is not asking to spend that again. So the
-- delete endpoints archive by default and only destroy when told to twice.
--
-- `projects.status` already allowed 'archived' -- it was in the check
-- constraint from the start and nothing ever set it. Documents had no such
-- column, which is what this adds.
--
--     psql "$SUPABASE_DB_URL" -f db/005_archive.sql

alter table documents
    add column if not exists status text not null default 'active'
        check (status in ('active', 'archived'));

-- Listings ask for the active rows of one project, in upload order.
create index if not exists documents_by_project_status
    on documents (project_id, status, created_at desc);

-- ---------------------------------------------------------------------------
-- Counts must not include what has been archived.
-- ---------------------------------------------------------------------------
-- A job reading "24 doors" while showing none of them is worse than a job
-- reading "0": the number is the only thing a person checks against their own
-- count. Archived documents are excluded from every total, and from
-- `last_upload`, so an archived set stops influencing the summary entirely.
--
-- Spend is the exception. `run_log` records what was actually billed, and
-- archiving a set does not refund it -- a total that drops when somebody tidies
-- up is a cost report nobody can reconcile.

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
                         and doc.status = 'active'
left join doors       dr  on dr.document_id = doc.id
left join detections  det on det.document_id = doc.id
left join corrections c   on c.document_id = doc.id
left join run_log     l   on l.project_id = p.id
group by p.id, p.name, p.code, p.status;

-- The view is owned by the migration runner, so it must keep reading with the
-- caller's rights rather than its own -- see 004_view_security.sql.
alter view project_summary set (security_invoker = on);
grant select on project_summary to authenticated, service_role;
