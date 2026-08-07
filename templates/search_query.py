"""Shared search-query parser supporting AND / OR / exclusion operators.

The same parsed representation drives both the SQL keyword search
(`/api/slides`) and the in-memory facet counting (`/api/filters`) so the
two always agree on which slides a query matches.

Supported syntax (Google-style, Japanese-input friendly):

  - Space between terms          -> AND   (foo bar  = foo AND bar)
  - ``OR`` (or ``|``) between terms -> OR  (foo OR bar)
  - Leading ``-``                -> exclude (NOT)  (foo -bar)
  - Double quotes                -> phrase with spaces ("foo bar")

Full-width variants commonly produced by Japanese IMEs are normalised:
full-width space, smart/full-width quotes, full-width ``｜`` and the
full-width / Unicode minus signs.

Precedence: AND binds tighter than OR, so ``a b OR c d`` parses as
``(a AND b) OR (c AND d)``. Exclusions are global ANDed-NOT terms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Match an optionally-negated quoted phrase, a quoted phrase, or a bare
# (whitespace-delimited) token.
_TOKEN_RE = re.compile(r'-"[^"]*"|"[^"]*"|\S+')
_OR_TOKENS = {"OR", "|"}


@dataclass
class ParsedQuery:
    # OR of AND-groups. e.g. [["a", "b"], ["c"]] == (a AND b) OR c
    or_groups: list[list[str]] = field(default_factory=list)
    # Terms that must NOT appear.
    excludes: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.or_groups and not self.excludes

    @property
    def positive_terms(self) -> list[str]:
        """Flat list of every positive term (across all OR groups)."""
        return [t for group in self.or_groups for t in group]


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].strip()
    return tok.strip()


def parse_search_query(q: str | None) -> ParsedQuery:
    raw = (q or "").strip()
    parsed = ParsedQuery(raw=raw)
    if not raw:
        return parsed

    norm = (
        raw.replace("\u3000", " ")  # full-width space
        .replace("\u201c", '"')  # left double quote
        .replace("\u201d", '"')  # right double quote
        .replace("\uff02", '"')  # full-width quote
        .replace("\uff5c", "|")  # full-width vertical bar
        .replace("\uff0d", "-")  # full-width hyphen-minus
        .replace("\u2212", "-")  # minus sign
    )

    current: list[str] = []
    for tok in _TOKEN_RE.findall(norm):
        if tok.upper() in _OR_TOKENS:
            if current:
                parsed.or_groups.append(current)
                current = []
            continue
        neg = tok.startswith("-")
        body = tok[1:] if neg else tok
        term = _strip_quotes(body)
        if not term:
            continue
        if neg:
            parsed.excludes.append(term)
        else:
            current.append(term)
    if current:
        parsed.or_groups.append(current)

    # Query consisted only of operators/empties that produced nothing
    # actionable -> treat the whole raw string as a single literal term
    # so the user still gets a sensible search.
    if parsed.is_empty and raw:
        parsed.or_groups.append([raw])
    return parsed


VALID_SOURCES = ("pptx", "confluence")


def normalize_sources(values: list[str] | None) -> set[str] | None:
    """Normalise a requested source-type filter into the set of
    ``Slide.source_type`` values to restrict to, or ``None`` for "no
    restriction" (search every source).

    Shared by the search endpoint (``/api/slides``), the facet-count
    endpoint (``/api/filters``) and the chat endpoint (``/api/ask``) so the
    パワポ / コンフル filter behaves identically everywhere. Selecting both
    sources (or none / unknown values) means "all", so we return ``None`` to
    skip the SQL/in-memory predicate entirely.
    """
    if not values:
        return None
    sel = {
        v.strip().lower()
        for v in values
        if v and v.strip().lower() in VALID_SOURCES
    }
    if not sel or sel == set(VALID_SOURCES):
        return None
    return sel


def query_matches(parsed: ParsedQuery, haystack: str) -> bool:
    """Evaluate a parsed query against a plain-text haystack (substring,
    case-insensitive). Used for in-memory facet counting."""
    if parsed.is_empty:
        return True
    h = haystack.lower()
    if parsed.or_groups:
        matched = any(
            all(term.lower() in h for term in group) for group in parsed.or_groups
        )
        if not matched:
            return False
    for term in parsed.excludes:
        if term.lower() in h:
            return False
    return True
