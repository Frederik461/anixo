"""AniList GraphQL client with SQLite-backed fresh and stale caching."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from config import Config
from data.database import Database
from models.anime import Anime, AnimeTitle, normalize_title

_MEDIA_FIELDS = """
  id
  title { romaji english native userPreferred }
  description(asHtml: false)
  coverImage { extraLarge large color }
  bannerImage
  format
  episodes
  averageScore
  popularity
  status
  season
  seasonYear
  startDate { year }
  genres
  studios { nodes { name } }
  isAdult
"""

_SEARCH_QUERY = f"""
query Search($search: String, $page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(search: $search, type: ANIME, sort: SEARCH_MATCH, isAdult: false) {{{_MEDIA_FIELDS}}}
  }}
}}
"""

_TRENDING_QUERY = f"""
query Trending($page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(sort: TRENDING_DESC, type: ANIME, isAdult: false) {{{_MEDIA_FIELDS}}}
  }}
}}
"""

_BROWSE_QUERY = f"""
query Browse(
  $page: Int
  $perPage: Int
  $sort: [MediaSort]
  $genre_in: [String]
  $status: MediaStatus
  $format: MediaFormat
) {{
  Page(page: $page, perPage: $perPage) {{
    media(
      type: ANIME
      isAdult: false
      sort: $sort
      genre_in: $genre_in
      status: $status
      format: $format
    ) {{{_MEDIA_FIELDS}}}
  }}
}}
"""

_DETAILS_QUERY = f"""
query Details($id: Int) {{
  Media(id: $id, type: ANIME) {{{_MEDIA_FIELDS}}}
}}
"""

_BROWSE_CATEGORIES: dict[str, dict[str, str | None]] = {
    "Trending": {"sort": "TRENDING_DESC"},
    "Popular": {"sort": "POPULARITY_DESC"},
    "Airing": {"sort": "POPULARITY_DESC", "status": "RELEASING"},
    "Movies": {"sort": "POPULARITY_DESC", "format": "MOVIE"},
    "Recent": {"sort": "START_DATE_DESC"},
}

_BROWSE_SORTS: dict[str, str | None] = {
    "Default": None,
    "Trending": "TRENDING_DESC",
    "Score": "SCORE_DESC",
    "A–Z": "TITLE_ROMAJI",
    "Latest": "START_DATE_DESC",
}

_USER_AGENT = "ANIXO/0.1.0 (Windows Textual TUI)"


class AnimeAPI:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def search(
        self,
        query: str,
        page: int = 1,
        per_page: int | None = None,
    ) -> list[Anime]:
        per_page = per_page or 24
        key = Database._metadata_key("search", page, per_page, query)
        payload = await self._request(
            key,
            _SEARCH_QUERY,
            {"search": query, "page": page, "perPage": per_page},
            query_kind="search",
            page=page,
            per_page=per_page,
            search_text=query,
        )
        return self._map_page(payload)

    async def trending(
        self,
        page: int = 1,
        per_page: int | None = None,
    ) -> list[Anime]:
        per_page = per_page or 18
        key = Database._metadata_key("trending", page, per_page, None)
        payload = await self._request(
            key,
            _TRENDING_QUERY,
            {"page": page, "perPage": per_page},
            query_kind="trending",
            page=page,
            per_page=per_page,
        )
        return self._map_page(payload)

    async def browse(
        self,
        *,
        category: str = "Trending",
        genre: str = "All",
        sort: str = "Default",
        page: int = 1,
        per_page: int | None = None,
    ) -> list[Anime]:
        """Browse AniList with category, genre, and sort filters applied."""
        per_page = per_page or 48
        category = category if category in _BROWSE_CATEGORIES else "Trending"
        sort = sort if sort in _BROWSE_SORTS else "Default"
        genre = genre if genre and genre != "All" else "All"

        # Keep the common entry point on the existing trending cache. This also
        # preserves the established offline fallback and test doubles.
        if category == "Trending" and genre == "All" and sort == "Default":
            return await self.trending(page=page, per_page=per_page)

        config = _BROWSE_CATEGORIES[category]
        selected_sort = _BROWSE_SORTS[sort] or config.get("sort")
        # AniList treats an explicit null filter as a filter for null values,
        # so only include variables that actually constrain the query.
        variables: dict[str, Any] = {"page": page, "perPage": per_page}
        if selected_sort:
            variables["sort"] = [selected_sort]
        if genre != "All":
            variables["genre_in"] = [genre]
        for name in ("status", "format"):
            value = config.get(name)
            if value:
                variables[name] = value
        key = Database._browse_key(category, genre, sort, page, per_page)
        payload = await self._request(
            key,
            _BROWSE_QUERY,
            variables,
            query_kind="browse",
            page=page,
            per_page=per_page,
            search_text=f"{category}|{genre}|{sort}",
        )
        return self._map_page(payload)

    async def details(self, anilist_id: int) -> Anime | None:
        key = f"anilist:details:{anilist_id}"
        payload = await self._request(key, _DETAILS_QUERY, {"id": anilist_id})
        data = payload or {}
        # Older cache rows may still contain the original GraphQL envelope.
        if isinstance(data.get("data"), Mapping):
            data = data["data"]
        media = data.get("Media")
        return self._map_media(media) if isinstance(media, dict) else None

    def enrich_from_cache(self, query: str, anime: Anime) -> Anime:
        """Attach cached cover metadata to a title-only history record."""
        key = Database._metadata_key("search", 1, 24, query)
        payload = self.database.get_stale_cache(key)
        for item in self._map_page(payload):
            if anime.title.normalized() & item.title.normalized():
                return self._merge(anime, item)
        return anime

    # ------------------------------------------------------------------
    # Internals

    async def _request(
        self,
        key: str,
        query: str,
        variables: dict[str, Any],
        *,
        query_kind: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
        search_text: str | None = None,
    ) -> dict[str, Any] | None:
        cached = self.database.get_cache(key, self.config.metadata_ttl_hours)
        if cached is not None:
            return cached

        async with self._request_lock:
            loop = asyncio.get_running_loop()
            wait = 0.35 - (loop.time() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            payload = await self._post(query, variables)
            self._last_request = loop.time()

        if payload is None:
            return self.database.get_stale_cache(key)
        self.database.set_cache(
            key,
            payload,
            self.config.metadata_ttl_hours,
            query_kind=query_kind,
            page=page,
            per_page=per_page,
            search_text=search_text,
        )
        return payload

    async def _post(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any] | None:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                    response = await client.post(
                        self.config.metadata_endpoint,
                        json={"query": query, "variables": variables},
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "User-Agent": _USER_AGENT,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            if not isinstance(payload, dict):
                return None
            errors = payload.get("errors") or []
            messages = " ".join(str(item.get("message", "")) for item in errors if isinstance(item, dict))
            if errors and "extraLarge" in messages and attempt == 0:
                query = query.replace("extraLarge", "large")
                continue
            if errors:
                return None
            # AniList wraps successful GraphQL results in ``data``. Normalize
            # here so the cache and every caller see one consistent shape.
            data = payload.get("data")
            return data if isinstance(data, Mapping) else payload
        return None

    @staticmethod
    def _map_page(payload: dict[str, Any] | None) -> list[Anime]:
        if not payload:
            return []
        # Accept both the normalized cache shape and legacy wrapped rows.
        if isinstance(payload.get("data"), Mapping):
            payload = payload["data"]
        media: Any = None
        page = payload.get("Page")
        if isinstance(page, Mapping):
            media = page.get("media")
        if media is None and isinstance(payload.get("items"), list):
            media = payload["items"]
        if not isinstance(media, list):
            return []
        return [AnimeAPI._map_media(item) for item in media if isinstance(item, dict)]

    @staticmethod
    def _map_media(item: dict[str, Any]) -> Anime:
        title_data = item.get("title") or {}
        cover = item.get("coverImage") or {}
        cover_url = cover.get("extraLarge") or cover.get("large") or item.get("coverUrl")
        title = AnimeTitle(
            english=title_data.get("english"),
            romaji=title_data.get("romaji") or title_data.get("userPreferred"),
            native=title_data.get("native"),
        )
        anime_id = str(item["id"]) if item.get("id") is not None else normalize_title(title.primary())
        return Anime.from_mapping(
            {
                **item,
                "id": anime_id,
                "title": title,
                "coverUrl": cover_url,
                "coverColor": cover.get("color") or item.get("coverColor"),
                "anilistId": item.get("id"),
            }
        )

    @staticmethod
    def _merge(base: Anime, enriched: Anime) -> Anime:
        base.cover_url = base.cover_url or enriched.cover_url
        base.cover_color = base.cover_color or enriched.cover_color
        base.banner_url = base.banner_url or enriched.banner_url
        base.description = base.description or enriched.description
        base.format = base.format or enriched.format
        base.episodes = base.episodes or enriched.episodes
        base.season = base.season or enriched.season
        base.season_year = base.season_year or enriched.season_year
        base.average_score = base.average_score or enriched.average_score
        base.popularity = base.popularity or enriched.popularity
        base.status = base.status or enriched.status
        base.genres = base.genres or enriched.genres
        base.studios = base.studios or enriched.studios
        base.anilist_id = base.anilist_id or enriched.anilist_id
        return base
