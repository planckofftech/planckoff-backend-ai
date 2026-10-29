"""Does a finished result want a person's eye before anyone prices from it?

Every read used to end "ready". A scanned schedule whose 56 doors all came back
without a door number was ready; a plan pass that placed 1 door of 34 was
ready. Nothing on screen said otherwise, so the first to find out was whoever
priced it.

Two counts, nothing learned: how many doors carry a number, and how many were
placed on the plans. Below either line the job still ends ready -- the result
is stored and shown -- but says it needs review, in a plain sentence of what
happened. Never why: how the reading decides is not for the screen.
"""

from __future__ import annotations

from app.schemas import ExtractionResult

# A schedule with fewer of its rows numbered than this was misread, not drawn
# that way: a row without a door number is the exception on a real schedule.
_MIN_NUMBERED_SHARE = 0.8
# A plan pass placing fewer of the scheduled doors than this has looked at the
# wrong sheets, or could not read the right ones.
_MIN_PLACED_SHARE = 0.6


def schedule(result: ExtractionResult) -> str | None:
    """Why this schedule read needs review, as told to a person, or None."""
    if result.status == "unsure":
        return "Check that this is the door schedule before pricing from it."
    rows = result.rows
    unnumbered = sum(1 for r in rows if not (r.door_tag or "").strip())
    if rows and len(rows) - unnumbered < len(rows) * _MIN_NUMBERED_SHARE:
        return (f"{unnumbered} of {len(rows)} doors were read without a door "
                "number. Check the schedule before pricing from it.")
    return None


def plans(located: int, scheduled: int) -> str | None:
    """Why this plan pass needs review, as told to a person, or None."""
    if scheduled and located < scheduled * _MIN_PLACED_SHARE:
        return (f"{located} of {scheduled} doors were placed on the plans. "
                "Check the plan sheets, or choose the sheets to read.")
    return None
