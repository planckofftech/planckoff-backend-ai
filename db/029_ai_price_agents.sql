-- Pricing with agents (app/price_book/agent_pricing.py), beside step A and step B.
--
--     psql "$SUPABASE_DB_URL" -f db/029_ai_price_agents.sql
--
-- ai_prices
--   verdict 'options'  every option of a code that leaves a choice open (rose or
--                      escutcheon trim) has a checked price; a person picks one
--   engine             'pipeline' (step A and B) or 'agent'
--   notes              the pricing agent's notes: rules, inclusions, with pages
--   needs              what a person must specify ("trim letter: A, B, C or N")
--   orderable          false when the book says the code cannot be ordered as
--                      written ("41 and 42 knobs are not available on Vandlgard")
--   reasons            why the verdict is not agreed, in plain words
--
-- ai_price_calls
--   step               also agent:guide, agent:price, agent:check
--
-- ai_price_run_items   one row per item of a run, written as it moves:
--                      queued -> working (with the step its agent is on) -> done
--                      (with its answer). The frontend polls the run and shows
--                      each row as it finishes. Insert-only per run and item, so
--                      two runs never overwrite each other.

alter table ai_prices drop constraint if exists ai_prices_verdict_check;
alter table ai_prices add constraint ai_prices_verdict_check
    check (verdict in ('agreed', 'options', 'disagree', 'one_answer', 'unsure'));

alter table ai_prices add column if not exists engine    text not null default 'pipeline';
alter table ai_prices add column if not exists notes     jsonb not null default '[]';
alter table ai_prices add column if not exists needs     jsonb not null default '[]';
alter table ai_prices add column if not exists orderable boolean not null default true;
alter table ai_prices add column if not exists reasons   jsonb not null default '[]';

alter table ai_price_calls drop constraint if exists ai_price_calls_step_check;
alter table ai_price_calls add constraint ai_price_calls_step_check
    check (step in ('identify', 'A', 'B', 'agent:guide', 'agent:price', 'agent:check'));

create table if not exists ai_price_run_items (
    run_id        uuid not null references ai_price_runs(id) on delete cascade,
    item_id       text not null,
    position      int not null,
    status        text not null check (status in ('queued', 'working', 'done')),
    step          text not null default '',
    answer        jsonb,
    updated_at    timestamptz not null default now(),
    primary key (run_id, item_id)
);

alter table ai_price_run_items enable row level security;

drop policy if exists ai_price_run_items_read on ai_price_run_items;
create policy ai_price_run_items_read on ai_price_run_items for select using (auth.uid() is not null);
