"""Conservative franchise grouping for provider search results.

Grouping is intentionally strict: related entries are recognized only when a
title is an exact franchise-base match after removing explicit season/movie/
special markers.  Titles that merely share a word (for example ``One Piece``
and ``One Punch Man``) are never grouped.  Provider order and original titles
are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from models.anime import normalize_title
from services.provider import ProviderAnime

_ROMAN = {
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
}
_MARKERS = {
    "season",
    "movie",
    "film",
    "special",
    "specials",
    "ova",
    "ona",
    "oav",
    "part",
    "parts",
    "volume",
    "vol",
    "episode",
}


@dataclass(frozen=True, slots=True)
class Franchise:
    """A group of provider results sharing one conservative franchise key."""

    key: str
    label: str
    entries: tuple[ProviderAnime, ...]


def franchise_key(title: str) -> str:
    """Return a conservative franchise key for an original provider title."""
    raw = title.split(":", 1)[0] if ":" in title else title
    tokens = normalize_title(raw).split()
    if not tokens:
        return normalize_title(title)

    for index, token in enumerate(tokens):
        if token in _MARKERS or (token.startswith("s") and token[1:].isdigit()):
            tokens = tokens[:index]
            break

    while tokens:
        last = tokens[-1]
        if last.isdigit() or last in _ROMAN or (
            last.startswith("s") and last[1:].isdigit()
        ):
            tokens.pop()
            continue
        break

    key = " ".join(tokens)
    if len(key) < 4 or not any(len(token) >= 3 for token in tokens):
        return normalize_title(title)
    return key


def group_franchises(results: Iterable[ProviderAnime]) -> list[Franchise]:
    """Group exact provider results without changing their relative order."""
    groups: dict[str, list[ProviderAnime]] = {}
    for result in results:
        groups.setdefault(franchise_key(result.title), []).append(result)

    franchises: list[Franchise] = []
    for key, entries in groups.items():
        label = min((entry.title for entry in entries), key=len)
        franchises.append(Franchise(key, label, tuple(entries)))
    return franchises


def flatten_franchises(franchises: Sequence[Franchise]) -> list[ProviderAnime]:
    """Return every provider result in original order for tests and lookup."""
    return [entry for franchise in franchises for entry in franchise.entries]
