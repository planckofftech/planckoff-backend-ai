-- Doors a person placed, and boxes a person removed.
--
--     psql "$SUPABASE_DB_URL" -f db/007_manual.sql
--
-- ---------------------------------------------------------------------------
-- Why these are their own tables
-- ---------------------------------------------------------------------------
-- `save_audit` begins by deleting every detection and every sheet for the
-- document, then writing what the drawing says now. That is correct for
-- derived data -- detections are read off the plan, so rebuilding them
-- wholesale is the only way they stay true after the detector improves.
--
-- It is exactly wrong for anything a person typed. Keeping both in one table
-- behind a `source <> 'manual'` filter would mean every future query had to
-- remember which rows are sacred, and that only has to be forgotten once to
-- destroy somebody's afternoon. So user data lives here, and the wipe cannot
-- reach it.
--
-- Same reasoning as `corrections`, and the same shape.
--
-- ---------------------------------------------------------------------------
-- Why these key on the page number, not sheet_id
-- ---------------------------------------------------------------------------
-- `save_audit` deletes the `sheets` rows too, and the rebuilt ones get new
-- uuids. A manual box referencing sheet_id would either be cascade-deleted with
-- its sheet, or left pointing at a row that no longer exists -- the very
-- failure these tables exist to prevent, reintroduced through the foreign key.
--
-- The page number is a property of the PDF and survives every re-read. The API
-- accepts a sheet_id from callers, because that is what they hold, and resolves
-- it to a page on the way in and back on the way out.

create table if not exists manual_detections (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organisations(id) on delete cascade,
    document_id uuid not null references documents(id) on delete cascade,
    page        int  not null,
    -- Null when the estimator boxed an opening that carries no number. That is
    -- the common case and the valuable one: a door drawn but never scheduled.
    door_tag    text,
    -- Page fractions, the same units the detector reports, so a viewer draws
    -- both kinds of box with one code path.
    x0 real not null, y0 real not null, x1 real not null, y1 real not null,
    kind        text,
    swing       text,
    note        text,
    created_by  uuid references auth.users(id),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists manual_by_document
    on manual_detections (document_id, page);

-- What a person removed, kept rather than deleted.
--
-- The geometry pass is deterministic: the same PDF yields the same arcs and the
-- same boxes every run. So a box somebody deleted as wrong -- a circle fitted
-- to a structural column, say -- comes back identical on the next audit, and
-- they delete it again. Recording the removal is what breaks that loop.
--
-- It is also one-way. If deletions are not written down and we later decide
-- they should survive a re-audit, there is nothing to reconstruct them from.
create table if not exists detection_tombstones (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organisations(id) on delete cascade,
    document_id uuid not null references documents(id) on delete cascade,
    page        int  not null,
    -- Where the box was. For a tagged door the number is enough to recognise
    -- the repeat; for an untagged one -- which is most of what gets deleted --
    -- position is the only handle there is.
    door_tag    text,
    x0 real not null, y0 real not null, x1 real not null, y1 real not null,
    reason      text,
    created_by  uuid references auth.users(id),
    created_at  timestamptz not null default now()
);

create index if not exists tombstones_by_document
    on detection_tombstones (document_id, page);

-- ---------------------------------------------------------------------------
-- Where a door row came from
-- ---------------------------------------------------------------------------
-- An estimator boxing an opening with no schedule row should create that row:
-- a door on the plan nobody has priced is the most useful thing a takeoff can
-- surface, and refusing it discards exactly what the user just found.
--
-- But it must be distinguishable, or the table shows a row with no width and no
-- material and no explanation, which reads as a bug.
--
--     'schedule'  read from a door schedule table
--     'plan'      exists because somebody boxed it on the drawing
--
-- 'plan' rather than 'manual': the door came from the drawing and is perfectly
-- real. What was manual is the act of recording it -- and that is on the
-- detection, which carries source 'manual' beside 'geometry' and 'model'.
alter table doors
    add column if not exists source text not null default 'schedule'
        check (source in ('schedule', 'plan'));

grant select, insert, update, delete on manual_detections to service_role;
grant select, insert, update, delete on detection_tombstones to service_role;
grant select on manual_detections, detection_tombstones to authenticated;
