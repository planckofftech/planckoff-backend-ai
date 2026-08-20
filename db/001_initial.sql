-- Door takeoff: schema. Paste into the Supabase SQL Editor and run once.
--
-- Sized for the free tier, which is the real constraint: 500 MB of database and
-- 1 GB of storage. One project's results are about 200 KB, so the database
-- holds a couple of thousand of them -- but one drawing set is 115 MB, so the
-- PDFs stay out of it entirely. A document here is a hash and some facts about
-- a file, never the file.
--
-- Four ideas run through the design:
--
--   a project owns its drawings   a document belongs to a job, and dedup is
--                                 scoped to that job. Two projects that happen
--                                 to hold the same bytes get their own rows,
--                                 so a file put in the wrong project is one
--                                 row to delete rather than shared state to
--                                 untangle.
--   a document is its content     identified by sha256 *within a project*,
--                                 because the same set is re-uploaded
--                                 endlessly under new names.
--   one row per door              re-reading a document replaces its rows. The
--                                 question "which of these is the door?" must
--                                 have exactly one answer.
--   a correction outlives the read  it belongs to the door number, not to the
--                                 row that happened to be on screen -- see
--                                 `corrections`.

create extension if not exists pgcrypto;

-- --------------------------------------------------------------------------
-- 1-2. who
-- --------------------------------------------------------------------------

create table organisations (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    created_at  timestamptz not null default now()
);

create table memberships (
    org_id      uuid not null references organisations(id) on delete cascade,
    user_id     uuid not null references auth.users(id) on delete cascade,
    role        text not null default 'member'
                check (role in ('owner', 'member', 'viewer')),
    created_at  timestamptz not null default now(),
    primary key (org_id, user_id)
);

-- Used by every policy below. `security definer` so the check itself is not
-- subject to the policies it exists to evaluate.
create or replace function is_member(target uuid)
returns boolean language sql security definer stable as $$
    select exists (
        select 1 from memberships
        where org_id = target and user_id = auth.uid()
    );
$$;

-- --------------------------------------------------------------------------
-- 3. the job
-- --------------------------------------------------------------------------

create table projects (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organisations(id) on delete cascade,
    name        text not null,          -- "BMK Pharma", "UNIQLO Barton Creek"
    code        text,                   -- the firm's own job number
    address     text,
    status      text not null default 'active'
                check (status in ('active', 'archived')),
    created_by  uuid references auth.users(id),
    created_at  timestamptz not null default now()
);

create index projects_by_org on projects (org_id, status);

-- --------------------------------------------------------------------------
-- 4. the drawing set
-- --------------------------------------------------------------------------

create table documents (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organisations(id) on delete cascade,
    project_id  uuid not null references projects(id) on delete cascade,
    -- The file's own identity, so the same bytes are not read twice: a set
    -- arrives as "Binder.pdf", then "Binder (1).pdf", then "IFC FINAL.pdf".
    -- Scoped to the project on purpose -- see the note at the top.
    sha256      char(64) not null,
    filename    text not null,
    -- Which issue this is: "IFC", "Permit Set", "Addendum 1". A revised set is
    -- different bytes and should be read again; the hash cannot tell you that
    -- one supersedes the other, so a person says it here.
    revision    text,
    size_bytes  bigint not null,
    page_count  int,
    -- Where the file actually lives: a path, an S3 key, a SharePoint URL.
    -- Not the bytes.
    source_uri  text,
    created_by  uuid references auth.users(id),
    created_at  timestamptz not null default now(),
    unique (project_id, sha256)
);

create index documents_by_project on documents (project_id, created_at desc);

-- --------------------------------------------------------------------------
-- 5-6. the schedule
-- --------------------------------------------------------------------------

create table schedules (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents(id) on delete cascade,
    page        int not null,
    title       text,                   -- "DOOR SCHEDULE CONTINUE."
    -- The sheet's own column names, and what each was mapped to. Kept together
    -- so a mis-mapping is visible rather than silent.
    headers     jsonb not null default '[]',
    field_map   jsonb not null default '[]',
    row_count   int not null default 0
);

create table doors (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references organisations(id) on delete cascade,
    document_id    uuid not null references documents(id) on delete cascade,
    schedule_id    uuid references schedules(id) on delete set null,
    row_index      int not null,        -- position as printed
    door_tag       text,
    from_space     text,
    to_space       text,
    -- Sizes are text, not numbers, on purpose. A schedule prints 3' - 0",
    -- (2)3'-0", 3'-0"/1'-6" and (PR)2'-10", and the string is what the drawing
    -- says. Turning it into inches is a view's job, not the record's.
    door_width     text,
    door_height    text,
    door_type      text,
    door_material  text,
    door_finish    text,
    frame_material text,
    frame_finish   text,
    threshold      text,
    fire_rating    text,
    hw_set         text,
    comments       text,
    -- Columns this sheet has that no canonical field fits. Never dropped: one
    -- firm's "ACOUSTIC RATING" is another's whole reason for the schedule.
    extra          jsonb not null default '{}',
    -- One row per door, enforced. A door number is unique within a job, so a
    -- second one means something went wrong upstream -- and this is where it
    -- gets caught: one set's damaged font read doors 106 and 108 as "10" and
    -- "10", which looked like data until something refused it.
    unique (document_id, door_tag)
);

create index doors_by_document on doors (document_id, row_index);
create index doors_by_tag on doors (org_id, door_tag);

-- --------------------------------------------------------------------------
-- 7-8. the drawings
-- --------------------------------------------------------------------------

create table sheets (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents(id) on delete cascade,
    page        int not null,
    number      text,                   -- "A-111"
    title       text,                   -- "FLOOR PLANS - LEVEL 1"
    -- Which storey, normalised: LEVEL 1, LEVEL B1, MEZZANINE, PHASE II.
    level       text,
    -- Is this the sheet to open for that storey: the whole floor at working
    -- scale, rather than the index that redraws every floor small.
    leads       boolean not null default false,
    width_pt    real,
    height_pt   real,
    unique (document_id, page)
);

create table detections (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references organisations(id) on delete cascade,
    document_id    uuid not null references documents(id) on delete cascade,
    sheet_id       uuid not null references sheets(id) on delete cascade,
    door_tag       text,
    -- Page fractions, so a viewer can draw the box at whatever size it renders.
    x0 real, y0 real, x1 real, y1 real,
    kind           text,                -- single_swing, double_swing, sliding
    swing          text,
    -- 'geometry' when the box is the measured extent of the door's own arc,
    -- 'model' when a vision model estimated it.
    source         text,
    confidence     text,
    measured_width text,
    -- The same door is drawn on the overall plan, the partial and the
    -- enlargement. This marks the one to price from; the others stay so the
    -- viewer can draw the door on whichever sheet is open.
    is_primary     boolean not null default true,
    sheet_scale    real,                -- points per leaf: the drawing's scale
    also_on        text[] not null default '{}',
    -- The swing itself, in PDF points. Numbers rather than an image: it is
    -- small, and any picture can be redrawn from it.
    hinge_x    real, hinge_y real,
    radius     real,
    start_deg  real, end_deg real,
    residual   real,
    other_leaf jsonb,                   -- a pair's second leaf, same shape
    -- One row per door per sheet. Not a duplicate: door 104 really is drawn on
    -- A3.10 and again on A3.13, and both are needed.
    unique (document_id, door_tag, sheet_id)
);

create index detections_by_document on detections (document_id);
create index detections_primary on detections (document_id) where is_primary;

-- --------------------------------------------------------------------------
-- 9. what a person changed
-- --------------------------------------------------------------------------

-- Keyed on the document and the door number, NOT on a row in `doors`.
--
-- This is the one piece of the design that is not obvious, and getting it wrong
-- is expensive. A document is re-read every time the extractor improves, which
-- replaces its `doors` rows -- so a correction tied to a row would be destroyed
-- by the very improvement it was compensating for, and an estimator would watch
-- their fixes disappear. Tied to the door number, it survives every re-read.
--
-- The machine's value is never overwritten. `was` and `now` both stay, because
-- the question asked later is not what the number is, but who changed it.
create table corrections (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organisations(id) on delete cascade,
    document_id uuid not null references documents(id) on delete cascade,
    door_tag    text not null,
    field       text not null,          -- 'door_width', 'door_type', ...
    was         text,
    "now"       text,
    note        text,
    created_by  uuid references auth.users(id),
    created_at  timestamptz not null default now()
);

create index corrections_current on corrections
    (document_id, door_tag, field, created_at desc);

-- --------------------------------------------------------------------------
-- 10. what each read cost and found
-- --------------------------------------------------------------------------

-- Summary only, and nothing joins to it. That is the point: it keeps the
-- history that makes "did last night's change help?" answerable, without
-- putting a second copy of every door in the database. Append-only.
create table run_log (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references organisations(id) on delete cascade,
    project_id      uuid references projects(id) on delete cascade,
    document_id     uuid references documents(id) on delete set null,
    kind            text not null check (kind in ('extract', 'audit')),
    status          text not null default 'ok'
                    check (status in ('ok', 'partial', 'failed')),
    method          text,               -- deterministic_ruled | ai_vision | ...
    -- The build that produced it. Without this a comparison between two reads
    -- says nothing, because you cannot tell what was different.
    app_version     text,
    pages_scanned   int,
    duration_ms     int,
    model           text,
    cost_usd        numeric(10, 4) default 0,
    tiles_sent      int default 0,
    doors_scheduled int,
    doors_located   int,
    swings_measured int,
    warnings        jsonb not null default '[]',
    created_by      uuid references auth.users(id),
    created_at      timestamptz not null default now()
);

create index run_log_by_document on run_log (document_id, created_at desc);

-- --------------------------------------------------------------------------
-- access
-- --------------------------------------------------------------------------

alter table organisations enable row level security;
alter table memberships   enable row level security;
alter table projects      enable row level security;
alter table documents     enable row level security;
alter table schedules     enable row level security;
alter table doors         enable row level security;
alter table sheets        enable row level security;
alter table detections    enable row level security;
alter table corrections   enable row level security;
alter table run_log       enable row level security;

create policy org_read       on organisations for select using (is_member(id));
create policy own_membership on memberships   for select using (user_id = auth.uid());

create policy projects_rw    on projects    for all using (is_member(org_id));
create policy documents_rw   on documents   for all using (is_member(org_id));
create policy doors_rw       on doors       for all using (is_member(org_id));
create policy detections_rw  on detections  for all using (is_member(org_id));
create policy corrections_rw on corrections for all using (is_member(org_id));
create policy run_log_rw     on run_log     for all using (is_member(org_id));

-- These two hang off a document rather than carrying an org of their own; the
-- join is cheap and one less column can go stale.
create policy schedules_rw on schedules for all using (exists (
    select 1 from documents d where d.id = document_id and is_member(d.org_id)));
create policy sheets_rw on sheets for all using (exists (
    select 1 from documents d where d.id = document_id and is_member(d.org_id)));
