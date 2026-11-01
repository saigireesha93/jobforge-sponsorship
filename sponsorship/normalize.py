"""Employer-name normalisation.

The same company appears in DOL disclosure data, on a job board and in an offer letter
under three different spellings:

    "BARRACUDA NETWORKS INC"
    "Barracuda Networks, Inc."
    "Barracuda Networks Inc."

All three must collapse to one lookup key, or the sponsorship check silently returns
"no filings" for a company with sixty-four of them. That failure mode is invisible and
expensive, so normalisation lives in its own module with its own tests.
"""
from __future__ import annotations

import re
import unicodedata

# Corporate suffixes stripped from the tail of a name. Order matters only in that the
# loop runs until nothing more is removed, so "Holdings LLC" loses both tokens.
_SUFFIXES = frozenset(
    """
    inc incorporated corp corporation co company llc llp lp plc ltd limited
    gmbh ag sa nv bv ab oy as pte pvt private holdings holding group
    technologies technology solutions services systems international worldwide
    usa us na america americas
    """.split()
)

# Tokens that carry no identifying signal but appear inconsistently.
_NOISE = frozenset({"the", "and", "of"})

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _ascii_fold(value: str) -> str:
    """Café -> Cafe. DOL data is ASCII but board scrapes are not."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_employer(name: str | None) -> str:
    """Collapse an employer name to a stable lookup key.

    Lowercases, folds accents, drops punctuation, removes corporate suffixes from the
    tail and filler words anywhere, then collapses whitespace.

    Returns "" for input that normalises to nothing, which callers must treat as
    "unknown employer" rather than "matches everything".
    """
    if not name:
        return ""

    text = _ascii_fold(str(name)).lower()
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t and t not in _NOISE]

    # Strip suffixes from the tail only. "Technology Partners" keeps "technology"
    # because it is not in tail position once "partners" anchors the name.
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()

    # A name made entirely of suffixes ("The Company LLC") would empty out. Fall back
    # to the punctuation-stripped form rather than returning a key that matches nothing.
    if not tokens:
        tokens = [t for t in _WS_RE.split(text) if t]

    return " ".join(tokens)


def name_variants(name: str | None) -> list[str]:
    """Lookup keys to try, most specific first.

    Used when an exact normalised match fails. "barracuda networks" also tries
    "barracuda", because DOL sometimes records only the parent brand.
    """
    key = normalize_employer(name)
    if not key:
        return []

    variants = [key]
    tokens = key.split()

    # Progressive truncation from the right, down to two tokens, then the first token
    # alone only if it is distinctive (4+ characters, so "ibm" survives but "a1" does not).
    for cut in range(len(tokens) - 1, 1, -1):
        variants.append(" ".join(tokens[:cut]))
    if len(tokens) > 1 and len(tokens[0]) >= 4:
        variants.append(tokens[0])

    seen: set[str] = set()
    ordered: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered
