-- A door schedule table a person drew around, read on its own.
--
--     psql "$SUPABASE_DB_URL" -f db/038_schedule_area_reads.sql
--
-- RUN THIS BEFORE DEPLOYING THE CODE THAT GOES WITH IT.
--
-- The schedule read finds its tables by itself and sometimes finds the wrong
-- one, half of one, or none -- a scan, a table drawn as linework, a schedule
-- split in two. A person draws a box around the table and only the box is
-- read: its text first, the image reader where the text is missing or
-- partial. See app/core/read_schedule_area.py.
--
-- schedule_area_reads
--     One per request. `drawn` is what the person outlined: [{page, box}].
--     `tables` is what was read, rows included, so the read can be applied
--     again after the whole schedule is re-read (which replaces every door).
--     `replaced` is, per door it changed, the values it replaced -- for undo.
--     `method` is text or image. status: queued | reading | ready | failed |
--     undone.
--
-- doors.schedule_area_id, schedules.schedule_area_id
--     The read a door or a table came from. Undo removes exactly these.
--
-- Only the backend reads and writes schedule_area_reads; RLS is on with no
-- policy for signed-in users, as for area_reads.

create table if not exists schedule_area_reads (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references documents(id) on delete cascade,
    job_id       uuid references reading_jobs(id) on delete set null,
    drawn        jsonb not null default '[]',
    status       text not null default 'queued',
    -- A plain message for the person: what happened, never why.
    message      text,
    method       text,
    added        int,
    updated      int,
    tables       jsonb not null default '[]',
    replaced     jsonb not null default '[]',
    created_by   text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    undone_at    timestamptz
);

create index if not exists schedule_area_reads_by_document
    on schedule_area_reads (document_id, created_at desc);

alter table schedule_area_reads enable row level security;

grant all privileges on schedule_area_reads to service_role;

alter table doors
    add column if not exists schedule_area_id uuid
        references schedule_area_reads(id) on delete set null;

create index if not exists doors_by_schedule_area
    on doors (schedule_area_id) where schedule_area_id is not null;

alter table schedules
    add column if not exists schedule_area_id uuid
        references schedule_area_reads(id) on delete set null;
