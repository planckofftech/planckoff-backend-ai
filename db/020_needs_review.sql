-- A finished job whose answer wants a person's eye.
--
--     psql "$SUPABASE_DB_URL" -f db/020_needs_review.sql
--
-- Every read used to end 'ready'. A scanned schedule whose 56 doors all came
-- back without a door number was ready; a plan pass that placed 1 door of 34
-- was ready. The job still ends 'ready' -- the result is stored and shown --
-- and this says it needs review, with the reason in `message`.
--
-- A column and not a fifth status, so a caller waiting for 'ready' keeps
-- working unchanged.

alter table reading_jobs
    add column if not exists needs_review boolean not null default false;
