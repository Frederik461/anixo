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

from models.anime import Anime, normalize_title
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


def filter_for_anime(results: Iterable[ProviderAnime], anime: Anime) -> list[ProviderAnime]:
    """Keep only provider rows belonging to *anime* or its franchise.

    Provider search endpoints return fuzzy matches.  The AniList ID is the
    strongest identity signal; franchise-key matching additionally retains
    seasons, movies, and specials that legitimately have their own AniList IDs.
    Provider rows and their order are never rewritten or synthesized.
    """
    accepted_ids: set[str] = set()
    if anime.anilist_id:
        accepted_ids.add(str(anime.anilist_id))
    bare_id = anime.id.split(":", 1)[1] if ":" in anime.id else anime.id
    if bare_id.isdigit():
        accepted_ids.add(bare_id)

    titles = [value for value in (anime.display_title(), *anime.title.values()) if value]
    accepted_keys = {franchise_key(value) for value in titles}
    accepted_keys.update(normalize_title(value) for value in titles)

    kept: list[ProviderAnime] = []
    for result in results:
        if result.numeric_id and result.numeric_id in accepted_ids:
            kept.append(result)
            continue
        key = franchise_key(result.title)
        if key in accepted_keys or normalize_title(result.title) in accepted_keys:
            kept.append(result)
    return kept


def flatten_franchises(franchises: Sequence[Franchise]) -> list[ProviderAnime]:
    """Return every provider result in original order for tests and lookup."""
    return [entry for franchise in franchises for entry in franchise.entries]
