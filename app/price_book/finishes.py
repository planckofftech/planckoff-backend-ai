"""Finish names that mean the same finish.

Hardware schedules mix two naming systems -- BHMA numbers (626) and the older
US codes (US26D) -- often within one set. A request for US26D must find a row
printed as 626 and the other way round.

The pairs are the ones Von Duprin prints side by side in its finish table
(price book 16, p. 30), so they come from a manufacturer's page rather than
memory. A name not listed here still works; it only matches itself.
"""

from __future__ import annotations

_PAIRS = [
    ("605", "US3"), ("606", "US4"), ("611", "US9"), ("612", "US10"),
    ("613", "US10B"), ("619", "US15"), ("622", "US19"), ("625", "US26"),
    ("626", "US26D"), ("626AM", "US26DAM"), ("628", "US28"), ("629", "US32"),
    ("630", "US32D"), ("630AM", "US32DAM"), ("689", "SP28"), ("691", "SP10"),
    ("693", "SPBLK"), ("695", "SP313"), ("706", "SP4"), ("710", "313"),
    ("711", "315"), ("721", "US3A"), ("728", "US4A"),
]

_GROUP: dict[str, frozenset[str]] = {}
for _a, _b in _PAIRS:
    _GROUP[_a] = _GROUP[_b] = frozenset({_a, _b})

KNOWN = frozenset(_GROUP) | {"643E", "LS", "USP", "US10A", "US11", "600", "618", "US14"}


def norm(finish: str) -> str:
    """One spelling per finish: upper case, no spaces, no "US " gap."""
    return "".join(finish.upper().split())


def same(finish: str) -> frozenset[str]:
    """Every name for this finish, itself included."""
    f = norm(finish)
    return _GROUP.get(f, frozenset({f}))
