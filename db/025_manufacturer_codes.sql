-- How hardware schedules write each manufacturer: HA for Hager, VD for Von Duprin.
--
--     psql "$SUPABASE_DB_URL" -f db/025_manufacturer_codes.sql
--
-- A table rather than a list in the code, so a code can be added or corrected
-- without a release. `code` is kept the way the lookup matches it: letters and
-- digits only, upper-cased ("Von-Duprin" -> VONDUPRIN).
--
--   source    standard  the industry codes this table starts with
--             user      entered or corrected by a person
--             ai        the AI's answer for a code nobody had entered
--   verified  a person has confirmed it (standard codes count as confirmed).
--             An unverified AI answer is used, and shown as such.
--
-- A maker's own name always wins over this table, so no code can re-point
-- "Hager" at another maker. Written by the backend only.

create table if not exists manufacturer_codes (
    code          text primary key check (code ~ '^[A-Z0-9]+$'),
    manufacturer  text not null,
    source        text not null check (source in ('standard', 'user', 'ai')),
    verified      boolean not null,
    note          text not null default '',
    updated_at    timestamptz not null default now()
);

insert into manufacturer_codes (code, manufacturer, source, verified) values
    ('VON', 'Von Duprin', 'standard', true),
    ('VD', 'Von Duprin', 'standard', true),
    ('VONDUPRIN', 'Von Duprin', 'standard', true),
    ('HA', 'Hager', 'standard', true),
    ('HAG', 'Hager', 'standard', true),
    ('HAGER', 'Hager', 'standard', true),
    ('HAGERCOMPANIES', 'Hager', 'standard', true),
    ('LC', 'LCN', 'standard', true),
    ('IV', 'Ives', 'standard', true),
    ('SC', 'Schlage', 'standard', true),
    ('ZE', 'Zero', 'standard', true),
    ('PE', 'Pemko', 'standard', true),
    ('RO', 'Rockwood', 'standard', true),
    ('SA', 'Sargent', 'standard', true),
    ('CR', 'Corbin Russwin', 'standard', true),
    ('MK', 'McKinney', 'standard', true),
    ('GL', 'Glynn-Johnson', 'standard', true),
    ('GJ', 'Glynn-Johnson', 'standard', true),
    ('TR', 'Trimco', 'standard', true),
    ('YA', 'Yale', 'standard', true),
    ('BE', 'Best', 'standard', true),
    ('FA', 'Falcon', 'standard', true),
    ('AR', 'Adams Rite', 'standard', true),
    ('NO', 'Norton Rixson', 'standard', true),
    ('RF', 'Norton Rixson', 'standard', true),
    ('NORTON', 'Norton Rixson', 'standard', true),
    ('RIXSON', 'Norton Rixson', 'standard', true),
    ('NG', 'National Guard', 'standard', true),
    ('NGP', 'National Guard', 'standard', true),
    ('MR', 'Pemko', 'standard', true),
    ('MARKAR', 'Pemko', 'standard', true),
    ('CORBIN', 'Corbin Russwin', 'standard', true),
    ('RUSSWIN', 'Corbin Russwin', 'standard', true),
    ('DORMA', 'dormakaba', 'standard', true),
    ('DONJO', 'Don-Jo', 'standard', true),
    ('TOWNSTEEL', 'TownSteel', 'standard', true),
    ('ACCENTRA', 'Yale', 'standard', true),
    ('DE', 'Detex', 'standard', true),
    ('DET', 'Detex', 'standard', true),
    ('AB', 'ABH', 'standard', true)
    on conflict (code) do nothing;

alter table manufacturer_codes enable row level security;

drop policy if exists manufacturer_codes_read on manufacturer_codes;
create policy manufacturer_codes_read on manufacturer_codes for select using (auth.uid() is not null);
