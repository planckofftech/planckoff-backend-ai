-- Who may touch these tables through the Data API.
--
-- The project was created with "Automatically expose new tables" off, which is
-- the right default -- it means nothing is reachable until somebody says so.
-- The consequence is that nothing is reachable until somebody says so, and this
-- is that file. Without it every call returns "permission denied", including
-- from the service key, because a grant and a policy are different things:
-- a GRANT decides whether a role may touch the table at all, RLS decides which
-- rows it sees. Missing grants deny before any policy is consulted.
--
-- `anon` is deliberately absent. A signed-out caller has no business reading a
-- takeoff, and leaving the grant off is a second lock behind the RLS policies.

grant usage on schema public to authenticated, service_role;

-- The backend. Bypasses RLS by design, so it still needs the grant.
grant all privileges on all tables in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;

-- A signed-in user, through the browser. Every read and write is still filtered
-- by the RLS policies in 001, which is what keeps one company out of another's
-- projects.
grant select, insert, update, delete
    on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;

-- Tables added later inherit the same arrangement, so a new table is usable
-- without anyone remembering this file -- and still not exposed to `anon`.
alter default privileges in schema public
    grant all privileges on tables to service_role;
alter default privileges in schema public
    grant select, insert, update, delete on tables to authenticated;
alter default privileges in schema public
    grant all privileges on sequences to service_role;

-- Views run with their creator's rights, so they need granting in their own
-- right; being built on granted tables is not enough.
grant select on doors_current, run_scorecard, project_summary
    to authenticated, service_role;
