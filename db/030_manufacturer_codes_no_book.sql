-- A maker code a person saved for a maker with no price book.
--
--     psql "$SUPABASE_DB_URL" -f db/030_manufacturer_codes_no_book.sql
--
--   no_book   true: the maker has no price book on file (Alarm Controls, whose
--             price list is only its introduction pages). The code then answers
--             "no price book for this maker" instead of being guessed at by AI
--             or matched to another maker's book. Saved only as an explicit
--             choice (PUT ... { "no_book": true }), so a typo is still refused.
--
-- HES and HS are added as standard codes for HES electric strikes: schedules
-- write it either way.

alter table manufacturer_codes add column if not exists no_book boolean not null default false;

insert into manufacturer_codes (code, manufacturer, source, verified) values
    ('HES', 'HES', 'standard', true),
    ('HS', 'HES', 'standard', true)
    on conflict (code) do nothing;
