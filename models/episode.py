"""Episode identity, state, and browser helpers.

The current metadata source (AniList) only exposes a series-level episode
count.  Episode titles, descriptions, and durations are therefore optional:
they are shown when a caller supplies source-provided metadata and omitted
otherwise.  Nothing in this module invents episode data.

All functions here are pure; persistence lives in ``data.database`` and UI
composition lives in the screens/components packages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from models.anime import Anime

UNWATCHED = "unwatched"
WATCHING = "watching"
COMPLETED = "completed"

_STATE_LABELS = {
    UNWATCHED: "Unwatched",
    WATCHING: "Watching",
    COMPLETED: "Watched",
}

_NUMBER = re.compile(r"(\d+)")


def episode_number(value: str | int | None) -> int | None:
    """Return the first integer found in an episode label, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    match = _NUMBER.search(str(value))
    return int(match.group(1)) if match else None


def normalize_state(value: str | None) -> str:
    """Clamp persisted state strings to the three supported states."""
    return value if value in (WATCHING, COMPLETED) else UNWATCHED


def released_episode_limit(
    anime: Anime,
    *,
    states: Mapping[str, str] | None = None,
    progress: Mapping[str, Mapping[str, Any]] | None = None,
    last_episode: str | None = None,
) -> int | None:
    """Highest episode number that may be *inferred* from metadata alone.

    A provider episode list is always authoritative and is never capped here.
    This cap only applies when ANIXO would otherwise generate ``1..episodes``
    from AniList's planned season total, which includes unaired episodes.

    ``None`` means no restriction is known; ``0`` means a releasing series with
    no known airing progress, so only locally known episodes should be shown.
    """
    total = anime.episodes if anime.episodes and anime.episodes > 0 else None
    next_number = anime.next_airing_episode
    if next_number is not None and next_number > 0:
        # AniList reports the number of the *next* episode to air, so every
        # episode from that number onward is still unreleased.
        limit = next_number - 1
    elif (anime.status or "").upper() == "RELEASING":
        limit = 0
    else:
        return total

    known = {
        number
        for value in (*(states or {}), *(progress or {}), last_episode)
        if (number := episode_number(value)) is not None
    }
    if known:
        limit = max(limit, max(known))
    return min(limit, total) if total is not None else limit


def state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[UNWATCHED])


@dataclass(slots=True)
class Episode:
    """One selectable episode.

    ``number`` is the value handed to ani-cli's ``-e`` flag.  ``ordinal`` is
    the position in the source-provided ordering and is used for navigation,
    so non-numeric labels (OVAs, specials) still have stable previous/next
    behaviour.
    """

    number: str
    ordinal: int
    title: str | None = None
    description: str | None = None
    duration: str | None = None
    group: str | None = None
    state: str = UNWATCHED
    progress_seconds: float | None = None
    duration_seconds: float | None = None

    @property
    def key(self) -> str:
        return self.number

    @property
    def numeric(self) -> int | None:
        return episode_number(self.number)

    @property
    def display_title(self) -> str:
        return self.title or f"Episode {self.number}"

    @property
    def status_label(self) -> str:
        return state_label(self.state)

    @property
    def is_completed(self) -> bool:
        return self.state == COMPLETED

    @property
    def is_watching(self) -> bool:
        return self.state == WATCHING

    @property
    def progress_text(self) -> str | None:
        if self.progress_seconds is None or not self.duration_seconds:
            return None
        percent = max(
            0, min(100, round(self.progress_seconds / self.duration_seconds * 100))
        )
        return f"{percent}% watched"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_episodes(
    anime: Anime,
    *,
    entries: Sequence[Mapping[str, Any]] | None = None,
    states: Mapping[str, str] | None = None,
    progress: Mapping[str, Mapping[str, Any]] | None = None,
    last_episode: str | None = None,
) -> list[Episode]:
    """Build an ordered episode list from available data only.

    ``entries`` is optional provider-reported episode data and is always
    trusted as the exact list of released episodes.  When it is absent the list
    is derived from ``Anime.episodes``, capped at the last aired episode for
    releasing series; unaired episodes are never generated.
    """
    states = states or {}
    progress = progress or {}
    episodes: list[Episode] = []

    if entries:
        for index, item in enumerate(entries):
            number = _text(item.get("number") or item.get("episode")) or str(index + 1)
            episodes.append(
                Episode(
                    number=number,
                    ordinal=index,
                    title=_text(item.get("title")),
                    description=_text(item.get("description") or item.get("synopsis")),
                    duration=_text(item.get("duration")),
                    group=_text(item.get("group") or item.get("season")),
                )
            )
    elif anime.episodes and anime.episodes > 0:
        limit = released_episode_limit(
            anime,
            states=states,
            progress=progress,
            last_episode=last_episode,
        )
        count = anime.episodes if limit is None else min(anime.episodes, limit)
        for index in range(count):
            episodes.append(Episode(number=str(index + 1), ordinal=index))

    last_number = episode_number(last_episode)
    for episode in episodes:
        record = progress.get(episode.number) or {}
        seconds = record.get("progress_seconds")
        total = record.get("duration_seconds")
        episode.progress_seconds = float(seconds) if seconds is not None else None
        episode.duration_seconds = float(total) if total is not None else None
        explicit = states.get(episode.number)
        if explicit is not None:
            episode.state = normalize_state(explicit)
            continue
        number = episode_number(episode.number)
        if last_number is None or number is None:
            episode.state = UNWATCHED
        elif number < last_number:
            episode.state = COMPLETED
        elif number == last_number:
            episode.state = WATCHING
        else:
            episode.state = UNWATCHED
    return episodes


def find_episode(
    episodes: Sequence[Episode], number: str | int | None
) -> Episode | None:
    """Find an episode by exact label, falling back to numeric equality."""
    if number is None:
        return None
    target = str(number).strip()
    for episode in episodes:
        if episode.number == target:
            return episode
    target_number = episode_number(target)
    if target_number is None:
        return None
    return next(
        (episode for episode in episodes if episode_number(episode.number) == target_number),
        None,
    )


def grouped_episodes(
    episodes: Sequence[Episode],
) -> list[tuple[str | None, list[Episode]]]:
    """Group consecutive episodes by source-provided group label.

    Episodes without a group stay in a ``None`` group.  Callers should only
    render headers when at least one episode actually has a group.
    """
    groups: list[tuple[str | None, list[Episode]]] = []
    for episode in episodes:
        label = episode.group
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(episode)
    return groups


def has_groups(episodes: Sequence[Episode]) -> bool:
    return any(episode.group for episode in episodes)


def is_series_completed(
    anime: Anime,
    last_episode: str | None,
    states: Mapping[str, str] | None = None,
) -> bool:
    """Return True only when the final episode has been explicitly completed.

    Playing the final episode does not complete a series on its own: the
    user must mark it watched (or a reliable progress signal must do so).
    """
    if not anime.episodes or anime.episodes <= 0:
        return False
    states = states or {}
    last = episode_number(last_episode)
    if last is None or last < anime.episodes:
        return False
    final = str(anime.episodes)
    if states.get(final) == COMPLETED:
        return True
    return bool(last_episode) and states.get(str(last_episode)) == COMPLETED
