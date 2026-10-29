-- Every page of a set, not only its door plans.
--
--     psql "$SUPABASE_DB_URL" -f db/021_document_pages.sql
--
-- `sheets` holds the pages taken for door plans. A person choosing pages to be
-- read again needs all of them -- above all the scanned plans the pass could
-- not read, which are never in `sheets` -- and had only `page_count` to go on.
-- Written by every plan pass; the names are the ones the pass already worked
-- out, so keeping them costs nothing.
--
--     unreadable  the drawing is almost bare of readable text: a scan, or
--                 lettering drawn as lines. Only the vision model can find
--                 doors on it.

create table if not exists document_pages (
    document_id uuid not null references documents(id) on delete cascade,
    page        int not null,
    number      text,
    title       text,
    unreadable  boolean not null default false,
    primary key (document_id, page)
);

alter table document_pages enable row level security;

drop policy if exists document_pages_rw on document_pages;
create policy document_pages_rw on document_pages for all using (exists (
    select 1 from documents d where d.id = document_id and is_member(d.org_id)));
