-- Each price book's guide, written once by the Book Guide agent for the pricing agent.
--
--     psql "$SUPABASE_DB_URL" -f db/028_price_book_guides.sql
--
-- A guide says, for each product family a book prices, which pages it is on,
-- the order the pieces of an order code come in ("function, then cylinder,
-- then lever, then trim, then finish"), where the base prices and each kind of
-- add-on are printed, and the rules ("41 and 42 knobs are not available on
-- Vandlgard functions"). Reading a code's pieces in the wrong order prices the
-- wrong thing with prices that are all really printed, so the guide is what the
-- pricing agent starts from.
--
-- One row per book, keyed by file name like price_book_pages. `sha256` says
-- which copy of the PDF it was made from. A person may correct `guide`;
-- `reviewed` then says so, and the agent never overwrites a reviewed guide.

create table if not exists price_book_guides (
    file_name     text primary key,
    sha256        text not null default '',
    guide         jsonb not null,
    model         text not null default '',
    usd           numeric(10, 6) not null default 0,
    reviewed      boolean not null default false,
    reviewed_note text not null default '',
    made_at       timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

alter table price_book_guides enable row level security;

drop policy if exists price_book_guides_read on price_book_guides;
create policy price_book_guides_read on price_book_guides for select using (auth.uid() is not null);
