"""The skills an agent uses to look through a maker's price books.

  book_contents   the book's table of contents: one line per page, heading and
                  how many prices it prints (the map made by book_map.py)
  search_book     the pages a text is printed on, with its lines
  read_page       one page's text, one printed line per line, cells split by |
  page_image      one page's picture, for a table its text does not make clear

A maker may have more than one book (Schlage has four), so every skill names
the book by the label it was listed under: B1, B2... Pages are PDF pages, the
ones the skills take and return; the number printed on the page is shown
beside it, since that is what the book's own "see page 177" means.

The checker is given the same skills held to the pages it is checking:
`only` names them, and any other page is refused.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.price_book import book_text
from app.price_book.agent import Reply, Skill
from app.price_book.page_finder import Book as MappedBook

# Lines of contents per request; a 460-page book is read in a few.
CONTENTS_LINES = 120
_MODELS_SHOWN = 8


@dataclass
class Shelf:
    """A maker's books as the agent sees them, by label."""

    books: dict[str, str]                               # label -> file name
    maps: dict[str, MappedBook] = field(default_factory=dict)
    only: set[tuple[str, int]] | None = None            # (label, pdf page) the agent may open

    @classmethod
    def of(cls, file_names: list[str], maps: list[MappedBook] | None = None) -> "Shelf":
        books = {f"B{i}": name for i, name in enumerate(file_names, 1)}
        by_name = {m.file_name: m for m in maps or []}
        return cls(books, {label: by_name[name] for label, name in books.items() if name in by_name})

    def listing(self) -> str:
        lines = []
        for label, name in self.books.items():
            m = self.maps.get(label)
            lines.append(f"{label}: {name}" + (f" ({len(m.pages)} pages)" if m else ""))
        return "\n".join(lines)

    def printed(self, label: str, pdf_page: int) -> str:
        m = self.maps.get(label)
        if m and 0 < pdf_page <= len(m.pages):
            return m.pages[pdf_page - 1].printed_page
        return ""

    def where(self, label: str, pdf_page: int) -> str:
        printed = self.printed(label, pdf_page)
        return f"{label} p.{pdf_page}" + (f" (printed page {printed})" if printed else "")


def _book_arg() -> dict:
    return {"type": "string", "description": "The book's label, e.g. B1"}


def _page_arg() -> dict:
    return {"type": "integer", "description": "PDF page number (not the number printed on the page)"}


def skills(shelf: Shelf) -> list[Skill]:
    """book_contents, search_book, read_page and page_image over these books.

    A checker held to `shelf.only` gets read_page and page_image alone.
    """

    def label_of(args: dict) -> str:
        label = str(args.get("book", "")).strip().upper()
        if label not in shelf.books:
            raise ValueError(f"no book {args.get('book')!r}; the books are {', '.join(shelf.books)}")
        return label

    def page_of(args: dict, label: str, count: int) -> int:
        page = int(args.get("page", 0))
        if not 0 < page <= count:
            raise ValueError(f"{label} has pages 1 to {count}")
        if shelf.only is not None and (label, page) not in shelf.only:
            raise ValueError("only the pages cited in the answer may be opened")
        return page

    async def open_book(label: str) -> book_text.Book:
        # Off the event loop: the first use fetches the PDF and reads every page.
        return await asyncio.to_thread(book_text.book, shelf.books[label])

    async def contents(args: dict) -> Reply:
        label = label_of(args)
        m = shelf.maps.get(label)
        if m is None:
            return Reply(f"{label} has no table of contents; use search_book.", error=True)
        first = max(int(args.get("first_page", 1)), 1)
        last = min(int(args.get("last_page", first + CONTENTS_LINES - 1)), first + CONTENTS_LINES - 1,
                   len(m.pages))
        lines = []
        for p in m.pages[first - 1:last]:
            if not p.has_text:
                lines.append(f"p{p.pdf_page} (picture page, no text: use page_image)")
                continue
            models = ", ".join(p.models[:_MODELS_SHOWN]) + (" ..." if len(p.models) > _MODELS_SHOWN else "")
            lines.append(f"p{p.pdf_page}" + (f" [printed {p.printed_page}]" if p.printed_page else "")
                         + f" {p.heading or '(no heading)'} | {p.prices} prices" + (f" | {models}" if models else ""))
        tail = f"\n(more: ask from page {last + 1})" if last < len(m.pages) else ""
        return Reply(f"{label} pages {first}-{last} of {len(m.pages)}:\n" + "\n".join(lines) + tail)

    async def search(args: dict) -> Reply:
        label = label_of(args)
        text = str(args.get("text", "")).strip()
        b = await open_book(label)
        found = b.search(text, int(args.get("first_page", 1) or 1), args.get("last_page"))
        if not found.hits:
            return Reply(f"'{text}' is not printed in {label}"
                         + (" in that page range" if args.get("first_page") or args.get("last_page") else "")
                         + ".")
        parts = [f"'{text}' in {label}:"]
        for h in found.hits:
            parts.append(f"- {shelf.where(label, h.pdf_page)}, {h.matched} line(s):")
            parts.extend(f"    {line}" for line in h.lines)
        if found.more:
            parts.append(f"Also on pages {', '.join(map(str, found.more))} "
                         "(narrow the search with first_page/last_page to see their lines).")
        return Reply("\n".join(parts), pages=[h.pdf_page for h in found.hits])

    async def read(args: dict) -> Reply:
        label = label_of(args)
        b = await open_book(label)
        page = page_of(args, label, b.page_count)
        text = b.page(page) or "(no text on this page: it is a picture; use page_image)"
        return Reply(f"=== {shelf.where(label, page)} ===\n{text}", pages=[page])

    async def image(args: dict) -> Reply:
        label = label_of(args)
        b = await open_book(label)
        page = page_of(args, label, b.page_count)
        png = await asyncio.to_thread(b.image, page)
        return Reply(f"The picture of {shelf.where(label, page)} follows.", image=png,
                     image_label=f"Picture of {shelf.where(label, page)}:", pages=[page])

    book_page = {"type": "object", "properties": {"book": _book_arg(), "page": _page_arg()},
                 "required": ["book", "page"]}
    out = [
        Skill("read_page", "Read one page's text, one printed line per line, table cells split by ' | '.",
              book_page, read),
        Skill("page_image", "See one page as a picture: for a table whose columns or headings the text "
              "does not make clear, or a page with no text.", book_page, image),
    ]
    if shelf.only is None:
        out = [
            Skill("book_contents", "The book's table of contents: for each page its heading, how many "
                  "prices it prints and the models priced there. Up to 120 pages per request.",
                  {"type": "object", "properties": {
                      "book": _book_arg(),
                      "first_page": {"type": "integer", "description": "PDF page to start from (default 1)"},
                      "last_page": {"type": "integer", "description": "PDF page to end at"}},
                   "required": ["book"]}, contents),
            Skill("search_book", "Find the pages a text is printed on, with the lines it is on. Case does "
                  "not matter, and spaces and hyphens in the text match any spacing. A code does not "
                  "match a longer code (L9050 does not find L9050R) unless the text ends in '*'. "
                  "Give first_page/last_page to search one section only.",
                  {"type": "object", "properties": {
                      "book": _book_arg(),
                      "text": {"type": "string", "description": "The text to find, e.g. L9050, IS-LOC, Buy America"},
                      "first_page": {"type": "integer"}, "last_page": {"type": "integer"}},
                   "required": ["book", "text"]}, search),
            *out,
        ]
    return out
