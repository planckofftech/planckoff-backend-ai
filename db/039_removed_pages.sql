-- Plan pages a person removed from a drawing set.
--
--     psql "$SUPABASE_DB_URL" -f db/039_removed_pages.sql
--
-- RUN THIS BEFORE DEPLOYING THE CODE THAT GOES WITH IT. Until it has been
-- run, removing a page answers 503 and nothing is touched.
--
-- removed_pages
--     One row per page removed. Written by DELETE /documents/{id}/pages/{page};
--     taken off by DELETE /documents/{id}/removed-pages[/{page}], which puts
--     the page back.
--
--     Removing a page takes its boxes off -- the ones the plan reads placed
--     and the ones a person placed -- and nothing else: no door and no
--     schedule row changes. A door whose only box was there is left unplaced.
--     The boxes are kept here whole, so putting the page back puts them back.
--
--     `sheet` is the page's row in `sheets` as it stood, since a read of the
--     whole set rebuilds `sheets` and leaves a removed page out.
--     `detections` and `manual` are its boxes, each with its `page`.
--     `promoted` are the boxes elsewhere made the counted one for a door
--     whose counted box was on this page: [{id, door_tag}]. Putting the page
--     back makes its own box the counted one again, where they still are.
--
-- While a page is listed here, plan reads and area reads do not look at it.

create table if not exists removed_pages (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents(id) on delete cascade,
    page        int not null,
    sheet       jsonb,
    detections  jsonb not null default '[]',
    manual      jsonb not null default '[]',
    promoted    jsonb not null default '[]',
    created_by  text,
    created_at  timestamptz not null default now(),
    unique (document_id, page)
);

-- Only the backend reads and writes it; RLS is on with no policy for
-- signed-in users, as for area_reads.
alter table removed_pages enable row level security;

grant all privileges on removed_pages to service_role;
