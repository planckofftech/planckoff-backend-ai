-- Watching the makers' websites for new price books (app/price_book/watch.py).
--
--     psql "$SUPABASE_DB_URL" -f db/036_price_book_watch.sql
--
--   price_book_sources   where each maker publishes its price book, one row per
--                        place to look. `kind` says how it is looked at:
--                          pdf     the URL is the PDF itself
--                          page    a web page that links to the price books;
--                                  `link_pattern` picks the price-book links
--                          index   a distributor's list of many makers' books
--                                  (SECLOCK): the fallback, checked last and only
--                                  for makers whose own source found nothing new
--                          portal  behind a login: never fetched, checked by a person
--                          rep     only from the sales rep: checked by a person
--                          email   announced by email: checked by a person
--                        `verified` is a person saying the URL is right; a URL
--                        found by research starts unverified. `state` is what
--                        the last check saw (the page's ETag and Last-Modified,
--                        and each price-book link's validators and sha256), so
--                        the next check downloads only what changed.
--   price_book_updates   a PDF the watcher found that is not a book already
--                        loaded. Kept, never loaded by itself: a person looks
--                        at it and accepts it (then maps and loads it the usual
--                        way) or ignores it. `sha256` is unique, so one file is
--                        reported once however many pages link to it, and an
--                        ignored file is not reported again.
--   price_book_watch_runs  one per run of the watcher, with what each source
--                        answered.

create table if not exists price_book_sources (
    id            uuid primary key default gen_random_uuid(),
    manufacturer  text not null,
    url           text not null default '',
    kind          text not null check (kind in ('pdf', 'page', 'index', 'portal', 'rep', 'email')),
    link_pattern  text not null default '',
    active        boolean not null default true,
    verified      boolean not null default false,
    note          text not null default '',
    state         jsonb not null default '{}',
    checked_at    timestamptz,
    last_status   text not null default '',
    last_detail   text not null default '',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (manufacturer, url)
);

create table if not exists price_book_watch_runs (
    id           uuid primary key default gen_random_uuid(),
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    summary      jsonb,
    error        text not null default ''
);

create table if not exists price_book_updates (
    id              uuid primary key default gen_random_uuid(),
    source_id       uuid references price_book_sources(id) on delete set null,
    run_id          uuid references price_book_watch_runs(id) on delete set null,
    manufacturer    text not null,
    url             text not null,
    file_name       text not null,
    sha256          text not null unique,
    size_bytes      bigint not null,
    page_count      int not null default 0,
    title           text not null default '',
    effective_hint  text not null default '',
    staged_key      text not null default '',
    status          text not null default 'new' check (status in ('new', 'accepted', 'ignored')),
    note            text not null default '',
    found_at        timestamptz not null default now(),
    decided_at      timestamptz
);

create index if not exists price_book_updates_status on price_book_updates (status, found_at desc);

alter table price_book_sources    enable row level security;
alter table price_book_watch_runs enable row level security;
alter table price_book_updates    enable row level security;

-- Like the books themselves: any signed-in user may read; only the backend writes.
drop policy if exists price_book_sources_read on price_book_sources;
create policy price_book_sources_read on price_book_sources for select using (auth.uid() is not null);
drop policy if exists price_book_watch_runs_read on price_book_watch_runs;
create policy price_book_watch_runs_read on price_book_watch_runs for select using (auth.uid() is not null);
drop policy if exists price_book_updates_read on price_book_updates;
create policy price_book_updates_read on price_book_updates for select using (auth.uid() is not null);
