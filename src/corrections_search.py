"""
Keyword search over a corrections registry.

Deliberately simple: substring and token-overlap matching over the fields a
correction is actually written in (trigger_context, correction, id, category).
No embeddings, no external services -- a registry of a few hundred entries is
small enough that a keyword search is both sufficient and fully offline.

Used by `corrections search` in corrections_cli.py; also usable standalone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Fields checked, in descending order of how strongly a match there implies
# relevance. A hit in `correction` (the rule itself) is a stronger signal than
# a hit in `category` (one word shared by dozens of entries).
_FIELD_WEIGHTS: dict[str, float] = {
    "correction": 1.0,
    "trigger_context": 0.8,
    "pattern": 0.6,
    "id": 0.6,
    "category": 0.5,
}

_TOKEN_OVERLAP_WEIGHT = 0.2
_MAX_TOKEN_OVERLAP_BONUS = 0.6  # caps token-overlap scoring at 3 shared terms
_MIN_TOKEN_LENGTH = 4  # excludes short stop words ("the", "and", "for") from overlap scoring


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (hyphens/underscores become spaces), collapse whitespace."""
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"[_\-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= _MIN_TOKEN_LENGTH}


@dataclass
class SearchMatch:
    """One scored search result."""

    entry: dict[str, Any]
    score: float
    matched_fields: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.entry.get("id", "")

    @property
    def category(self) -> str:
        return self.entry.get("category", "")

    @property
    def correction(self) -> str:
        return self.entry.get("correction", "")


def search(
    query: str,
    entries: list[dict[str, Any]],
    top_k: int = 5,
    status: str | None = None,
) -> list[SearchMatch]:
    """
    Search `entries` for the ones most relevant to `query`.

    Args:
        query:  Free-text search string.
        entries: List of correction entry dicts (as loaded from a registry YAML).
        top_k:  Maximum number of results to return.
        status: If set, only consider entries whose `status` field matches
                (e.g. "active") before scoring.

    Returns:
        Matches with score > 0, ordered by score descending. Ties keep the
        registry's original order (Python's sort is stable).
    """
    if not query or not query.strip():
        return []

    q_normalized = normalize(query)
    if not q_normalized:
        return []
    q_lower = query.strip().lower()
    q_tokens = _tokens(query)
    # Compiled once per search() call, not once per field per entry: q_lower is fixed
    # for the whole call, so every one of the (entries x fields) iterations below would
    # otherwise re-escape, re-concatenate, and re-hash the identical pattern string just
    # to hit Python's own regex cache.
    q_boundary_re = re.compile(r"\b" + re.escape(q_lower) + r"\b")

    results: list[SearchMatch] = []

    for entry in entries:
        if status is not None and entry.get("status") != status:
            continue

        score = 0.0
        matched_fields: list[str] = []

        for field_name, weight in _FIELD_WEIGHTS.items():
            value = entry.get(field_name)
            if not value:
                continue
            value_str = str(value)
            value_lower = value_str.lower()

            if q_boundary_re.search(value_lower):
                # Whole query appears verbatim at word boundaries -- strongest possible
                # signal for this field. Word-boundary anchored so a short query doesn't
                # match as a substring of an unrelated longer word (e.g. "table" inside
                # "unauditable").
                score += weight
                matched_fields.append(field_name)
                continue

            overlap = q_tokens & _tokens(value_str)
            if overlap:
                score += min(len(overlap) * _TOKEN_OVERLAP_WEIGHT, _MAX_TOKEN_OVERLAP_BONUS) * weight
                matched_fields.append(field_name)

        if score > 0:
            results.append(SearchMatch(entry=entry, score=round(score, 4), matched_fields=matched_fields))

    results.sort(key=lambda m: m.score, reverse=True)
    return results[:top_k]
