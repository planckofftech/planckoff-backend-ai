-- Hardware prices read by AI from the manufacturers' books, and the runs that read them.
--
--     psql "$SUPABASE_DB_URL" -f db/026_ai_prices.sql
--
--   ai_price_runs  one per "price these items with AI": its items, its status
--                  (running, done, failed), what it found and what it cost.
--                  Runs in the background; the frontend polls it.
--   ai_prices      one per distinct product of a maker (model + finish + size,
--                  matched on letters and digits). Holds the verdict of the
--                  checks (agreed, disagree, one_answer, unsure), every model's
--                  answer with its parts and pages, and -- once a person has
--                  looked -- the confirmed list price with `verified` true.
--
-- A verified price is never read again by AI: the next project with the same
-- product gets it from here, at no cost. An unverified one is replaced by the
-- next run's answer. `list_price` is set only when agreed or confirmed; an
-- answer a person has not settled carries no price, never a guess.

create table if not exists ai_price_runs (
    id            uuid primary key default gen_random_uuid(),
    project_id    text,
    status        text not null check (status in ('running', 'done', 'failed')),
    items         jsonb not null,
    result        jsonb,
    counts        jsonb,
    usd           numeric(10, 6) not null default 0,
    error         text not null default '',
    created_at    timestamptz not null default now(),
    finished_at   timestamptz
);

create table if not exists ai_prices (
    id            uuid primary key default gen_random_uuid(),
    manufacturer  text not null,
    product_key   text not null,
    match_key     text not null,
    model         text not null default '',
    finish        text not null default '',
    size          text not null default '',
    name          text not null default '',
    description   text not null default '',
    verdict       text not null check (verdict in ('agreed', 'disagree', 'one_answer', 'unsure')),
    list_price    numeric(12, 2) check (list_price is null or list_price > 0),
    parts         jsonb not null default '[]',
    answers       jsonb not null default '[]',
    pages         jsonb not null default '[]',
    verified      boolean not null default false,
    verified_note text not null default '',
    verified_at   timestamptz,
    run_id        uuid references ai_price_runs(id) on delete set null,
    updated_at    timestamptz not null default now(),
    unique (manufacturer, product_key)
);

create index if not exists ai_prices_search on ai_prices (manufacturer, match_key text_pattern_ops);

alter table ai_price_runs enable row level security;
alter table ai_prices     enable row level security;

drop policy if exists ai_price_runs_read on ai_price_runs;
create policy ai_price_runs_read on ai_price_runs for select using (auth.uid() is not null);
drop policy if exists ai_prices_read on ai_prices;
create policy ai_prices_read on ai_prices for select using (auth.uid() is not null);
