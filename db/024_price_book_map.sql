-- Each price book's own table of contents, for finding the page a product is on.
--
--     psql "$SUPABASE_DB_URL" -f db/024_price_book_map.sql
--
-- Made by code from the page text (app/price_book/book_map.py), once per book:
--
--   price_book_pages   one per PDF page: the heading printed on it, how many
--                      prices it prints, and the models priced there. A page
--                      with no text is a picture (`has_text` false).
--   price_book_index   the part-number index some books print ("Part Number |
--                      Page"), with the PDF page it points to where that is
--                      known for certain.
--
-- Keyed by file name rather than by price_books.id: reading a book's prices
-- again replaces its price_books row, and the map of an unchanged PDF should
-- not go with it. `sha256` says which copy of the file the map was made from.

create table if not exists price_book_pages (
    file_name     text not null,
    sha256        text not null,
    pdf_page      int not null,
    printed_page  text not null default '',
    heading       text not null default '',
    prices        int not null,
    models        text[] not null default '{}',
    has_text      boolean not null,
    made_at       timestamptz not null default now(),
    primary key (file_name, pdf_page)
);

create table if not exists price_book_index (
    id            bigint generated always as identity primary key,
    file_name     text not null,
    model         text not null,
    match_key     text not null,
    printed_page  text not null,
    pdf_page      int,
    index_page    int not null
);

create index if not exists price_book_index_lookup on price_book_index (file_name, match_key);

alter table price_book_pages enable row level security;
alter table price_book_index enable row level security;

drop policy if exists price_book_pages_read on price_book_pages;
create policy price_book_pages_read on price_book_pages for select using (auth.uid() is not null);
drop policy if exists price_book_index_read on price_book_index;
create policy price_book_index_read on price_book_index for select using (auth.uid() is not null);
