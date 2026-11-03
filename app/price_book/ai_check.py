"""Whether to believe a price the AI read: the checks between step B and a person.

A price read by AI is never shown as settled. It is shown as one of:

  agreed       two different models read the same parts and the same total,
               and every part's price is printed on the page it names. Still
               unconfirmed -- a person confirms it -- but the one most likely
               right
  disagree     both models priced it, differently. Both answers are shown
  one_answer   one model priced it, the other was unsure. Shown, for a person
  unsure       neither could price it from the pages (no finish given, two
               inserts printed, a suffix with no price). Sent to a person
  rejected     an answer named a price that is not printed on its page. It is
               dropped; what is left decides among the above

The trial on Uptown Phase 4 is why there are two models: Gemini 2.5 Flash
priced more but changed its answer between runs (750S at 11.47, then 7.72),
and Haiku 5.5 never gave a wrong price but was unsure more often. Neither is
safe alone; together a wrong price has to be read the same wrong way twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from app.price_book import measure, pdfs
from app.price_book.price_reader import Read

# Every number on a page that could be a price: "$2,408", "68.31", "1,061.30".
_NUMBER = re.compile(r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\$?\d+(?:\.\d+)?")
_CENT = 0.005
# A page saying its prices are rates, by length or by area.
_MEASURE = re.compile(r"U/M is (feet|foot)|per (lineal |linear )?(foot|ft)(?![a-z])|"
                      r"square inch|sq\. ?in|per square", re.I)
_EACH = {"each", "ea", "set", "pair", "pr", "kit", ""}

Verdict = str   # agreed | disagree | one_answer | unsure


@dataclass
class Checked:
    """One model's answer for a product, after the page check."""

    model: str
    read: Read
    printed: bool = True                # every part's price is on its page
    problems: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.read.status == "priced" and self.printed and self.read.total is not None


@dataclass
class Judged:
    key: str
    verdict: Verdict
    total: float | None                  # only when agreed
    answers: list[Checked]


def _amounts(text: str) -> set[float]:
    out = set()
    for token in _NUMBER.findall(text):
        try:
            out.add(round(float(token.replace("$", "").replace(",", "")), 2))
        except ValueError:
            continue
    return out


class PageText:
    """The numbers printed on each page asked about, read once per page."""

    def __init__(self) -> None:
        self._docs: dict[str, fitz.Document] = {}
        self._pages: dict[tuple[str, int], tuple[bool, set[float]]] = {}
        self._text: dict[tuple[str, int], str] = {}

    def numbers(self, book: str, pdf_page: int) -> tuple[bool, set[float]]:
        """(the page has text, the numbers on it)."""
        key = (book, pdf_page)
        if key not in self._pages:
            if book not in self._docs:
                self._docs[book] = fitz.open(pdfs.path_of(book))
            doc = self._docs[book]
            text = doc[pdf_page - 1].get_text() if 0 < pdf_page <= doc.page_count else ""
            self._text[key] = text
            self._pages[key] = (bool(text.strip()), _amounts(text))
        return self._pages[key]

    def priced_by_measure(self, book: str, pdf_page: int) -> bool:
        """Whether this page, or the one before it, says its prices are rates.

        Hager prints "U/M is feet" on every threshold page, and the rule for
        door plates ("multiply the square inch size by the list price") on the
        page before the plates' table -- so the page before counts too. A page
        next to a section that prices by measure may be flagged with it; that
        sends an answer to a person, never a wrong one through.
        """
        for page in (pdf_page, pdf_page - 1):
            if page >= 1:
                self.numbers(book, page)
                if _MEASURE.search(self._text.get((book, page), "")):
                    return True
        return False

    def close(self) -> None:
        for d in self._docs.values():
            d.close()
        self._docs.clear()


def check_printed(model: str, read: Read, pages: PageText) -> Checked:
    """Every part's price must be a number printed on the page it names.

    A picture page has no text to look in: its parts cannot be checked here,
    so the answer is kept but cannot count as agreed until a person looks.
    """
    out = Checked(model, read)
    product = read.product
    text = " ".join(t for t in (product.model, product.description, product.size) if t)
    for part in read.parts:
        has_text, numbers = pages.numbers(part.book, part.pdf_page)
        if not has_text:
            out.printed = False
            out.problems.append(f"p{part.pdf_page} is a picture page; {part.price_text} not checked")
        elif round(part.amount, 2) not in numbers or part.amount <= 0:
            out.printed = False
            out.problems.append(f"{part.price_text} is not printed on p{part.pdf_page}")
        elif part.per.strip().lower() in _EACH and pages.priced_by_measure(part.book, part.pdf_page) \
                and not measure.same_size_row(part.row, text, product.door_width):
            # Both models read a per-foot or per-area rate as the price of one
            # (Hager 835S "x LAR", 190S plates); agreeing did not make it right.
            out.printed = False
            out.problems.append(f"p{part.pdf_page} prices by length or area; "
                                f"{part.price_text} was read as the price of one")
    for part in read.parts:
        is_rate, units = measure.expected_units(part.per, text, product.door_width)
        if not is_rate:
            continue
        # The count is worked out from the schedule's size, not taken from
        # the model: arithmetic both models got wrong alike is still wrong.
        if units is None:
            out.printed = False
            out.problems.append(f"{part.price_text} is per {part.per}; the size to count it "
                                "by is not on the schedule")
        elif abs(part.units - units) > _CENT:
            # The rate was read and checked above; only the count is off, and
            # the count is ours to make. Both models then agree on the same sum.
            out.problems.append(f"{part.price_text} per {part.per} was counted {part.units:g} times; "
                                f"counted {units:g} from the size")
            part.units = units
    if read.status == "priced" and read.parts:
        read.total = round(sum(p.extended for p in read.parts), 2)
    if read.status == "priced" and read.model_total is not None and read.total is not None \
            and abs(read.model_total - read.total) > _CENT:
        # Ours is the sum used; the model's own differing is worth a look.
        out.problems.append(f"the model added its parts to {read.model_total}, not {read.total}")
    return out


def _same(a: Checked, b: Checked) -> bool:
    if abs((a.read.total or 0) - (b.read.total or 0)) > _CENT:
        return False
    parts = lambda c: sorted((p.book, p.pdf_page, round(p.amount, 2), p.units) for p in c.read.parts)
    return parts(a) == parts(b)


def judge(key: str, answers: list[Checked]) -> Judged:
    """The verdict on one product from each model's checked answer."""
    priced = [a for a in answers if a.priced]
    if len(priced) >= 2 and all(_same(priced[0], p) for p in priced[1:]):
        return Judged(key, "agreed", priced[0].read.total, answers)
    if len(priced) >= 2:
        return Judged(key, "disagree", None, answers)
    if len(priced) == 1:
        return Judged(key, "one_answer", None, answers)
    return Judged(key, "unsure", None, answers)


def judge_all(reads: dict[str, list[Read]]) -> list[Judged]:
    """`reads` is each model's step B answers; one verdict per product."""
    pages = PageText()
    try:
        by_key: dict[str, list[Checked]] = {}
        for model, rs in reads.items():
            for r in rs:
                by_key.setdefault(r.product.key, []).append(check_printed(model, r, pages))
    finally:
        pages.close()
    return [judge(k, answers) for k, answers in by_key.items()]
