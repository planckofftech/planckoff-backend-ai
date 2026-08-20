-- Make the views obey row-level security.
--
-- A Postgres view runs with its *creator's* privileges by default, not the
-- caller's. These were created in the SQL Editor as the superuser, so RLS on
-- `doors` and `corrections` is evaluated as that superuser -- which is to say
-- not at all. A signed-in user selecting from `doors_current` would see every
-- organisation's doors, and `is_member()` would never be consulted.
--
-- Nothing depends on that today: only the backend reads these, and it holds the
-- service key and bypasses RLS by design. This is here so that the day someone
-- points a browser at a view -- which is the whole reason for having them -- it
-- is already safe rather than one forgotten line away from a breach.
--
-- `security_invoker` needs Postgres 15 or newer. Supabase is well past that.

alter view doors_current   set (security_invoker = true);
alter view run_scorecard    set (security_invoker = true);
alter view project_summary  set (security_invoker = true);
