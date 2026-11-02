"""What every reader needs from a page besides its prices."""

from __future__ import annotations

import re
from datetime import datetime

import fitz

_EFFECTIVE = re.compile(
    r"Effective\s+(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s*(\d{4})")
_SLASH_DATE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\b")


def effective_date(doc: fitz.Document, pages: int = 6) -> str | None:
    """The date the book says its prices take effect, as an ISO date.

    "Effective February 27, 2026" where the book says so in words; otherwise
    the date a page header carries most often ("03/01/2026" on Hager's).
    """
    counts: dict[str, int] = {}
    for i in range(min(pages, doc.page_count)):
        text = doc[i].get_text()
        m = _EFFECTIVE.search(text)
        if m:
            return datetime.strptime(" ".join(m.groups()), "%B %d %Y").date().isoformat()
        for mm, dd, yyyy in _SLASH_DATE.findall(text):
            iso = f"{yyyy}-{mm}-{dd}"
            counts[iso] = counts.get(iso, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    return None


def printed_page(text: str) -> str:
    """The page number printed in the running header, or "".

    Both "30  Price book 16 ..." and "... Rev. 10/26  31" (Von Duprin, left
    and right pages) and "25\\n03/01/2026" (Hager) put it at the top.
    """
    head = text[:200]
    # Von Duprin separates header parts with em spaces and a private-use
    # glyph (U+F0A0), which \s does not cover.
    m = re.match(r"\s*(\d{1,4})[^\w\n]{1,12}Price book", head)
    if m:
        return m.group(1)
    m = re.search(r"Rev\. [\d/]+[^\w\n]{1,12}(\d{1,4})\b", head)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\n)\s*(\d{1,4})\s*\n?\s*\d{2}/\d{2}/\d{4}", head)
    if m:
        return m.group(1)
    return ""
