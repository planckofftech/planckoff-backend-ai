"""Where the price-book PDFs are read from: the local folder, else R2.

On a developer's machine the books sit in "HARDWARE PRICE BOOK/" and are read
there. A deployed server has no such folder; the books are uploaded once to R2
under `price-books/<file name>` (scripts/map_price_books.py --upload) and a book
is fetched the first time a page of it is needed, then kept for the life of the
process -- step B reads a handful of pages per maker, and Hager's book is not
worth fetching once per page.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.db import files

log = logging.getLogger(__name__)

FOLDER = Path(__file__).resolve().parents[2] / "HARDWARE PRICE BOOK"
PREFIX = "price-books/"

_fetched: dict[str, Path] = {}
_lock = threading.Lock()


class BookMissing(RuntimeError):
    """The book is in neither the local folder nor R2."""


def key_for(file_name: str) -> str:
    return PREFIX + file_name


def path_of(file_name: str) -> Path:
    if FOLDER.exists():
        local = next(FOLDER.rglob(file_name), None)
        if local is not None:
            return local
    with _lock:
        if file_name in _fetched and _fetched[file_name].exists():
            return _fetched[file_name]
        if not files.available() or not files.exists(key_for(file_name)):
            raise BookMissing(f"{file_name} is not in {FOLDER} or in R2")
        _fetched[file_name] = files.fetch(key_for(file_name))
        return _fetched[file_name]
