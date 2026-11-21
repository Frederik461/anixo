"""Domain models shared by screens, services, and migrations."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

_EPISODE = re.compile(r"^(\d+)(?:\.\d+)?$")


def _normalize_characters(value: str) -> str:
    """Keep Unicode letters/numbers and replace every other run with a space.

    ``re`` on Python 3.12 does not support JavaScript-style ``\\p{L}`` classes,
    so category checks are explicit here. This matches the legacy title
    normalization closely enough to preserve duplicate detection semantics.
    """
    result: list[str] = []
    pending_space = False
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        if category.startswith(("L", "N")):
            if pending_space and result:
                result.append(" ")
            pending_space = False
            result.append(character)
        else:
            pending_space = True
    return "".join(result)


def normalize_title(value: str) -> str:
    """Normalize a title using the legacy Unicode comparison rules."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_apostrophes = decomposed.replace("\u2019", "").replace("'", "")
    return _normalize_characters(without_apostrophes.casefold()).strip()


def canonical_id(anime: "Anime") -> str:
    """Preserve provider-prefixed IDs; default bare IDs to AniList."""
    if anime.id.startswith(("ani-cli:", "anilist:", "local:")):
        return anime.id
    return f"anilist:{anime.id}"


def is_same_anime(first: "Anime", second: "Anime") -> bool:
    if first.id == second.id:
        return True
    first_title = normalize_title(first.title.primary())
    second_title = normalize_title(second.title.primary())
    return bool(first_title) and first_title == second_title


def next_episode(value: str | None) -> str | None:
    """Increment only plain integer or decimal episode strings."""
    if not value:
        return None
    match = _EPISODE.match(value.strip())
    if not match:
        return None
    return str(int(match.group(1)) + 1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class AnimeTitle:
    english: str | None = None
    romaji: str | None = None
    native: str | None = None

    def values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.english, self.romaji, self.native)
            if isinstance(value, str) and value.strip()
        )

    def primary(self) -> str:
        return next(iter(self.values()), "Unknown anime")

    def normalized(self) -> frozenset[str]:
        return frozenset(normalize_title(value) for value in self.values())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "AnimeTitle":
        data = data or {}
        return cls(
            english=data.get("english") or data.get("en") or None,
            romaji=data.get("romaji") or data.get("romanji") or None,
            native=data.get("native") or data.get("userPreferred") or None,
        )

    def to_mapping(self) -> dict[str, str | None]:
        return {"english": self.english, "romaji": self.romaji, "native": self.native}


@dataclass(slots=True)
class Anime:
    id: str
    title: AnimeTitle
    cover_url: str | None = None
    cover_color: str | None = None
    banner_url: str | None = None
    description: str | None = None
    format: str | None = None
    episodes: int | None = None
    season: str | None = None
    season_year: int | None = None
    average_score: int | None = None
    popularity: int | None = None
    status: str | None = None
    genres: tuple[str, ...] = ()
    studios: tuple[str, ...] = ()
    is_adult: bool = False
    source: str | None = None
    source_id: str | None = None
    provider: str | None = None
    provider_title: str | None = None
    anilist_id: int | None = None
    updated_at: datetime = field(default_factory=utcnow)

    def display_title(self) -> str:
        return self.title.primary()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Anime":
        data = data or {}
        title_data = data.get("title")
        if isinstance(title_data, AnimeTitle):
            parsed_title = title_data
        else:
            parsed_title = AnimeTitle.from_mapping(
                title_data if isinstance(title_data, Mapping) else None
            )
        if not parsed_title.values() and isinstance(data.get("titleText"), Mapping):
            parsed_title = AnimeTitle.from_mapping(data["titleText"])
        if not parsed_title.values() and data.get("name"):
            parsed_title = AnimeTitle(romaji=str(data["name"]))

        cover_image = data.get("coverImage")
        if not isinstance(cover_image, Mapping):
            cover_image = {}
        anime_id = str(data.get("id") or data.get("anilist_id") or "")
        if not anime_id:
            anime_id = normalize_title(parsed_title.primary()) or "unknown"

        raw_genres = data.get("genres") or ()
        if isinstance(raw_genres, str):
            raw_genres = (raw_genres,)
        raw_studios = data.get("studios") or ()
        if isinstance(raw_studios, Mapping):
            raw_studios = raw_studios.get("nodes") or ()
        if isinstance(raw_studios, str):
            raw_studios = (raw_studios,)
        studio_names: list[str] = []
        for studio in raw_studios:
            if isinstance(studio, Mapping):
                name = studio.get("name")
            else:
                name = studio
            if name:
                studio_names.append(str(name))

        start_date = data.get("startDate")
        season_year = data.get("seasonYear") or data.get("season_year")
        if season_year is None and isinstance(start_date, Mapping):
            season_year = start_date.get("year")

        return cls(
            id=anime_id,
            title=parsed_title,
            cover_url=(
                data.get("coverUrl")
                or data.get("cover_url")
                or cover_image.get("extraLarge")
                or cover_image.get("large")
            ),
            cover_color=(
                data.get("coverColor")
                or data.get("cover_color")
                or cover_image.get("color")
            ),
            banner_url=data.get("bannerUrl") or data.get("bannerImage"),
            description=data.get("description"),
            format=data.get("format"),
            episodes=data.get("episodes"),
            season=data.get("season"),
            season_year=season_year,
            average_score=data.get("averageScore") or data.get("average_score"),
            popularity=data.get("popularity"),
            status=data.get("status"),
            genres=tuple(str(item) for item in raw_genres),
            studios=tuple(studio_names),
            is_adult=bool(data.get("isAdult") or data.get("is_adult") or False),
            source=data.get("source"),
            source_id=(
                data.get("sourceId")
                or data.get("source_id")
                or data.get("provider_id")
            ),
            provider=data.get("provider"),
            provider_title=data.get("providerTitle") or data.get("provider_title"),
            anilist_id=data.get("anilistId") or data.get("anilist_id"),
            updated_at=parse_iso(data.get("updatedAt")) or utcnow(),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title.to_mapping(),
            "coverUrl": self.cover_url,
            "coverColor": self.cover_color,
            "bannerImage": self.banner_url,
            "description": self.description,
            "format": self.format,
            "episodes": self.episodes,
            "season": self.season,
            "seasonYear": self.season_year,
            "averageScore": self.average_score,
            "popularity": self.popularity,
            "status": self.status,
            "genres": list(self.genres),
            "studios": list(self.studios),
            "isAdult": self.is_adult,
            "source": self.source,
            "sourceId": self.source_id,
            "provider": self.provider,
            "providerTitle": self.provider_title,
            "anilistId": self.anilist_id,
            "updatedAt": self.updated_at.isoformat(),
        }


@dataclass(slots=True)
class HistoryEntry:
    id: str
    anime: Anime
    episode: str | None = None
    last_watched: datetime | None = None
    watched_at: datetime | None = None
    updated_at: datetime | None = None
    source: str | None = None
    source_id: str | None = None
    progress_seconds: float | None = None
    duration_seconds: float | None = None
    title_snapshot: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HistoryEntry":
        anime_data = data.get("anime") if isinstance(data.get("anime"), Mapping) else data
        anime = Anime.from_mapping(anime_data or {})
        raw_id = str(data.get("id") or anime.id or "")
        explicit_source_id = data.get("sourceId") or data.get("source_id") or anime.source_id
        source_id = explicit_source_id or raw_id.split(":", 1)[1] if ":" in raw_id else explicit_source_id
        watched_at = parse_iso(data.get("watchedAt") or data.get("lastWatched"))
        updated_at = parse_iso(data.get("updatedAt"))
        return cls(
            id=raw_id or str(source_id or anime.id),
            anime=anime,
            episode=str(data["episode"]) if data.get("episode") is not None else None,
            last_watched=watched_at or updated_at,
            watched_at=watched_at,
            updated_at=updated_at,
            source=data.get("source") or anime.source,
            source_id=source_id,
            progress_seconds=data.get("progressSeconds"),
            duration_seconds=data.get("durationSeconds"),
            title_snapshot=data.get("title") or data.get("titleSnapshot"),
        )


@dataclass(slots=True)
class WatchlistEntry:
    anime: Anime
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WatchlistEntry":
        anime_data = data.get("anime") if isinstance(data.get("anime"), Mapping) else data
        return cls(
            anime=Anime.from_mapping(anime_data or {}),
            created_at=(
                parse_iso(data.get("addedAt") or data.get("createdAt")) or utcnow()
            ),
        )


@dataclass(slots=True)
class PlaybackRequest:
    anime: Anime
    title: str
    episode: str | None = None
    history_id: str | None = None
    source_id: str | None = None
    resume: bool = False
    dub: bool = False
    return_route: str = "dashboard"


def titles_match(first: Anime | AnimeTitle, second: Anime | AnimeTitle) -> bool:
    first_titles = first.title.normalized() if isinstance(first, Anime) else first.normalized()
    second_titles = second.title.normalized() if isinstance(second, Anime) else second.normalized()
    return bool(first_titles & second_titles)
