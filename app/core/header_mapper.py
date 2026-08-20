"""Phase 3 -- map printed column headers to canonical field names.

Every firm names columns differently: HW / HDW / HDWE SET / HARDWARE GROUP all
mean the same thing. Without this the extractor works on exactly one firm's
drawings.
"""

from __future__ import annotations

import re

from app.schemas import CANONICAL_FIELDS

# Order matters: qualified aliases are tested before bare ones, or "FINISH"
# swallows "FRAME FINISH" and every frame column lands in the door column.
HEADER_ALIASES: list[tuple[str, list[str]]] = [
    ("frame_material", ["FRAME MATERIAL", "FRAME MATL", "FRM MATERIAL", "FRAME MAT"]),
    ("frame_finish", ["FRAME FINISH", "FRM FINISH", "FRAME FIN"]),
    # NUMBER is as common as NO. and was missing: two real schedules headed
    # their door column that way, and both fell through to guessing the tag
    # from the data. On one of them the guess failed and every row came back
    # with no door number at all.
    ("door_tag", ["#", "NO", "NO.", "NUMBER", "DOOR NUMBER", "MARK", "DOOR NO",
                  "DOOR NO.", "DOOR #", "TAG", "DOOR MARK", "DR NO"]),
    ("from_space", ["FROM", "FROM ROOM", "FROM SPACE"]),
    ("to_space", ["TO", "TO ROOM", "TO SPACE"]),
    # "PANEL WIDTH" first: sheets that group columns under DOOR and FRAME print
    # a bare "WIDTH" under FRAME, and matching that would report the frame's
    # width as the door's.
    ("door_width", ["PANEL WIDTH", "LEAF WIDTH", "DOOR WIDTH", "WIDTH", "W", "WD"]),
    ("door_height", ["PANEL HEIGHT", "LEAF HEIGHT", "DOOR HEIGHT", "HEIGHT", "HT", "H"]),
    ("door_type", ["PANEL TYPE", "LEAF TYPE", "DOOR TYPE", "DR TYPE", "TYPE"]),
    # PANEL / LEAF qualify the door leaf, exactly as FRAME qualifies the frame.
    ("door_material", ["PANEL MATERIAL", "PANEL MATL", "PANEL MAT L",
                       "LEAF MATERIAL", "DOOR MATERIAL", "DR MATERIAL",
                       "MATERIAL", "MATL", "MAT L"]),
    ("door_finish", ["FINISH", "DOOR FINISH", "FIN"]),
    ("threshold", ["THRESHOLD", "THRESH"]),
    ("fire_rating", ["F.R", "F_R", "FR", "RATING", "FIRE RATING", "LABEL",
                     "FIRE RTG"]),
    ("hw_set", ["HW", "HDW", "HDWE", "HW SET", "HARDWARE", "HARDWARE SET",
                "HARDWARE GROUP", "HDW SET", "HDWE SET", "HW GROUP"]),
    ("comments", ["COMMENTS", "REMARKS", "NOTES", "COMMENT"]),
]

# Periods are dropped, not kept: sheets abbreviate with them -- DR. TYPE, NO.,
# MAT'L -- and keeping them stopped every such heading matching its alias.
_PUNCT = re.compile(r"[^A-Z0-9# ]+")

# What a door tag looks like when we have to recognise it from the data alone.
_MIN_PREFIX_ALIAS = 3
# The longest a stacked header's own name may be for the "leaf" pass to trust
# it. W, H, HT and WD are what schedules actually print under a SIZE band;
# anything longer than this has already had its chance at the rules above.
_MAX_LEAF_ALIAS = 2
# Single words allowed to match in the middle of a heading, so that
# "DOOR SIZE WIDTH" is read as a width.
#
# A door schedule states exactly one width, one height and one thickness, so
# those words cannot mean anything else on it. TYPE, RATING, MATERIAL and
# FINISH are the opposite: every one of them appears twice, qualified -- DOOR
# TYPE and FRAME TYPE, FIRE RATING and ACOUSTIC RATING. Matching those loose
# puts the frame's value in the door's column, and an acoustic rating in the
# fire rating.
_SAFE_MID_HEADING = frozenset({"WIDTH", "HEIGHT", "THICKNESS", "THK", "UNDERCUT"})
# Which door field a repeated heading hands over to the frame.
_DOOR_TO_FRAME = {"door_material": "frame_material", "door_finish": "frame_finish"}
_MAX_TAG_LEN = 10
_MIN_TAG_ROWS = 3
_MIN_TAG_UNIQUENESS = 0.9


def normalize(header: str) -> str:
    text = _PUNCT.sub(" ", header.upper())
    return re.sub(r"\s+", " ", text).strip()


# Aliases go through the same normalizer as the headers they are matched
# against, or "F.R" never matches a sheet that prints "F_R".
_NORMALIZED_ALIASES: list[tuple[str, list[str]]] = [
    (field, [normalize(a) for a in aliases]) for field, aliases in HEADER_ALIASES
]


def map_headers(headers: list[str],
                overrides: dict[str, str] | None = None
                ) -> tuple[list[str | None], list[str]]:
    """Returns (canonical field per column, list of unmapped header strings).

    A field is claimed by at most one column -- if a sheet prints "FINISH" twice
    the second becomes an extra rather than overwriting the first.

    `overrides` maps a raw header string to a canonical field and is applied
    first. It carries resolutions the static alias table cannot know about --
    every firm names columns differently, and no fixed list survives that.
    """
    normalized = [normalize(h) for h in headers]
    mapped: list[str | None] = [None] * len(headers)
    claimed: set[str] = set()


    # Three passes, most specific first, because a heading can legitimately
    # match several fields and the tightest match is the right one:
    #
    #   exact     "FINISH" is a finish, and must not be stolen by a prefix rule
    #   prefix    "HARDWARE TYPE" starts with HARDWARE, so it is the hardware
    #             set -- decided before anything gets to notice it ends in TYPE
    #   contains  "DOOR SIZE WIDTH" is a width; nothing tighter claimed it
    #
    # Collapsing prefix and contains into one pass makes "HARDWARE TYPE" a door
    # type, because door_type is tried before hw_set and TYPE is inside it.
    # "leaf" runs last and only on what is still unmapped. A stacked header is
    # read as its group plus its own name -- "SIZE W x H" over "W" arrives here
    # as "SIZE W X H W" -- and no rule above can see the "W", because a
    # one-letter alias is too dangerous to match anywhere inside a heading.
    #
    # At the end of a grouped heading it is not dangerous: that last token IS
    # the column's own name and the group in front of it is context. Restricted
    # to short tokens, because anything longer already matches above. Without
    # this, every width and height on two real schedules came back empty while
    # the values sat in `extra` under "size_w_x_h_w_h".
    for mode in ("exact", "prefix", "contains", "leaf"):
        for field, aliases in _NORMALIZED_ALIASES:
            if field in claimed:
                continue
            for idx, text in enumerate(normalized):
                if mapped[idx] is not None or not text:
                    continue
                if mode == "exact":
                    hit = text in aliases
                elif mode == "prefix":
                    # Two-letter aliases match exactly or not at all: FR meant
                    # fire rating, so "FR. TYPE" -- a frame type -- was being
                    # read as the fire rating on every row.
                    hit = any(
                        len(a) >= _MIN_PREFIX_ALIAS
                        and (text.startswith(a + " ") or a.startswith(text + " "))
                        for a in aliases
                    )
                elif mode == "leaf":
                    leaf = text.rsplit(" ", 1)[-1] if " " in text else ""
                    hit = bool(leaf) and len(leaf) <= _MAX_LEAF_ALIAS \
                        and leaf in aliases
                else:
                    # A whole word anywhere in the heading. Multi-word aliases
                    # always -- "OPENING FIRE RATING" is a fire rating. Single
                    # words only where the word can mean nothing else on a door
                    # schedule; see _SAFE_MID_HEADING.
                    hit = any(
                        len(a) >= _MIN_PREFIX_ALIAS
                        and (" " in a or a in _SAFE_MID_HEADING)
                        and f" {a} " in f" {text} "
                        for a in aliases
                    )
                if hit:
                    mapped[idx] = field
                    claimed.add(field)
                    break

    # A repeated heading is the frame's. Schedules group the door's columns
    # first and the frame's second, printing MATERIAL and FINISH twice and
    # relying on the group heading above to tell them apart. Where that group
    # heading cannot be recovered, the second occurrence is still the frame's --
    # and without this it stayed unmapped, leaving frame_material empty on every
    # row while the value sat in extras.
    for idx, text in enumerate(normalized):
        if mapped[idx] is not None or not text:
            continue
        earlier = next((j for j in range(idx)
                        if normalized[j] == text and mapped[j] in _DOOR_TO_FRAME),
                       None)
        if earlier is None:
            continue
        frame_field = _DOOR_TO_FRAME[mapped[earlier]]
        if frame_field not in claimed:
            mapped[idx] = frame_field
            claimed.add(frame_field)

    # Overrides fill the gaps the alias table left -- they never displace it.
    # Applied first, a suggestion like "FRAME TYPE -> frame_material" claims the
    # field and locks out "FRAME MAT'L", which genuinely matches.
    if overrides:
        by_normalized = {normalize(k): v for k, v in overrides.items()}
        for idx, text in enumerate(normalized):
            if mapped[idx] is not None or not text:
                continue
            field = by_normalized.get(text)
            if field in CANONICAL_FIELDS and field not in claimed:
                mapped[idx] = field
                claimed.add(field)

    unmapped = [headers[i] for i, f in enumerate(mapped) if f is None and headers[i]]
    return mapped, unmapped


def extra_key(header: str, index: int) -> str:
    """Stable snake_case key for a column that did not map. Never dropped."""
    key = re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")
    return key or f"column_{index + 1}"


def infer_tag_column(mapped: list[str | None], columns: list[list[str]],
                     headers: list[str] | None = None) -> list[str | None]:
    """Claim a door_tag column from the data when no heading gave us one.

    The tag is the row's identity and the key every other source joins on, so
    losing it costs more than any other field. Headings alone are not enough: a
    model transcribing "DR. NO." as "DR. TYP" mapped a column of 1, 2, 3 to
    door_type and left the schedule with no tags at all.

    A tag column is short, near enough unique, and populated on most rows.
    """
    if "door_tag" in mapped:
        return mapped

    # A door *type* schedule genuinely uses its TYPE column as the row's
    # identity, so door_type is only reclaimed when a second, still-unplaced
    # column is plainly the type -- the DR. TYP. / DR. TYPE pair that a
    # misread heading creates.
    names = headers or []
    spare_type = any(
        "TYPE" in normalize(names[i])
        for i in range(min(len(names), len(mapped)))
        if mapped[i] is None
    )

    best: tuple[float, int] | None = None
    for index, values in enumerate(columns):
        if index >= len(mapped):
            continue
        if mapped[index] is not None and not (
            mapped[index] == "door_type" and spare_type
        ):
            continue
        filled = [v.strip() for v in values if v and v.strip()]
        if len(filled) < _MIN_TAG_ROWS or len(filled) < len(values) * 0.6:
            continue
        if any(len(v) > _MAX_TAG_LEN for v in filled):
            continue
        uniqueness = len(set(filled)) / len(filled)
        if uniqueness < _MIN_TAG_UNIQUENESS:
            continue
        # Leftmost wins on a tie: schedules put the tag first.
        score = (uniqueness, -index)
        if best is None or score > best:
            best = score
            chosen = index

    if best is None:
        return mapped
    updated = list(mapped)
    updated[chosen] = "door_tag"
    return updated


def unmapped_headers(headers: list[str], mapped: list[str | None]) -> list[str]:
    return [headers[i] for i, field in enumerate(mapped)
            if field is None and i < len(headers) and headers[i]]


def tag_column_index(mapped: list[str | None]) -> int:
    """Which column holds the door tag. Falls back to the first column."""
    for idx, field in enumerate(mapped):
        if field == "door_tag":
            return idx
    return 0
