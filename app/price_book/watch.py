"""Watching the makers' websites for new price books.

Each maker's price book is published somewhere: a PDF at a fixed address, a
page that links to the year's books, a distributor portal behind a login, or
only from the sales rep. `price_book_sources` lists those places
(db/036_price_book_watch.sql); this module looks at the ones that can be
looked at and reports any PDF that is not a book already loaded.

Cheap before dear. A page is asked for with the ETag and Last-Modified the
last check saw, so an unchanged page costs one 304. Each price-book link on it
is asked for the same way, and a PDF is downloaded only when its server cannot
say it is unchanged. A downloaded PDF is known by its sha256: a file already
loaded, already found, or already ignored is not reported again.

Nothing found is loaded by itself. A new book is kept (in R2, under
`price-books-incoming/`) and reported for a person to accept or ignore. A new
edition can change its layout, and a book read wrongly prices every bid that
uses it; a person looking first is the guard.

Polite by rule: robots.txt is obeyed, one host is asked at most once every few
seconds, the client names itself generically, and a login is never attempted --
portal, rep and email sources are listed as "check by hand" instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

KINDS = ("pdf", "page", "index", "portal", "rep", "email")
# `index` is a distributor's page listing many makers' books (SECLOCK). It is
# the fallback: checked after every maker's own source, and only for the makers
# whose own source found nothing new in the same run.
FETCHED = ("pdf", "page", "index")
# What a price-book link says about itself when a source gives no pattern of
# its own: its address or its text mentions a price (list) or a price book.
DEFAULT_PATTERN = r"price|pricing|list[\s_-]*price|price[\s_-]*list"
# A page linking to more price books than this is not a downloads page for one
# maker; the rest are named in the check's detail rather than fetched.
MAX_LINKS = 25
STAGE_PREFIX = "price-books-incoming/"
# Most files one index source downloads in a run. A first run against a
# distributor's whole list would otherwise fetch every book at once; the rest
# are left unrecorded and taken by the next run.
INDEX_MAX_DOWNLOADS = 30

UNCHANGED, NEW_BOOK, PAGE_CHANGED, BY_HAND, NO_LINKS, NOT_ALLOWED, ERROR = (
    "unchanged", "new_book", "page_changed", "by_hand", "no_links", "not_allowed", "error")


class WatchError(RuntimeError):
    """One address could not be checked; the words say why, plainly."""


@dataclass
class Source:
    """One row of price_book_sources."""

    manufacturer: str
    url: str
    kind: str
    id: str | None = None
    link_pattern: str = ""
    active: bool = True
    verified: bool = False
    note: str = ""
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, row: dict[str, Any]) -> "Source":
        return cls(row["manufacturer"], row.get("url") or "", row["kind"], row.get("id"),
                   row.get("link_pattern") or "", bool(row.get("active", True)),
                   bool(row.get("verified", False)), row.get("note") or "", dict(row.get("state") or {}))


@dataclass
class Found:
    """A PDF that is not a book already loaded."""

    url: str
    file_name: str
    sha256: str
    size_bytes: int
    page_count: int
    title: str
    effective_hint: str
    path: Path | None = field(default=None, repr=False)
    # Set when the book was found on an index: the maker it belongs to (our
    # name for it) and the name the index lists it under.
    manufacturer: str = ""
    listed_as: str = ""


@dataclass
class Checked:
    """What one source answered."""

    source_id: str | None
    manufacturer: str
    url: str
    status: str
    detail: str = ""
    found: list[Found] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "manufacturer": self.manufacturer, "url": self.url,
                "status": self.status, "detail": self.detail,
                "found": [{"file_name": f.file_name, "url": f.url, "sha256": f.sha256,
                           "effective_hint": f.effective_hint, "manufacturer": f.manufacturer or self.manufacturer,
                           "listed_as": f.listed_as} for f in self.found]}


# --- the web -----------------------------------------------------------------

class Web:
    """httpx with the manners: robots.txt, a gap between calls to one host, a size limit."""

    def __init__(self, user_agent: str = "PriceBookWatch/1.0", gap_s: float = 3.0,
                 max_bytes: int = 300 * 1024 * 1024, timeout_s: float = 60.0,
                 client: httpx.Client | None = None) -> None:
        self.user_agent = user_agent
        self.gap_s = gap_s
        self.max_bytes = max_bytes
        self.http = client or httpx.Client(follow_redirects=True, timeout=timeout_s)
        self.http.headers["User-Agent"] = user_agent
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._last: dict[str, float] = {}

    def close(self) -> None:
        self.http.close()

    def _wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        if self.gap_s > 0 and host in self._last:
            pause = self._last[host] + self.gap_s - time.monotonic()
            if pause > 0:
                time.sleep(pause)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(root)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                self._wait(root)
                r = self.http.get(root + "/robots.txt")
            except httpx.HTTPError:
                rp.allow_all = True        # the site itself will answer, or not, next
            else:
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            self._robots[root] = rp
        return rp.can_fetch(self.user_agent, url)

    @staticmethod
    def _conditional(prior: dict[str, Any]) -> dict[str, str]:
        headers = {}
        if prior.get("etag"):
            headers["If-None-Match"] = prior["etag"]
        if prior.get("last_modified"):
            headers["If-Modified-Since"] = prior["last_modified"]
        return headers

    def page(self, url: str, prior: dict[str, Any]) -> tuple[int, str, httpx.Headers]:
        self._wait(url)
        try:
            r = self.http.get(url, headers=self._conditional(prior))
        except httpx.HTTPError as exc:
            raise WatchError(f"the page could not be reached ({type(exc).__name__})") from exc
        if r.status_code == 304:
            return 304, "", r.headers
        if r.status_code >= 400:
            raise WatchError(f"the page answered {r.status_code}")
        return r.status_code, r.text, r.headers

    def head(self, url: str) -> httpx.Headers | None:
        self._wait(url)
        try:
            r = self.http.head(url)
        except httpx.HTTPError:
            return None
        return r.headers if r.status_code < 400 else None

    def download(self, url: str, prior: dict[str, Any]) -> tuple[int, Path | None, httpx.Headers]:
        """The PDF at `url` in a temp file, or (304, None) when the server says it is unchanged."""
        self._wait(url)
        try:
            with self.http.stream("GET", url, headers=self._conditional(prior)) as r:
                if r.status_code == 304:
                    return 304, None, r.headers
                if r.status_code >= 400:
                    raise WatchError(f"the link answered {r.status_code}")
                if int(r.headers.get("content-length") or 0) > self.max_bytes:
                    raise WatchError(f"the file is over {self.max_bytes // 1048576} MB")
                handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                path = Path(handle.name)
                size, first = 0, b""
                try:
                    with handle:
                        for chunk in r.iter_bytes(1024 * 1024):
                            if not first:
                                first = chunk[:5]
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise WatchError(f"the file is over {self.max_bytes // 1048576} MB")
                            handle.write(chunk)
                    if not first.startswith(b"%PDF"):
                        # A login page or an error page sent with a 200.
                        raise WatchError("the link did not return a PDF; it may need a login")
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
                return r.status_code, path, r.headers
        except httpx.HTTPError as exc:
            raise WatchError(f"the link could not be reached ({type(exc).__name__})") from exc


# --- reading a page and a PDF ------------------------------------------------

class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def price_book_links(html: str, base_url: str, pattern: str = "", pdf: bool = True) -> list[str]:
    """The links on a page whose address or text matches the source's pattern, in page order:
    the PDF ones, or with `pdf=False` the others (a flipbook, a viewer page, a portal link)."""
    parser = _Links()
    parser.feed(html)
    want = re.compile(pattern or DEFAULT_PATTERN, re.IGNORECASE)
    out: list[str] = []
    for href, text in parser.links:
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href.strip()).split("#")[0]
        if urlsplit(url).path.lower().endswith(".pdf") != pdf:
            continue
        if (want.search(unquote(url)) or want.search(text)) and url not in out:
            out.append(url)
    return out


def file_name_of(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip() or "price-book.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


_EFFECTIVE = re.compile(
    r"effective(?:\s+date)?\s*:?\s*"
    r"([A-Z][a-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|[A-Z][a-z]+\.?\s+\d{4})",
    re.IGNORECASE)


def inspect(path: Path) -> tuple[int, str, str]:
    """Pages, the cover's first words, and the effective date the cover prints, if it prints one.

    The date is a hint for the person reviewing, not a fact the watcher acts on.
    """
    import fitz

    try:
        with fitz.open(path) as doc:
            pages = doc.page_count
            text = "\n".join(doc[i].get_text() for i in range(min(2, pages)))
    except Exception:  # noqa: BLE001 - a damaged PDF is still reported, with what is known
        return 0, "", ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = " ".join(lines[:4])[:200]
    m = _EFFECTIVE.search(text)
    return pages, title, (f"Effective {m.group(1)}" if m else "")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


# --- checking ----------------------------------------------------------------

def _validators(headers: httpx.Headers) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if headers.get("etag"):
        out["etag"] = headers["etag"]
    if headers.get("last-modified"):
        out["last_modified"] = headers["last-modified"]
    if headers.get("content-length"):
        out["length"] = int(headers["content-length"])
    return out


def check_link(web: Web, url: str, prior: dict[str, Any], known: set[str]) -> tuple[Found | None, dict[str, Any]]:
    """Look at one PDF link. A Found only when its file is new to us; the link's new state either way."""
    if prior and not prior.get("etag") and not prior.get("last_modified") and prior.get("length"):
        # A server with no validators: an unchanged length is taken as unchanged,
        # so a book is not downloaded every week only to hash the same bytes.
        headers = web.head(url)
        if headers is not None and int(headers.get("content-length") or -1) == prior["length"]:
            return None, prior
    code, path, headers = web.download(url, prior)
    if code == 304 or path is None:
        return None, prior
    state = {**_validators(headers), "length": path.stat().st_size}
    sha = sha256_of(path)
    state["sha256"] = sha
    if sha == prior.get("sha256") or sha in known:
        path.unlink(missing_ok=True)
        return None, state
    pages, title, effective = inspect(path)
    return Found(url, file_name_of(url), sha, path.stat().st_size, pages, title, effective, path), state


def check_source(web: Web, source: Source, known: set[str], tracked: set[str] | None = None,
                 skip: set[str] | None = None) -> Checked:
    """Look at one source. Never raises: what went wrong is the answer's status and detail.

    `tracked` and `skip` are for an index source only: the makers worth reporting
    a book for, and those whose own source already found a new one this run.
    """
    if source.kind == "index" and source.url:
        return check_index(web, source, known, tracked or set(), skip or set())
    done = Checked(source.id, source.manufacturer, source.url, UNCHANGED, state=dict(source.state))
    if source.kind not in FETCHED or not source.url:
        done.status = BY_HAND
        done.detail = {"portal": "Behind a login: a person checks it.",
                       "rep": "From the sales rep: a person asks for it.",
                       "email": "Announced by email: a person watches for it."}.get(
            source.kind, "No address to check: a person checks it.")
        return done
    if not web.allowed(source.url):
        done.status, done.detail = NOT_ALLOWED, "The site's robots.txt asks that this address not be fetched."
        return done

    links_state: dict[str, dict[str, Any]] = dict(done.state.get("links") or {})
    # Price-book links that are not PDFs -- HES and Securitron publish flipbooks,
    # whose address carries the edition. A change in them is reported for a
    # person to look at; there is no file to fetch.
    others_before: list[str] | None = done.state.get("other_links")
    others: list[str] = list(others_before or [])
    try:
        if source.kind == "pdf":
            links = [source.url]
        else:
            code, html, headers = web.page(source.url, done.state.get("page") or {})
            if code == 304:
                links = list(links_state)       # the page is as it was; its files may not be
            else:
                done.state["page"] = _validators(headers)
                links = price_book_links(html, source.url, source.link_pattern)
                others = price_book_links(html, source.url, source.link_pattern, pdf=False)[:MAX_LINKS]
                done.state["other_links"] = others
            if not links and not others:
                done.status = NO_LINKS
                done.detail = ("No price-book links on the page. It may have moved, need a browser to "
                               "show its links, or need a different link pattern.")
                return done
    except WatchError as exc:
        done.status, done.detail = ERROR, str(exc)
        return done

    skipped = links[MAX_LINKS:]
    problems: list[str] = []
    new_state: dict[str, dict[str, Any]] = {}
    for url in links[:MAX_LINKS]:
        if url != source.url and not web.allowed(url):
            problems.append(f"{file_name_of(url)}: robots.txt asks that it not be fetched")
            continue
        try:
            found, new_state[url] = check_link(web, url, links_state.get(url) or {}, known)
        except WatchError as exc:
            problems.append(f"{file_name_of(url)}: {exc}")
            if url in links_state:
                new_state[url] = links_state[url]
            continue
        if found is not None:
            known.add(found.sha256)          # two links to one file report it once
            done.found.append(found)
    done.state["links"] = new_state

    appeared = [u for u in others if others_before is not None and u not in others_before]
    if done.found:
        done.status = NEW_BOOK
    elif appeared:
        done.status = PAGE_CHANGED
    elif problems and len(problems) >= len(links[:MAX_LINKS]):
        done.status = ERROR
    notes = []
    if done.found:
        notes.append("New: " + ", ".join(f.file_name for f in done.found) + ".")
    if appeared:
        notes.append("New links on the page (not PDFs; a person looks): " + ", ".join(appeared) + ".")
    notes += problems
    if skipped:
        notes.append(f"{len(skipped)} more price-book links not checked (over {MAX_LINKS}).")
    done.detail = " ".join(notes)
    return done


def check_all(web: Web, sources: list[Source], known: set[str],
              keep: Callable[[Source, Found], None] | None = None,
              tracked: set[str] | None = None) -> list[Checked]:
    """Check every active source. `keep` is given each new book before its temp file is removed.

    Index sources go last, as the fallback: a maker whose own source found a
    new book in this run is not looked for on them. `tracked` is the makers an
    index reports books for; by default every maker the sources name.
    """
    active = [s for s in sources if s.active]
    own = [s for s in active if s.kind != "index"]
    indexes = [s for s in active if s.kind == "index"]
    if tracked is None:
        tracked = {m for s in own for m in makers_named(s.manufacturer)}
    out = []
    caught: set[str] = set()
    for source in own + indexes:
        checked = check_source(web, source, known, tracked, caught)
        if source.kind != "index" and checked.status == NEW_BOOK:
            caught |= makers_named(source.manufacturer)
        for found in checked.found:
            try:
                if keep is not None:
                    keep(source, found)
            finally:
                if found.path is not None:
                    found.path.unlink(missing_ok=True)
                    found.path = None
        log.info("price watch %s %s: %s %s", source.manufacturer, source.url, checked.status, checked.detail)
        out.append(checked)
    return out


# --- a distributor's index of many makers' books (the fallback) -------------

@dataclass
class Listed:
    """One price book on an index: who it lists it under, its name, where it is, when it changed."""

    maker: str
    name: str
    url: str
    updated_at: str = ""


def _seclock(html: str) -> list[Listed]:
    """SECLOCK's price-book page. The list is page data (Inertia), not links:
    `props.manufacturers.data[].resources["Price Books"][].asset`. A file is
    served at marketing-assets.seclock.com/image/upload/<external_id>, the same
    address the site's own script builds."""
    m = re.search(r'<script[^>]*id="inertia-page"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise WatchError("the page no longer carries its list of price books where it did")
    try:
        data = json.loads(m.group(1))["props"]["manufacturers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise WatchError("the page's list of price books could not be read") from exc
    vendors = data.get("data", []) if isinstance(data, dict) else data
    out: list[Listed] = []
    for v in vendors or []:
        res = v.get("resources") if isinstance(v, dict) else None
        if not isinstance(res, dict):
            continue
        for b in res.get("Price Books") or []:
            asset = b.get("asset") or {}
            ext = asset.get("external_id")
            if ext:
                out.append(Listed(v.get("name") or "", b.get("name") or asset.get("name") or "",
                                  f"https://marketing-assets.seclock.com/image/upload/{ext}",
                                  asset.get("updated_at") or ""))
    return out


# host -> how its index is read. An index from a host not here cannot be read.
INDEX_READERS: dict[str, Callable[[str], list[Listed]]] = {
    "www.seclock.com": _seclock,
    "seclock.com": _seclock,
}

# How an index names a maker, where it is not our name (and not a code in
# makers.STANDARD). Compared on letters and digits only, any case.
INDEX_ALIASES = {
    "medecosecuritylocks": "Medeco", "pbbarchitectural": "PBB", "zktecousa": "ZKTeco",
    "schlageelectronics": "Schlage", "schlageresidential": "Schlage", "selecthinges": "Select",
    "masterlockcompany": "American Lock", "rci": "dormakaba", "dormakabamultihousing": "dormakaba",
    "accentra": "Yale",
}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def makers_named(name: str) -> set[str]:
    """The makers a source's name stands for: "Allegion (Von Duprin, Schlage)" is all three."""
    head, _, inner = name.partition("(")
    names = {head.strip()} | {n.strip() for n in inner.rstrip(")").split(",") if inner}
    return {n for n in names if n}


def maker_of_listing(listed: str, tracked: set[str]) -> str | None:
    """Our name for the maker an index lists a book under, if it is one we track."""
    from app.price_book.makers import STANDARD

    by_letters = {_letters(t): t for t in tracked}
    key = _letters(listed)
    for candidate in (key, _letters(INDEX_ALIASES.get(key, "")), _letters(STANDARD.get(key.upper(), ""))):
        if candidate and candidate in by_letters:
            return by_letters[candidate]
    return None


_EFFECTIVE_IN_NAME = re.compile(r"\((effective[^)]*)\)", re.IGNORECASE)


def check_index(web: Web, source: Source, known: set[str], tracked: set[str], skip: set[str]) -> Checked:
    """Look at a distributor's index for the tracked makers' books.

    An entry whose listed date is what the last run saw is not asked for at
    all; a changed one is fetched with the file's validators, so an unchanged
    file still costs no download.
    """
    done = Checked(source.id, source.manufacturer, source.url, UNCHANGED, state=dict(source.state))
    reader = INDEX_READERS.get(urlsplit(source.url).netloc.lower())
    if reader is None:
        done.status, done.detail = ERROR, "No reader for this site's list of price books."
        return done
    if not web.allowed(source.url):
        done.status, done.detail = NOT_ALLOWED, "The site's robots.txt asks that this address not be fetched."
        return done
    try:
        _, html, _ = web.page(source.url, {})
        listed = reader(html)
    except WatchError as exc:
        done.status, done.detail = ERROR, str(exc)
        return done

    before: dict[str, dict[str, Any]] = dict(done.state.get("links") or {})
    after: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    downloads, deferred = 0, 0
    skipped: set[str] = set()
    for entry in listed:
        if entry.url in after:
            continue                              # one file listed under two makers
        maker = maker_of_listing(entry.maker, tracked)
        if maker is None:
            continue
        prior = before.get(entry.url) or {}
        if maker in skip:
            skipped.add(maker)
            if prior:
                after[entry.url] = prior
            continue
        if prior.get("sha256") and prior.get("updated_at") == entry.updated_at:
            after[entry.url] = prior
            continue
        if downloads >= INDEX_MAX_DOWNLOADS:
            deferred += 1
            if prior:
                after[entry.url] = prior
            continue
        if not web.allowed(entry.url):
            problems.append(f"{entry.name}: robots.txt asks that it not be fetched")
            continue
        downloads += 1
        try:
            found, state = check_link(web, entry.url, prior, known)
        except WatchError as exc:
            problems.append(f"{entry.name}: {exc}")
            if prior:
                after[entry.url] = prior
            continue
        after[entry.url] = {**state, "updated_at": entry.updated_at, "maker": maker}
        if found is not None:
            known.add(found.sha256)
            found.manufacturer, found.listed_as = maker, entry.name
            if not found.effective_hint:
                m = _EFFECTIVE_IN_NAME.search(entry.name)
                found.effective_hint = m.group(1).capitalize() if m else ""
            done.found.append(found)
    done.state["links"] = after

    if done.found:
        done.status = NEW_BOOK
    elif problems and not after:
        done.status = ERROR
    notes = []
    if done.found:
        notes.append("New: " + ", ".join(f"{f.manufacturer}: {f.listed_as}" for f in done.found) + ".")
    notes.append(f"{len(after)} books of tracked makers watched on this list.")
    if skipped:
        notes.append("Not looked for here, their own site found a new book: " + ", ".join(sorted(skipped)) + ".")
    if deferred:
        notes.append(f"{deferred} more to download on the next run (at most {INDEX_MAX_DOWNLOADS} a run).")
    notes += problems
    done.detail = " ".join(notes)
    return done


# --- with the database and R2 ------------------------------------------------

def run(run_id: str, source_ids: list[str] | None = None) -> dict[str, Any]:
    """One watcher run against what is stored: check, keep what is new, record it all."""
    from app.config import get_settings
    from app.db import files
    from app.price_book import store

    settings = get_settings()
    sources = [Source.of(r) for r in store.watch_sources()]
    if source_ids:
        sources = [s for s in sources if s.id in set(source_ids)]
    known = store.known_book_shas()
    staging = files.available()
    from app.price_book.readers import BOOKS
    tracked = {maker for maker, _ in BOOKS.values()}
    tracked |= {m for s in sources if s.kind != "index" for m in makers_named(s.manufacturer)}

    def keep(source: Source, found: Found) -> None:
        key = ""
        if staging and found.path is not None:
            key = f"{STAGE_PREFIX}{found.sha256[:12]}/{found.file_name}"
            files.put(key, found.path)
        store.add_update({
            "source_id": source.id, "run_id": run_id, "manufacturer": found.manufacturer or source.manufacturer,
            "url": found.url, "file_name": found.file_name, "sha256": found.sha256,
            "size_bytes": found.size_bytes, "page_count": found.page_count, "title": found.title,
            "effective_hint": found.effective_hint, "staged_key": key,
        })

    web = Web(settings.price_watch_user_agent, settings.price_watch_gap_s,
              int(settings.price_watch_max_mb * 1048576))
    try:
        checked = check_all(web, sources, known, keep, tracked)
    finally:
        web.close()
    for c in checked:
        if c.source_id:
            store.source_checked(c.source_id, c.state, c.status, c.detail)
    counts: dict[str, int] = {}
    for c in checked:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {"counts": counts, "new_books": sum(len(c.found) for c in checked),
            "sources": [c.summary() for c in checked]}
