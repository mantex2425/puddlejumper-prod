"""
address_utils.py — shared address-string utilities

Public functions and constants used wherever named-road or address strings
need normalization. Currently consumers are bead_on_wire.py (intersection
cache keying, line 287 in pre-D.0) and pivot_context.py (road-name fuzzy
matching).

RELOCATION HISTORY (Phase D.0, 2026-04-26):
canonicalize_address (was: _canonicalize_address) and SUFFIX_CANONICAL
(was: _SUFFIX_CANONICAL) were relocated from bead_on_wire.py PRIMITIVE 2.
The leading underscore was dropped because both symbols are now public
across modules (Q27 verdict). Function body is otherwise unchanged.
"""

import re


# ============================================================================
# SUFFIX_CANONICAL -- spelled-out road suffix to abbreviated form
# ============================================================================

# Cache-key canonicalization: map all spelled-out road suffixes to their
# abbreviated forms so "4th Street" and "4th St" hit the same cache entry.
# Ordered longest-first so "Boulevard" matches before "Blvd".
SUFFIX_CANONICAL = [
    (r"\bstreet\b",    "st"),
    (r"\bavenue\b",    "ave"),
    (r"\bboulevard\b", "blvd"),
    (r"\bdrive\b",     "dr"),
    (r"\broad\b",      "rd"),
    (r"\blane\b",      "ln"),
    (r"\bcourt\b",     "ct"),
    (r"\bplace\b",     "pl"),
    (r"\bparkway\b",   "pkwy"),
    (r"\bfreeway\b",   "fwy"),
    (r"\bhighway\b",   "hwy"),
    (r"\btrail\b",     "trl"),
    (r"\bcircle\b",    "cir"),
    (r"\bterrace\b",   "ter"),
    (r"\btrace\b",     "trce"),
    (r"\bcrossing\b",  "xing"),
    (r"\bpoint\b",     "pt"),
]


def canonicalize_address(addr: str) -> str:
    """Normalize an address for cache keying.

    Lowercases, collapses whitespace, and abbreviates spelled-out road
    suffixes. "4th Street & Orchard Street, Missouri City" and
    "4th St & Orchard St, Missouri City" produce identical canonical forms.
    """
    s = addr.lower().strip()
    s = re.sub(r"\s+", " ", s)
    for pattern, repl in SUFFIX_CANONICAL:
        s = re.sub(pattern, repl, s)
    return s
