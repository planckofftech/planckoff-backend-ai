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
    ("door_tag", ["#", "NO", "NO.", "MARK", "DOOR NO", "DOOR NO.", "DOOR #", "TAG",
                  "DOOR MARK", "DR NO"]),
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

_PUNCT = re.compile(r"[^A-Z0-9#. ]+")


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


    # Exact matches first, so an exact "FINISH" is not stolen by a prefix rule.
    for exact_pass in (True, False):
        for field, aliases in _NORMALIZED_ALIASES:
            if field in claimed:
                continue
            for idx, text in enumerate(normalized):
                if mapped[idx] is not None or not text:
                    continue
                hit = (
                    text in aliases if exact_pass
                    else any(text.startswith(a + " ") or a.startswith(text + " ")
                             for a in aliases)
                )
                if hit:
                    mapped[idx] = field
                    claimed.add(field)
                    break

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


def tag_column_index(mapped: list[str | None]) -> int:
    """Which column holds the door tag. Falls back to the first column."""
    for idx, field in enumerate(mapped):
        if field == "door_tag":
            return idx
    return 0
