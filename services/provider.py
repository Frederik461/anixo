"""Read-only provider adapter used by ANIXO's own selectors.

ani-cli exposes only ``id<TAB>title`` search rows and ``episode-id<TAB>number``
episode rows, and its CLI cannot accept a chosen result ID.  ANIXO therefore
reads those two provider endpoints itself so the season/movie/special and
episode pickers are entirely owned by ANIXO.  Playback still delegates to a
checksum-verified ani-cli copy that is patched to honor the exact provider ID.

No metadata is invented: every title, format, episode number, and episode title
comes from the provider response.  Provider ordering is preserved.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

_DEFAULT_BASE_URL = "https://hianime.at"
_KNOWN_FORMATS = {
    "TV",
    "MOVIE",
    "SPECIAL",
    "SPECIALS",
    "OVA",
    "ONA",
    "OAV",
}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    """Raised when the provider response cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ProviderAnime:
    """One exact provider search result."""

    source_id: str
    title: str
    numeric_id: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def format(self) -> str | None:
        return self.metadata.get("format")

    @property
    def episode_count(self) -> str | None:
        return self.metadata.get("episode_count")

    @property
    def description(self) -> str | None:
        return self.metadata.get("description")

    @property
    def poster_url(self) -> str | None:
        return self.metadata.get("poster_url")


@dataclass(frozen=True, slots=True)
class ProviderEpisode:
    """One exact episode of one exact provider result."""

    number: str
    provider_episode_id: str
    title: str | None = None


def _unescape(value: str) -> str:
    return html.unescape(value).strip()


_MIRURO_NUMBER_RE = re.compile(
    r"(?:[,{;]number:|\.number=)([0-9]+(?:\.[0-9]+)?)"
)


def _js_string(value: str) -> str:
    decoded = (
        value.replace('\\"', '"')
        .replace("\\n", " ")
        .replace("\\r", " ")
        .replace("\\t", " ")
    )
    return _unescape(decoded)


def _js_field(block: str, name: str) -> str:
    match = re.search(
        rf'\b{re.escape(name)}:"((?:\\.|[^"\\])*)"', block, re.DOTALL
    )
    return _js_string(match.group(1)) if match else ""


def _detect_provider_kind(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    return "miruro" if "miruro" in host else "hianime"


def _miruro_site_id(source_id: str) -> str:
    parts = str(source_id or "").strip().split(":")
    if len(parts) >= 2 and parts[0].isdigit():
        candidate = parts[1]
    else:
        candidate = parts[0]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]+", candidate) else ""


def parse_miruro_search_page(page: str) -> list[ProviderAnime]:
    """Parse Miruro's Next.js search payload into exact composite result IDs."""
    marker = re.search(r"items:\[(.*?)\],\s*entityItems:", page, re.IGNORECASE | re.DOTALL)
    if not marker:
        return []
    body = marker.group(1).replace('},{id:"', '}\n{id:"')
    results: list[ProviderAnime] = []
    for block in body.splitlines():
        if '{id:"' not in block:
            continue
        site_id = _js_field(block, "id")
        anilist = re.search(r'\banilist:\["([^"]+)"\]', block)
        if not site_id or not anilist:
            continue
        al_id = str(anilist.group(1)).strip()
        if not al_id.isdigit():
            continue
        title_block = re.search(r"title:\{(.*?)\}", block, re.DOTALL)
        title = ""
        if title_block:
            title = (
                _js_field(title_block.group(1), "english")
                or _js_field(title_block.group(1), "romaji")
                or _js_field(title_block.group(1), "native")
            )
        if not title:
            continue
        metadata: dict[str, str] = {}
        media_format = _js_field(block, "format").upper()
        if media_format in _KNOWN_FORMATS:
            metadata["format"] = media_format
        counts = re.search(r"episode_counts:\{([^}]*)\}", block)
        count = ""
        if counts:
            for key in ("raw", "sub", "dub"):
                found = re.search(rf"\b{key}:(\d+)", counts.group(1))
                if found:
                    count = found.group(1)
                    break
        if not count:
            found = re.search(r"\bepisode_count:(\d+)", block)
            count = found.group(1) if found else ""
        if count:
            metadata["episode_count"] = count
        description = _js_field(block, "description")
        if description:
            metadata["description"] = description.rstrip(".")
        poster = _js_field(block, "cover_url")
        if poster:
            metadata["poster_url"] = poster
        results.append(
            ProviderAnime(
                source_id=f"{al_id}:{site_id}:",
                title=title,
                numeric_id=al_id,
                metadata=metadata,
            )
        )
    return results


def parse_miruro_episode_page(payload: Any, site_id: str) -> list[ProviderEpisode]:
    """Parse Miruro's watch page into exact episode numbers from the provider."""
    document = str(payload.get("html") or "") if isinstance(payload, dict) else str(payload or "")
    numbers: list[str] = []
    seen: set[str] = set()
    for value in _MIRURO_NUMBER_RE.findall(document):
        if value in seen:
            continue
        seen.add(value)
        numbers.append(value)
    numbers.sort(key=lambda value: float(value))
    return [
        ProviderEpisode(number=value, provider_episode_id=f"{site_id}:{value}")
        for value in numbers
    ]


def parse_search_page(page: str) -> list[ProviderAnime]:
    """Parse provider search HTML into ordered, exact result rows."""
    # The sidebar repeats result markup; ani-cli cuts it off for the same reason.
    page = page.split('id="main-sidebar"', 1)[0]
    results: list[ProviderAnime] = []
    for block in re.split(r'<div class="flw-item', page)[1:]:
        match = re.search(
            r'<h3 class="film-name">\s*<a\s+href="([^"]+)"\s+title="([^"]+)"',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        href = _unescape(match.group(1))
        source_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not source_id:
            continue
        numeric = re.search(r"(\d+)$", source_id)
        if not numeric:
            continue

        items = [
            _unescape(value)
            for value in re.findall(
                r'<span class="fdi-item[^"]*">(.*?)</span>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        media_format = next(
            (item for item in items if item.upper() in _KNOWN_FORMATS), None
        )
        duration = next(
            (item for item in items if re.fullmatch(r"\d+m", item, re.IGNORECASE)),
            None,
        )
        count = re.search(r'class="tick-item tick-eps"[^>]*>([^<]+)<', block)
        description = re.search(
            r'<div class="description">(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        poster = re.search(
            r'<img\s+src="([^"]+)"[^>]*class="film-poster-img"',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        metadata: dict[str, str] = {}
        if media_format:
            metadata["format"] = media_format.upper()
        if duration:
            metadata["duration"] = duration
        if count:
            metadata["episode_count"] = _unescape(count.group(1))
        if description:
            text = _unescape(re.sub(r"<[^>]+>", "", description.group(1)))
            if text:
                metadata["description"] = text.rstrip(".")
        if poster:
            metadata["poster_url"] = _unescape(poster.group(1))
        results.append(
            ProviderAnime(
                source_id=source_id,
                title=_unescape(match.group(2)),
                numeric_id=numeric.group(1),
                metadata=metadata,
            )
        )
    return results


def parse_episode_page(payload: Any, source_id: str) -> list[ProviderEpisode]:
    """Parse episode HTML for one exact ``source_id``."""
    if isinstance(payload, dict):
        document = str(payload.get("html") or "")
    else:
        document = str(payload or "")
    episodes: list[ProviderEpisode] = []
    for chunk in re.split(r'(?=<a\s[^>]*class="[^"]*\bep-item\b)', document)[1:]:
        anchor = chunk.split("</a>", 1)[0]
        number = re.search(r'\bdata-number="([^"]+)"', anchor)
        episode_id = re.search(r'\bdata-id="(\d+)"', anchor)
        href = re.search(r'\bhref="([^"]+)"', anchor)
        if not number or not episode_id or not href:
            continue
        # Never accept an episode belonging to a different provider result.
        if f"/watch/{source_id}?ep=" not in _unescape(href.group(1)):
            continue
        title = re.search(r'\btitle="([^"]*)"', anchor)
        episodes.append(
            ProviderEpisode(
                number=_unescape(number.group(1)),
                provider_episode_id=episode_id.group(1),
                title=_unescape(title.group(1)) if title else None,
            )
        )
    return episodes


class ProviderService:
    """Fetch search results and episodes from the configured provider."""

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        kind: str | None = None,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._client = client
        requested = str(kind or "").strip().lower()
        self.kind = (
            requested
            if requested in {"miruro", "hianime"}
            else _detect_provider_kind(self.base_url)
        )

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _get(self, path: str) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            if self._client is not None:
                response = self._client.get(
                    url, headers=self._headers(), timeout=15.0, follow_redirects=True
                )
            else:
                with httpx.Client(
                    timeout=15.0,
                    follow_redirects=True,
                    headers=self._headers(),
                ) as client:
                    response = client.get(url)
        except httpx.HTTPError as error:
            raise ProviderError(f"Provider request failed: {error}") from error
        if response.status_code >= 400:
            raise ProviderError(f"Provider returned HTTP {response.status_code}.")
        if "just a moment" in response.text[:500].lower():
            raise ProviderError("The provider is protected by a challenge page.")
        return response

    def search(self, query: str) -> list[ProviderAnime]:
        """Return exact provider rows in provider order."""
        clean = (query or "").strip()
        if not clean:
            return []
        if self.kind == "miruro":
            response = self._get(f"/search?q={quote(clean)}")
            return parse_miruro_search_page(response.text)
        response = self._get(f"/search?keyword={quote(clean)}")
        return parse_search_page(response.text)

    def episodes(self, source_id: str) -> list[ProviderEpisode]:
        """Return episodes for one exact provider result ID."""
        if self.kind == "miruro":
            site_id = _miruro_site_id(source_id)
            if not site_id:
                raise ProviderError(f"Invalid provider ID: {source_id}")
            response = self._get(f"/watch/{quote(site_id)}/x?ep=1")
            return parse_miruro_episode_page(response.text, site_id)
        numeric = re.search(r"(\d+)$", source_id)
        if not numeric:
            raise ProviderError(f"Invalid provider ID: {source_id}")
        response = self._get(f"/api/theme/episode/list/{numeric.group(1)}")
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError("The provider returned an invalid episode response.") from error
        return parse_episode_page(payload, source_id)
