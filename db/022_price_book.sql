-- Hardware list prices, read from the manufacturers' price books.
--
--     psql "$SUPABASE_DB_URL" -f db/022_price_book.sql
--
-- A price is a row read off a page, and keeps the page it was read from so
-- anyone can open the book and check it. Nothing here is estimated; an item
-- with no row is reported as having no price.
--
--   price_books       one per PDF read. Reading a book again replaces it and
--                     its rows, so a price has one answer.
--   price_rows        one per printed price. `match_key` is the model with
--                     only letters and digits kept, upper-cased: what a
--                     schedule's "9948 EO-F" is matched on.
--   price_book_gaps   pages that print prices no reader took yet. The coverage
--                     of a book, stated rather than assumed.
--   price_multipliers what a company actually pays against list, per maker.
--                     Empty until a company enters one; list price stands.
--
-- List prices are the manufacturers' published figures and the same for every
-- company, so any signed-in user may read them. Only the backend writes them.
-- A multiplier is a company's own terms and is kept to its members.

create table if not exists price_books (
    id              uuid primary key default gen_random_uuid(),
    manufacturer    text not null,
    file_name       text not null unique,
    sha256          text not null,
    reader          text not null,
    effective_date  date,
    page_count      int not null,
    rows_read       int not null,
    pages_unread    int not null,
    prices_unread   int not null,
    loaded_at       timestamptz not null default now()
);

create table if not exists price_rows (
    id            bigint generated always as identity primary key,
    book_id       uuid not null references price_books(id) on delete cascade,
    manufacturer  text not null,
    model         text not null,
    match_key     text not null,
    kind          text not null check (kind in ('base', 'size_add', 'option_add')),
    finish        text not null default '',
    size          text not null default '',
    list_price    numeric(12, 2) not null check (list_price >= 0),
    price_text    text not null,
    pdf_page      int not null,
    printed_page  text not null default '',
    description   text not null default '',
    note          text not null default ''
);

create index if not exists price_rows_lookup on price_rows (manufacturer, match_key);

create table if not exists price_book_gaps (
    book_id         uuid not null references price_books(id) on delete cascade,
    pdf_page        int not null,
    prices_on_page  int not null,
    reason          text not null,
    primary key (book_id, pdf_page)
);

create table if not exists price_multipliers (
    org_id        uuid not null references organisations(id) on delete cascade,
    manufacturer  text not null,
    multiplier    numeric(6, 4) not null check (multiplier > 0),
    note          text not null default '',
    updated_at    timestamptz not null default now(),
    primary key (org_id, manufacturer)
);

alter table price_books       enable row level security;
alter table price_rows        enable row level security;
alter table price_book_gaps   enable row level security;
alter table price_multipliers enable row level security;

drop policy if exists price_books_read on price_books;
create policy price_books_read on price_books for select using (auth.uid() is not null);
drop policy if exists price_rows_read on price_rows;
create policy price_rows_read on price_rows for select using (auth.uid() is not null);
drop policy if exists price_book_gaps_read on price_book_gaps;
create policy price_book_gaps_read on price_book_gaps for select using (auth.uid() is not null);

drop policy if exists price_multipliers_rw on price_multipliers;
create policy price_multipliers_rw on price_multipliers for all using (is_member(org_id));
