"""Which manufacturer a hardware schedule means, and which books are theirs.

Schedules write a maker as the industry's short code (HA, VD, LC ...), a short
form (VON, NORTON) or the full name. The codes are a table, not a list in the
code: `manufacturer_codes` in Supabase holds them, a person can add or correct
one from the frontend, and `STANDARD` below is only what that table starts
with -- and what is used where no database is configured.

A code no one has entered yet is asked of the AI (`ask_ai`), shown the code,
a few of the products written with it, and the makers whose books are loaded.
Its answer is kept as `source = 'ai'`, unverified, and checked against the
book maps: a maker whose books print the products it was given is likelier
than one whose books do not. A person confirming it makes it verified.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.price_book.rows import match_key

log = logging.getLogger(__name__)

# The codes the table starts with. A code whose maker has no book loaded still
# names the maker, so "LC" reads as "LCN", not as unknown.
STANDARD: dict[str, str] = {
    "VON": "Von Duprin", "VD": "Von Duprin", "VONDUPRIN": "Von Duprin",
    "HA": "Hager", "HAG": "Hager", "HAGER": "Hager", "HAGERCOMPANIES": "Hager",
    "LC": "LCN", "IV": "Ives", "SC": "Schlage", "ZE": "Zero", "PE": "Pemko",
    "RO": "Rockwood", "SA": "Sargent", "CR": "Corbin Russwin", "MK": "McKinney",
    "GL": "Glynn-Johnson", "GJ": "Glynn-Johnson", "TR": "Trimco", "YA": "Yale",
    "BE": "Best", "FA": "Falcon", "AR": "Adams Rite",
    "NO": "Norton Rixson", "RF": "Norton Rixson", "NORTON": "Norton Rixson", "RIXSON": "Norton Rixson",
    "NG": "National Guard", "NGP": "National Guard",
    # Markar is printed in Pemko's book.
    "MR": "Pemko", "MARKAR": "Pemko",
    "CORBIN": "Corbin Russwin", "RUSSWIN": "Corbin Russwin", "DORMA": "dormakaba",
    "DONJO": "Don-Jo", "TOWNSTEEL": "TownSteel", "ACCENTRA": "Yale",
    "DE": "Detex", "DET": "Detex", "AB": "ABH",
    # The three-letter forms many hardware schedules use instead (FAL, IVE...).
    "FAL": "Falcon", "IVE": "Ives", "ZER": "Zero", "SCH": "Schlage", "SCE": "Schlage",
    "GLY": "Glynn-Johnson", "STE": "Steelcraft", "DOR": "dormakaba", "PEM": "Pemko",
    "ROC": "Rockwood", "SAR": "Sargent", "COR": "Corbin Russwin", "MCK": "McKinney",
    "NOR": "Norton Rixson", "TRI": "Trimco", "YAL": "Yale", "BES": "Best",
}

Source = str   # 'name' | 'table' | 'ai' | 'none'

_BRACKETED = re.compile(r"\[[^\]]*\]")


@dataclass
class Code:
    """One row of the code table."""

    code: str
    manufacturer: str
    source: str = "standard"          # standard | user | ai
    verified: bool = True
    note: str = ""


@dataclass
class Resolved:
    """What one manufacturer as written on a schedule turned out to be."""

    written: str
    manufacturer: str | None
    found_by: Source
    verified: bool
    books: list[str] = field(default_factory=list)
    # Of the products written with this code, how many the maker's book maps print.
    checked: int | None = None


def standard_codes() -> dict[str, Code]:
    return {k: Code(k, m) for k, m in STANDARD.items()}


def resolve(written: str, makers: set[str], codes: dict[str, Code]) -> tuple[str | None, Source, bool]:
    """(maker, how it was found, verified) for a manufacturer as written.

    A maker's own name wins over the table, so a code can never re-point
    "Hager" at anyone else. A table entry for a maker with no book loaded is
    still returned -- with no books, which the caller reports as not loaded.
    """
    key = match_key(written)
    if not key:
        return None, "none", False
    for maker in makers:
        if match_key(maker) == key:
            return maker, "name", True
    entry = codes.get(key)
    if entry is not None:
        return entry.manufacturer, "ai" if entry.source == "ai" else "table", entry.verified
    return None, "none", False


# --------------------------------------------------------------------------- #
# a project's items, grouped by maker
# --------------------------------------------------------------------------- #

@dataclass
class Written:
    """One schedule item, as far as finding its maker goes."""

    id: str | None
    manufacturer: str
    model: str = ""
    finish: str = ""
    size: str = ""
    description: str = ""
    # The schedule's item name ("Mortise Cylinder(s)", "Closer(s)"): says which
    # section of a book to look in when a model number is printed in several.
    name: str = ""
    # Inches; lets a plate sized "less door width" (LDW) be counted.
    door_width: float | None = None

    def product(self) -> tuple[str, str, str]:
        """What makes two items the same product: quantity never does."""
        return (match_key(self.model or self.description), match_key(self.finish),
                match_key(self.size))

    def sample(self) -> str:
        return " ".join(t for t in (self.model, self.finish, self.description) if t)[:120]

    def candidates(self) -> list[str]:
        """Words that may be the model as a book prints it: '190S', '1009DU'."""
        out: list[str] = []
        for text in (self.model, self.description):
            for word in _BRACKETED.sub(" ", text).split()[:2]:
                word = word.strip(",;").upper()
                if len(word) >= 3 and any(c.isdigit() for c in word) and word not in out:
                    out.append(word)
        return out


@dataclass
class Group:
    """Every item one written manufacturer covers."""

    written: str
    items: list[Written] = field(default_factory=list)

    @property
    def products(self) -> int:
        return len({i.product() for i in self.items})


def group(items: list[Written]) -> dict[str, Group]:
    """Items by manufacturer as written, matched on letters and digits only,
    so "HA", "Ha" and "H.A." are one group."""
    out: dict[str, Group] = {}
    for item in items:
        key = match_key(item.manufacturer)
        out.setdefault(key, Group(item.manufacturer.strip())).items.append(item)
    return out


# --------------------------------------------------------------------------- #
# the AI, for codes nobody has entered yet
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You identify door hardware manufacturers from the short codes and product "
    "numbers written in construction hardware schedules. You answer only from the "
    "list of manufacturers given, and say null when none fits."
)

# Products shown per code: enough to recognise a maker's numbering, few enough
# that this stays a short text question.
_SAMPLES = 8


def _prompt(unknown: dict[str, list[str]], makers: list[str],
            printed: dict[str, list[str]]) -> str:
    listing = "\n".join(f"  {code}: {'; '.join(samples[:_SAMPLES])}"
                        for code, samples in unknown.items())
    found = "\n".join(f"  {model}: {', '.join(names) or 'no book'}"
                      for model, names in printed.items())
    return (
        "Each code below is how a hardware schedule wrote a manufacturer, followed "
        "by products written with it (model, finish, description).\n\n"
        f"Codes:\n{listing}\n\n"
        f"Manufacturers with price books: {', '.join(makers)}\n\n"
        + (f"Which books print these model numbers on a page:\n{found}\n\n" if found else "")
        + "Rules:\n"
        "- A code is almost always a short form of the maker's name: HA is Hager, "
        "VD is Von Duprin, LC is LCN. Read the code first.\n"
        "- Use the products, and the books that print them, to confirm it. A model "
        "printed in only one maker's book points to that maker; 'no book' tells "
        "nothing, since many models are built from parts and never printed whole.\n"
        "- Many makers share series numbers (several make a '1000' or a '5200'); "
        "a number alone does not name a maker.\n"
        "- If the maker is not on the list, or you are not sure, return null. A "
        "wrong maker prices the item from the wrong book.\n\n"
        'Return JSON: {"codes": {"<code>": {"manufacturer": "<name from the list or null>", '
        '"why": "<a few words>"}}}'
    )


@dataclass
class AiAnswer:
    picks: dict[str, tuple[str | None, str]]   # code -> (maker or None, why)
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0


async def ask_ai(unknown: dict[str, list[str]], makers: list[str], model: str,
                 printed: dict[str, list[str]] | None = None) -> AiAnswer:
    """Ask which listed maker each unknown code is. Raises AiUnavailableError
    when no key is set; any answer outside `makers` is taken as null.

    `printed` is, for the products' model numbers, the makers whose book maps
    print them -- found by code, and shown to the AI as evidence."""
    from app.ai import spend
    from app.ai.client import get_client
    from app.ai.pricing import cost_usd
    from app.ai.response_parser import first_object

    from app.price_book import call_log

    timer = call_log.Timer()
    response = await get_client().chat.completions.create(
        model=model, temperature=0, max_tokens=4000 + 60 * len(unknown),
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": _prompt(unknown, makers, printed or {})}],
    )
    usage = getattr(response, "usage", None)
    spend.add(model, usage)
    used_in = getattr(usage, "prompt_tokens", 0) or 0
    used_out = getattr(usage, "completion_tokens", 0) or 0
    call_log.add(call_log.Call("identify", model, "", [], [], list(unknown), used_in, used_out,
                               cost_usd(model, used_in, used_out), timer.seconds,
                               "cut_off" if response.choices[0].finish_reason == "length" else "ok"))
    if response.choices[0].finish_reason == "length":
        # A thinking model can spend the whole allowance before answering.
        raise RuntimeError(f"{model} stopped at its token limit before answering")
    raw = first_object(response.choices[0].message.content).get("codes") or {}
    by_key = {match_key(m): m for m in makers}
    picks: dict[str, tuple[str | None, str]] = {}
    for code in unknown:
        got = raw.get(code) if isinstance(raw, dict) else None
        name = got.get("manufacturer") if isinstance(got, dict) else None
        why = str(got.get("why", "")) if isinstance(got, dict) else ""
        picks[code] = (by_key.get(match_key(name)) if isinstance(name, str) else None, why)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    log.info("maker codes asked of %s: %d codes, tokens %s/%s", model, len(unknown), prompt, completion)
    return AiAnswer(picks, model, prompt, completion, cost_usd(model, prompt, completion))
