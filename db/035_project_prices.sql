-- Prices per project, and one master of confirmed prices every project shares.
--
--     psql "$SUPABASE_DB_URL" -f db/035_project_prices.sql
--
-- Replaces ai_prices (db/026), which held one row per product for every
-- project at once: an unconfirmed answer from one project was overwritten by
-- the next project's run, and a project had no price of its own to change or
-- delete.
--
--   project_prices        one per project and product (maker + model + finish +
--                         size). What the AI read for this project, or what the
--                         project took from the master. The project's own copy:
--                         a later change to the master never changes it.
--                           source 'ai'      read by AI for this project
--                           source 'master'  taken from the master by lookup
--   price_master          one per product: the latest price a person confirmed,
--                         on any project. What lookup shares with every project.
--                         Only a confirmation (or an admin edit) writes it.
--   price_master_history  every confirmation, edit and removal of a master
--                         price, with the price before. Two projects confirming
--                         different prices for one product both stay on record;
--                         the latest is the master's.
--
-- Deleting a project's price never touches the master. Removing a master price
-- never touches a project's copy. A project destroyed (hard delete) takes its
-- prices with it; an archived one keeps them.
--
-- project_id is text, as ai_price_runs.project_id is: the id the frontend sends.
--
-- ai_prices is left as it was, read by nothing; drop it once this has run in
-- production and the carried-over rows have been checked.

create table if not exists price_master (
    id                uuid primary key default gen_random_uuid(),
    manufacturer      text not null,
    product_key       text not null,
    match_key         text not null,
    model             text not null default '',
    finish            text not null default '',
    size              text not null default '',
    name              text not null default '',
    description       text not null default '',
    list_price        numeric(12, 2) not null check (list_price > 0),
    parts             jsonb not null default '[]',
    pages             jsonb not null default '[]',
    note              text not null default '',
    project_id        text,
    project_price_id  uuid,
    confirmed_at      timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    unique (manufacturer, product_key)
);

create index if not exists price_master_search on price_master (manufacturer, match_key text_pattern_ops);

create table if not exists project_prices (
    id              uuid primary key default gen_random_uuid(),
    project_id      text not null,
    manufacturer    text not null,
    product_key     text not null,
    match_key       text not null,
    model           text not null default '',
    finish          text not null default '',
    size            text not null default '',
    name            text not null default '',
    description     text not null default '',
    source          text not null check (source in ('ai', 'master')),
    verdict         text check (verdict is null or
                                verdict in ('agreed', 'options', 'disagree', 'one_answer', 'unsure')),
    list_price      numeric(12, 2) check (list_price is null or list_price > 0),
    parts           jsonb not null default '[]',
    answers         jsonb not null default '[]',
    pages           jsonb not null default '[]',
    engine          text not null default 'pipeline',
    notes           jsonb not null default '[]',
    needs           jsonb not null default '[]',
    orderable       boolean not null default true,
    reasons         jsonb not null default '[]',
    confirmed       boolean not null default false,
    confirmed_note  text not null default '',
    confirmed_at    timestamptz,
    master_id       uuid references price_master(id) on delete set null,
    run_id          uuid references ai_price_runs(id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (project_id, manufacturer, product_key)
);

create index if not exists project_prices_project on project_prices (project_id);

do $$ begin
    alter table price_master add constraint price_master_project_price_fk
        foreign key (project_price_id) references project_prices(id) on delete set null;
exception when duplicate_object then null;
end $$;

-- No foreign key to price_master: the record outlives a removed master price.
create table if not exists price_master_history (
    id                bigint generated always as identity primary key,
    master_id         uuid not null,
    manufacturer      text not null,
    product_key       text not null,
    model             text not null default '',
    finish            text not null default '',
    size              text not null default '',
    action            text not null check (action in ('confirmed', 'edited', 'removed')),
    list_price        numeric(12, 2),
    previous_price    numeric(12, 2),
    project_id        text,
    project_price_id  uuid,
    note              text not null default '',
    at                timestamptz not null default now()
);

create index if not exists price_master_history_product
    on price_master_history (manufacturer, product_key, at desc);
create index if not exists price_master_history_master on price_master_history (master_id, at desc);


-- --------------------------------------------------------------------------- --
-- writes that must happen together: one function each, one transaction each
-- --------------------------------------------------------------------------- --

-- A person confirms a project's price: the project row is settled, the master
-- takes it (the latest confirmation wins), and the history keeps the price the
-- master had before. Null when there is no such project price.
create or replace function confirm_project_price(p_id uuid, p_price numeric, p_note text, p_parts jsonb)
returns jsonb language plpgsql as $$
declare
    pp    project_prices;
    m     price_master;
    prev  numeric;
begin
    update project_prices set
        list_price = p_price, confirmed = true, confirmed_note = coalesce(p_note, ''),
        confirmed_at = now(), updated_at = now(), parts = coalesce(p_parts, parts)
    where id = p_id returning * into pp;
    if not found then
        return null;
    end if;

    select list_price into prev from price_master
    where manufacturer = pp.manufacturer and product_key = pp.product_key for update;

    insert into price_master (manufacturer, product_key, match_key, model, finish, size, name, description,
                              list_price, parts, pages, note, project_id, project_price_id)
    values (pp.manufacturer, pp.product_key, pp.match_key, pp.model, pp.finish, pp.size, pp.name,
            pp.description, p_price, pp.parts, pp.pages, coalesce(p_note, ''), pp.project_id, pp.id)
    on conflict (manufacturer, product_key) do update set
        match_key = excluded.match_key, model = excluded.model, finish = excluded.finish,
        size = excluded.size, name = excluded.name, description = excluded.description,
        list_price = excluded.list_price, parts = excluded.parts, pages = excluded.pages,
        note = excluded.note, project_id = excluded.project_id,
        project_price_id = excluded.project_price_id, confirmed_at = now(), updated_at = now()
    returning * into m;

    update project_prices set master_id = m.id where id = pp.id returning * into pp;

    insert into price_master_history (master_id, manufacturer, product_key, model, finish, size, action,
                                      list_price, previous_price, project_id, project_price_id, note)
    values (m.id, m.manufacturer, m.product_key, m.model, m.finish, m.size, 'confirmed',
            p_price, prev, pp.project_id, pp.id, coalesce(p_note, ''));

    return jsonb_build_object('project_price', to_jsonb(pp), 'master', to_jsonb(m), 'previous_price', prev);
end $$;

-- An admin corrects a master price. Projects keep their own copies.
create or replace function edit_master_price(p_id uuid, p_price numeric, p_note text)
returns jsonb language plpgsql as $$
declare
    m     price_master;
    prev  numeric;
begin
    select list_price into prev from price_master where id = p_id for update;
    if not found then
        return null;
    end if;
    update price_master set list_price = p_price, note = coalesce(p_note, ''), updated_at = now()
    where id = p_id returning * into m;
    insert into price_master_history (master_id, manufacturer, product_key, model, finish, size, action,
                                      list_price, previous_price, note)
    values (m.id, m.manufacturer, m.product_key, m.model, m.finish, m.size, 'edited',
            p_price, prev, coalesce(p_note, ''));
    return jsonb_build_object('master', to_jsonb(m), 'previous_price', prev);
end $$;

-- An admin removes a master price. Projects keep their own copies (their
-- master_id is cleared); the history keeps the removal.
create or replace function remove_master_price(p_id uuid, p_note text)
returns jsonb language plpgsql as $$
declare
    m  price_master;
begin
    delete from price_master where id = p_id returning * into m;
    if not found then
        return null;
    end if;
    insert into price_master_history (master_id, manufacturer, product_key, model, finish, size, action,
                                      list_price, previous_price, note)
    values (m.id, m.manufacturer, m.product_key, m.model, m.finish, m.size, 'removed',
            null, m.list_price, coalesce(p_note, ''));
    return jsonb_build_object('removed', to_jsonb(m));
end $$;

revoke all on function confirm_project_price(uuid, numeric, text, jsonb) from public, anon, authenticated;
revoke all on function edit_master_price(uuid, numeric, text)            from public, anon, authenticated;
revoke all on function remove_master_price(uuid, text)                   from public, anon, authenticated;
grant execute on function confirm_project_price(uuid, numeric, text, jsonb) to service_role;
grant execute on function edit_master_price(uuid, numeric, text)            to service_role;
grant execute on function remove_master_price(uuid, text)                   to service_role;

-- A project destroyed takes its prices; archiving does not delete the row.
create or replace function drop_project_prices() returns trigger language plpgsql as $$
begin
    delete from project_prices where project_id = old.id::text;
    return old;
end $$;

drop trigger if exists projects_drop_prices on projects;
create trigger projects_drop_prices after delete on projects
    for each row execute function drop_project_prices();


-- --------------------------------------------------------------------------- --
-- who may read
-- --------------------------------------------------------------------------- --

alter table project_prices       enable row level security;
alter table price_master         enable row level security;
alter table price_master_history enable row level security;

drop policy if exists project_prices_read on project_prices;
create policy project_prices_read on project_prices for select using (auth.uid() is not null);
drop policy if exists price_master_read on price_master;
create policy price_master_read on price_master for select using (auth.uid() is not null);
drop policy if exists price_master_history_read on price_master_history;
create policy price_master_history_read on price_master_history for select using (auth.uid() is not null);


-- --------------------------------------------------------------------------- --
-- carried over from ai_prices
-- --------------------------------------------------------------------------- --
-- Confirmed prices become the master, keeping their ids. Every row whose last
-- run named a project becomes that project's price, keeping its id, so an
-- ai_price_id the frontend holds still confirms. An unconfirmed answer with no
-- project is not carried: it stays in ai_prices.

insert into price_master (id, manufacturer, product_key, match_key, model, finish, size, name, description,
                          list_price, parts, pages, note, project_id, confirmed_at, updated_at)
select a.id, a.manufacturer, a.product_key, a.match_key, a.model, a.finish, a.size, a.name, a.description,
       a.list_price, a.parts, a.pages, a.verified_note, r.project_id,
       coalesce(a.verified_at, a.updated_at), a.updated_at
from ai_prices a left join ai_price_runs r on r.id = a.run_id
where a.verified and a.list_price is not null
on conflict do nothing;

insert into price_master_history (master_id, manufacturer, product_key, model, finish, size, action,
                                  list_price, project_id, note, at)
select m.id, m.manufacturer, m.product_key, m.model, m.finish, m.size, 'confirmed',
       m.list_price, m.project_id, 'carried over from ai_prices', m.confirmed_at
from price_master m
where not exists (select 1 from price_master_history h where h.master_id = m.id);

insert into project_prices (id, project_id, manufacturer, product_key, match_key, model, finish, size, name,
                            description, source, verdict, list_price, parts, answers, pages, engine, notes,
                            needs, orderable, reasons, confirmed, confirmed_note, confirmed_at, master_id,
                            run_id, created_at, updated_at)
select a.id, r.project_id, a.manufacturer, a.product_key, a.match_key, a.model, a.finish, a.size, a.name,
       a.description, 'ai', a.verdict, a.list_price, a.parts, a.answers, a.pages, a.engine, a.notes,
       a.needs, a.orderable, a.reasons, a.verified, a.verified_note, a.verified_at,
       case when a.verified and a.list_price is not null then a.id end, a.run_id, a.updated_at, a.updated_at
from ai_prices a join ai_price_runs r on r.id = a.run_id
where r.project_id is not null
on conflict do nothing;

update price_master m set project_price_id = p.id
from project_prices p
where p.id = m.id and m.project_price_id is null;

-- The Data API learns of the new functions now, not at its next restart.
notify pgrst, 'reload schema';
