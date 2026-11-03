-- Every AI call made to price hardware, for the cost and accuracy report.
--
--     psql "$SUPABASE_DB_URL" -f db/027_ai_price_calls.sql
--
-- One row per call, kept with what it was for and what it cost as billed:
--
--   step          identify (naming a maker from a code), A (finding pages),
--                 B (reading prices off page images)
--   pages         the PDF pages sent as images (step B); empty for text calls
--   product_keys  the products the call was about, so a call's cost can be
--                 shared across them for a cost per product
--   outcome       ok, or cut_off when the model stopped at its token limit
--
-- A failed run's calls are kept too: they were paid for. Written by the
-- backend only; scripts/ai_price_report.py reads it.

create table if not exists ai_price_calls (
    id                 bigint generated always as identity primary key,
    run_id             uuid references ai_price_runs(id) on delete set null,
    project_id         text,
    step               text not null check (step in ('identify', 'A', 'B')),
    model              text not null,
    manufacturer       text not null default '',
    books              text[] not null default '{}',
    pages              int[] not null default '{}',
    product_keys       text[] not null default '{}',
    prompt_tokens      int not null,
    completion_tokens  int not null,
    usd                numeric(10, 6) not null,
    seconds            numeric(8, 2) not null,
    outcome            text not null check (outcome in ('ok', 'cut_off', 'error')),
    created_at         timestamptz not null default now()
);

create index if not exists ai_price_calls_run on ai_price_calls (run_id);

alter table ai_price_calls enable row level security;

drop policy if exists ai_price_calls_read on ai_price_calls;
create policy ai_price_calls_read on ai_price_calls for select using (auth.uid() is not null);
