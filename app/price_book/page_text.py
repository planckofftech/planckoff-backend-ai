"""A price page as text pieces with positions, grouped into printed lines.

Readers work from PyMuPDF spans rather than words. A footnote mark is its own
span, printed smaller, so dropping small spans keeps "L" from becoming "L1" --
joined into words, the mark is glued onto whatever it follows.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import fitz

# A piece printed at under this share of the page's body size is a footnote
# mark. Measured: body 6.6 pt, marks 4.1 pt.
_SMALL = 0.75
# Pieces whose vertical centres are this close sit on one printed line. Half a
# point is enough to split "NL-OP" from the rest of its row when lines are
# bucketed by rounding instead.
_SAME_LINE = 2.5
# A piece over this many times the usual line height is not on a line.
_TALL = 2.0

MONEY = re.compile(r"^\$\d{1,3}(,\d{3})*(\.\d\d)?$")
DECIMAL = re.compile(r"^\d{1,3}(,\d{3})*\.\d\d$")


@dataclass
class Piece:
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


def pieces(page: fitz.Page) -> list[Piece]:
    """Every span on the page, cleaned, with footnote marks left out."""
    out: list[Piece] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = clean(span["text"])
                if text:
                    x0, y0, x1, y1 = span["bbox"]
                    out.append(Piece(x0, y0, x1, y1, span["size"], text))
    if not out:
        return out
    body = statistics.median(p.size for p in out)
    height = statistics.median(p.y1 - p.y0 for p in out)
    # Text set sideways in the margin ("75 SERIES" on a tab) is many lines
    # tall; its centre lands between two rows and splits whichever it nears.
    return [p for p in out
            if p.size >= body * _SMALL and p.y1 - p.y0 <= height * _TALL]


def clean(text: str) -> str:
    # Non-breaking hyphens and figure spaces are typesetting, not meaning.
    text = text.replace("‑", "-").replace("‐", "-").replace("–", "-")
    text = text.replace(" ", " ").replace(" ", " ")
    text = text.replace("”", '"').replace("″", '"').replace("’", "'")
    return " ".join(text.split())


def lines(items: list[Piece]) -> list[list[Piece]]:
    """Pieces grouped into printed lines, top to bottom, each left to right."""
    rows: list[list[Piece]] = []
    centre = 0.0
    for p in sorted(items, key=lambda p: p.yc):
        if rows and abs(centre - p.yc) <= _SAME_LINE:
            rows[-1].append(p)
            centre = sum(q.yc for q in rows[-1]) / len(rows[-1])
        else:
            rows.append([p])
            centre = p.yc
    return [sorted(r, key=lambda p: p.x0) for r in rows]


def amount(text: str) -> float:
    return float(text.replace("$", "").replace(",", ""))


def count_prices(items: list[Piece]) -> int:
    return sum(1 for p in items if MONEY.match(p.text) or DECIMAL.match(p.text))
