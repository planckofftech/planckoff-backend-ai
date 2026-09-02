-- Which wall each door sits in.
--
--     psql "$SUPABASE_DB_URL" -f db/010_wall_type.sql
--
-- Read from the drawing's own wall-type tags: the legend says which symbols a
-- set uses, those symbols are found on the plans inside whatever shape that
-- set draws them in, and the one nearest a door is that door's wall.
--
-- On `doors` rather than on `detections`, and that is the whole design.
--
-- A door's wall type is a property of the opening. It is the same fact however
-- many sheets the door is drawn on, so it does not belong on a per-sheet
-- sighting. Putting it here also means `corrections` already covers it --
-- keyed on the door number, applied by `doors_current`, surviving every
-- re-read -- so a wrong wall type is fixed by exactly the path that fixes a
-- wrong width, with no new code.
--
-- Three states, and the middle one matters:
--
--     wall_type set                  decided. One tag was clearly nearest.
--     wall_type null, options set    the drawing is ambiguous: two tags sit at
--                                    much the same distance. A person picks.
--     both null                      nothing found. Honest, and common.
--
-- Being unsure and saying so costs nothing. Being confident and wrong is what
-- makes an estimator stop trusting a takeoff, so the middle state is
-- deliberate rather than a gap to be closed by guessing.

alter table doors
    add column if not exists wall_type text,
    -- The shortlist when the drawing does not settle it. A list of symbols,
    -- never more than three: past that a picker stops being a decision.
    add column if not exists wall_type_options jsonb,
    -- Where the answer came from, so a reader can weigh it.
    --   'tag'  read off the plan's own tags
    --   'ai'   the legend was read by the vision tier because its layout
    --          defeated the deterministic reader
    add column if not exists wall_type_source text
        check (wall_type_source is null
               or wall_type_source in ('tag', 'ai'));

-- "Show me every door in a one-hour wall" -- the question an estimator asks
-- once the types are known.
create index if not exists doors_by_wall_type
    on doors (document_id, wall_type);
