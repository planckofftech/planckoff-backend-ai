-- Reading a drawing set as a job: the upload answers at once, the reading runs
-- after it, and the caller asks how it is going.
--
--     psql "$SUPABASE_DB_URL" -f db/019_reading_jobs.sql
--
-- ---------------------------------------------------------------------------
-- Why
-- ---------------------------------------------------------------------------
-- The frontend reaches this service through serverless functions that are cut
-- off at 300 seconds. Reading a schedule usually takes seconds, but a scanned
-- set that has to be read as images can take minutes, and a request cut off
-- mid-read stores nothing and tells the user nothing useful. No timeout is
-- long enough for every set, so the reading no longer happens inside the
-- request: the upload is answered with a job id, and the job's row says what
-- became of it.
--
-- A row lives in the database, not in the server's memory, so it is still
-- there when another instance answers the status call -- and a row left at
-- 'reading' by a server that restarted is reported as interrupted, not as
-- reading forever.
--
-- Only the backend reads and writes this table. RLS is on and no policy is
-- given to signed-in users, so the browser cannot see it directly; it asks
-- the API.

create table if not exists reading_jobs (
    id           uuid primary key default gen_random_uuid(),
    project_id   uuid not null references projects(id) on delete cascade,
    -- 'read' for a new upload, 'reread' for a stored set read again.
    kind         text not null default 'read',
    source_key   text,
    filename     text,
    -- reading | ready | no_schedule | failed
    status       text not null default 'reading',
    -- A plain message for the person waiting: what happened, never why.
    message      text,
    document_id  uuid references documents(id) on delete set null,
    doors        int,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists reading_jobs_by_project
    on reading_jobs (project_id, created_at desc);

alter table reading_jobs enable row level security;

grant all privileges on reading_jobs to service_role;
