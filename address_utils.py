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


# ============================================================================
# DIRECTIONAL_CANONICAL -- spelled-out and abbreviated cardinal prefixes
# ============================================================================
#
# Added 2026-05-30 to close the canonicalization gap surfaced by the
# 12:22:38 starved pickup (S Post Oak): canonicalize_address handled
# SUFFIX normalization but had no analogous pass for directional
# PREFIXES, so `_road_names_match("South Post Oak Road", "S Post Oak")`
# returned False (substring check fails because `s `/`south ` share no
# common prefix at the directional token). See
# docs/RECON_ROAD_NAME_CANONICALIZATION_GAP_2026-05-30.md §1 for the
# verbatim failing trace.
#
# Design: map BOTH spelled-out AND abbreviated forms to a common
# single-letter canonical (`south` and `s` both -> `s`). After this
# pass, "S Post Oak" and "South Post Oak Road" canonicalize to forms
# that share a substring at the directional token, so the
# `s in a or a in s` check in _road_names_match succeeds:
#
#   "South Post Oak Road" -> "s post oak road" -> "s post oak rd"
#   "S Post Oak"          -> "s post oak"      -> "s post oak"
#   substring: "s post oak" in "s post oak rd" -> True
#
# N/S/E/W must remain MUTUALLY DISTINCT (do NOT cross-map). The
# mapping below produces distinct canonical letters for each cardinal,
# so "N Post Oak" and "S Post Oak" still correctly fail to match.
#
# Compound cardinals (NE/NW/SE/SW) listed first per the longest-first
# convention from SUFFIX_CANONICAL — though under \b word-boundary
# protection the order is actually safe in either direction (no
# boundary exists between "north" and "east" in "northeast", so
# \bnorth\b cannot match inside it).
#
# All patterns use hard \b word boundaries. No global str.replace()
# anywhere — that would corrupt names containing "north" / "south" as
# substrings of other tokens (e.g. "Forsyth", "Westheimer").
#
# Already-abbreviated forms (\bn\b, \bs\b, etc.) are explicit no-op
# substitutions ("n" -> "n") so the canonical form is the same whether
# the input came in abbreviated or spelled-out. Symmetric, audit-able.

DIRECTIONAL_CANONICAL = [
    # Compound cardinals first (longest-first convention).
    (r"\bnortheast\b", "ne"),
    (r"\bnorthwest\b", "nw"),
    (r"\bsoutheast\b", "se"),
    (r"\bsouthwest\b", "sw"),
    # Compound cardinals — abbreviated forms (idempotent on already-
    # canonical input). Kept explicit so the form-equivalence is
    # auditable without scrolling.
    (r"\bne\b",        "ne"),
    (r"\bnw\b",        "nw"),
    (r"\bse\b",        "se"),
    (r"\bsw\b",        "sw"),
    # Single-cardinal spelled-out forms -> single-letter canonical.
    (r"\bnorth\b",     "n"),
    (r"\bsouth\b",     "s"),
    (r"\beast\b",      "e"),
    (r"\bwest\b",      "w"),
    # Single-cardinal abbreviated forms (idempotent).
    (r"\bn\b",         "n"),
    (r"\bs\b",         "s"),
    (r"\be\b",         "e"),
    (r"\bw\b",         "w"),
]


def canonicalize_address(addr: str) -> str:
    """Normalize an address for cache keying.

    Lowercases, collapses whitespace, abbreviates spelled-out road
    suffixes, and canonicalizes cardinal-direction prefixes
    (`South`/`S` both -> `s`). "4th Street North & Orchard Street,
    Missouri City" and "4th St N & Orchard St, Missouri City" produce
    identical canonical forms.

    Directional normalization (added 2026-05-30) closes the
    `_road_names_match` gap that silently starved the 12:22:38 pickup
    on "S Post Oak" — see DIRECTIONAL_CANONICAL above.
    """
    s = addr.lower().strip()
    s = re.sub(r"\s+", " ", s)
    for pattern, repl in SUFFIX_CANONICAL:
        s = re.sub(pattern, repl, s)
    for pattern, repl in DIRECTIONAL_CANONICAL:
        s = re.sub(pattern, repl, s)
    return s
