-- A part of a plan a person drew around, read for its doors by the vision
-- model.
--
--     psql "$SUPABASE_DB_URL" -f db/037_area_reads.sql
--
-- A person drags a box over a floor plan -- a corridor the pass missed, a
-- door it boxed on a room tag -- and the doors inside are read again with the
-- image reader. Whatever is drawn is answered: the backend widens a box too
-- small to hold a door, reads the whole sheet for a box over most of it, and
-- reads overlapping boxes once. See app/core/read_area.py.
--
-- area_reads
--     One per request. `drawn` is what the person outlined, `plan` what was
--     actually read -- [{page, read, drawn, whole}] -- so the viewer can show
--     the first and anyone asking can see the second.
--     `replaced` is every detected box this read took away, kept whole so
--     undo can put them back.
--     status: queued | reading | ready | failed | undone
--
-- detections.area_id
--     The area read a box came from. Such a box is a person's request, paid
--     for, and a later read of the whole set keeps it rather than wiping it.
--     Undo removes exactly these.
--
-- Only the backend reads and writes area_reads; RLS is on with no policy for
-- signed-in users, as for reading_jobs.

create table if not exists area_reads (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references documents(id) on delete cascade,
    job_id       uuid references reading_jobs(id) on delete set null,
    drawn        jsonb not null default '[]',
    plan         jsonb not null default '[]',
    status       text not null default 'queued',
    -- A plain message for the person: what happened, never why.
    message      text,
    found        int,
    replaced     jsonb not null default '[]',
    created_by   text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    undone_at    timestamptz
);

create index if not exists area_reads_by_document
    on area_reads (document_id, created_at desc);

alter table area_reads enable row level security;

grant all privileges on area_reads to service_role;

alter table detections
    add column if not exists area_id uuid references area_reads(id) on delete set null;

create index if not exists detections_by_area
    on detections (area_id) where area_id is not null;
