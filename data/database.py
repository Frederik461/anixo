"""SQLite persistence and one-time legacy JSON migration."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Row, connect
from typing import Any, Iterable, Mapping

from models.anime import (
    Anime,
    AnimeTitle,
    HistoryEntry,
    WatchlistEntry,
    canonical_id,
    is_same_anime,
    normalize_title,
    parse_iso,
    utcnow,
)
from models.episode import COMPLETED, UNWATCHED, WATCHING, episode_number

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id TEXT PRIMARY KEY,
    anime_id TEXT NOT NULL,
    episode TEXT,
    last_watched TEXT NOT NULL,
    updated_at TEXT,
    source TEXT,
    ani_cli_source_id TEXT,
    progress_seconds REAL,
    duration_seconds REAL,
    title_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS history_updated_at ON history(updated_at DESC);
CREATE INDEX IF NOT EXISTS history_source ON history(source);
CREATE INDEX IF NOT EXISTS history_ani_cli_source ON history(ani_cli_source_id);

CREATE TABLE IF NOT EXISTS episode_progress (
    anime_id TEXT NOT NULL,
    episode TEXT NOT NULL,
    state TEXT NOT NULL,
    progress_seconds REAL,
    duration_seconds REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (anime_id, episode)
);

CREATE INDEX IF NOT EXISTS episode_progress_anime ON episode_progress(anime_id);

CREATE TABLE IF NOT EXISTS watchlist (
    anime_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    normalized_title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ignored_ani_cli_source_id (
    source_id TEXT PRIMARY KEY,
    removed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    endpoint_hash TEXT,
    query_kind TEXT,
    page INTEGER,
    per_page INTEGER,
    search_text TEXT
);

CREATE TABLE IF NOT EXISTS migration_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str) -> Any:
    return json.loads(value)


class Database:
    """Small, explicit SQLite store with no ORM and no hidden magic."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[Connection] = []
        self._connections_lock = threading.Lock()
        connection = self.connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        connection.commit()
        self.last_migration_counts: dict[str, int] = {}

    @property
    def connection(self) -> Connection:
        """A connection owned by the calling thread.

        Several services call the database with ``asyncio.to_thread``.  Sharing
        one ``sqlite3.Connection`` across those threads raises ``InterfaceError``
        under concurrent access; WAL lets each thread use its own connection
        against the same file instead.
        """
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = connect(self.path, check_same_thread=False)
            connection.row_factory = Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=NORMAL")
            with self._connections_lock:
                self._connections.append(connection)
            self._local.connection = connection
        return connection

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()

    def replace_with(self, source: Path) -> None:
        """Replace this database with a migrated copy, then reopen it.

        Used only by the first-launch migration path. All connections are
        closed first so no WAL writer can race the file replacement.
        """
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        self.close()
        temporary = self.path.with_name(f"{self.path.name}.migrate.tmp")
        shutil.copy2(source, temporary)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
        temporary.replace(self.path)
        self._local = threading.local()
        self._connections = []
        connection = self.connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        connection.commit()

    # ------------------------------------------------------------------
    # Anime records

    def upsert_anime(self, anime: Anime, *, commit: bool = True) -> None:
        anime.updated_at = utcnow()
        self.connection.execute(
            """
            INSERT INTO anime(id, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload,
                                          updated_at = excluded.updated_at
            """,
            (anime.id, _dumps(anime.to_mapping()), anime.updated_at.isoformat()),
        )
        if commit:
            self.connection.commit()

    def get_anime(self, anime_id: str) -> Anime | None:
        row = self.connection.execute(
            "SELECT payload FROM anime WHERE id = ?", (anime_id,)
        ).fetchone()
        return Anime.from_mapping(_loads(row["payload"])) if row else None

    def find_anime_by_title(self, title: str) -> Anime | None:
        normalized = normalize_title(title)
        if not normalized:
            return None
        for row in self.connection.execute("SELECT payload FROM anime"):
            anime = Anime.from_mapping(_loads(row["payload"]))
            if normalized in anime.title.normalized():
                return anime
        return None

    def find_canonical_anime(self, source_id: str) -> Anime | None:
        """Find an anime by provider source ID without guessing by title."""
        for row in self.connection.execute("SELECT payload FROM anime"):
            anime = Anime.from_mapping(_loads(row["payload"]))
            if canonical_id(anime) == source_id:
                return anime
        return None

    def all_cover_urls(self) -> set[str]:
        urls: set[str] = set()
        for row in self.connection.execute("SELECT payload FROM anime"):
            anime = Anime.from_mapping(_loads(row["payload"]))
            if anime.cover_url:
                urls.add(anime.cover_url)
        return urls

    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self._set_meta(key, value, commit=commit)

    def meta(self, key: str, default: str = "") -> str:
        return self._meta(key, default)

    # ------------------------------------------------------------------
    # History

    def list_history(self, limit: int = 200) -> list[HistoryEntry]:
        rows = self.connection.execute(
            """
            SELECT h.*, a.payload AS anime_payload
            FROM history h
            LEFT JOIN anime a ON a.id = h.anime_id
            ORDER BY COALESCE(h.updated_at, h.last_watched) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._history_entry_from_row(row) for row in rows]

    @staticmethod
    def _history_entry_from_row(row: Any) -> HistoryEntry:
        payload = row["anime_payload"]
        anime = (
            Anime.from_mapping(_loads(payload))
            if payload
            else Anime(
                id=row["anime_id"],
                title=AnimeTitle(romaji=row["title_snapshot"] or "Unknown anime"),
                source_id=row["ani_cli_source_id"],
            )
        )
        return HistoryEntry(
            id=row["id"],
            anime=anime,
            episode=row["episode"],
            last_watched=parse_iso(row["last_watched"]),
            watched_at=parse_iso(row["last_watched"]),
            updated_at=parse_iso(row["updated_at"]),
            source=row["source"],
            source_id=row["ani_cli_source_id"],
            progress_seconds=row["progress_seconds"],
            duration_seconds=row["duration_seconds"],
            title_snapshot=row["title_snapshot"],
        )

    def get_history_entry(self, entry_id: str) -> HistoryEntry | None:
        row = self.connection.execute(
            """
            SELECT h.*, a.payload AS anime_payload
            FROM history h
            LEFT JOIN anime a ON a.id = h.anime_id
            WHERE h.id = ?
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
        return self._history_entry_from_row(row) if row else None

    def find_history_for_anime(self, anime: Anime) -> HistoryEntry | None:
        """Find the history row that belongs to an anime.

        Canonical IDs win; provider source IDs and finally the snapshot title
        are used as conservative fallbacks.
        """
        normalized = normalize_title(anime.display_title())
        fast_row = self.connection.execute(
            """
            SELECT h.*, a.payload AS anime_payload
            FROM history h
            LEFT JOIN anime a ON a.id = h.anime_id
            WHERE h.anime_id = ? OR (? != '' AND h.ani_cli_source_id = ?)
            ORDER BY COALESCE(h.updated_at, h.last_watched) DESC
            LIMIT 1
            """,
            (anime.id, anime.source_id or "", anime.source_id or ""),
        ).fetchone()
        if fast_row is not None:
            return self._history_entry_from_row(fast_row)
        for entry in self.list_history(limit=1000):
            if entry.anime.id == anime.id:
                return entry
            if anime.source_id and entry.source_id == anime.source_id:
                return entry
            if (
                anime.source_id
                and entry.anime.source_id
                and entry.anime.source_id == anime.source_id
            ):
                return entry
            if normalized and normalize_title(entry.title_snapshot or "") == normalized:
                return entry
        return None

    def update_history_episode(
        self,
        entry_id: str,
        episode: str | None,
        *,
        clear_progress: bool = False,
    ) -> bool:
        """Update only the provider episode, preserving title and artwork."""
        now = utcnow().isoformat()
        if clear_progress:
            cursor = self.connection.execute(
                """
                UPDATE history
                SET episode = ?, progress_seconds = NULL,
                    duration_seconds = NULL, updated_at = ?
                WHERE id = ?
                """,
                (episode, now, entry_id),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE history SET episode = ?, updated_at = ? WHERE id = ?",
                (episode, now, entry_id),
            )
        self.connection.commit()
        return cursor.rowcount > 0

    def record_history(
        self,
        anime: Anime,
        episode: str | None,
        *,
        entry_id: str | None = None,
        source_id: str | None = None,
        last_watched: datetime | None = None,
        updated_at: datetime | None = None,
        source: str | None = None,
        progress_seconds: float | None = None,
        duration_seconds: float | None = None,
        commit: bool = True,
    ) -> HistoryEntry:
        self.upsert_anime(anime, commit=False)
        key = entry_id or source_id or canonical_id(anime)
        watched = last_watched or utcnow()
        updated = updated_at or watched
        resolved_source_id = source_id or anime.source_id
        if source == "ani-cli" and not resolved_source_id and ":" in key:
            resolved_source_id = key.split(":", 1)[1]
        self.connection.execute(
            """
            INSERT INTO history(
                id, anime_id, episode, last_watched, updated_at, source,
                ani_cli_source_id, progress_seconds, duration_seconds, title_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                anime_id = excluded.anime_id,
                episode = excluded.episode,
                last_watched = excluded.last_watched,
                updated_at = excluded.updated_at,
                source = excluded.source,
                ani_cli_source_id = excluded.ani_cli_source_id,
                progress_seconds = excluded.progress_seconds,
                duration_seconds = excluded.duration_seconds,
                title_snapshot = excluded.title_snapshot
            """,
            (
                key,
                anime.id,
                str(episode) if episode is not None else None,
                watched.isoformat(),
                updated.isoformat(),
                source or anime.source,
                resolved_source_id,
                progress_seconds,
                duration_seconds,
                anime.display_title(),
            ),
        )
        if commit:
            self.connection.commit()
        return HistoryEntry(
            id=key,
            anime=anime,
            episode=str(episode) if episode is not None else None,
            last_watched=watched,
            watched_at=watched,
            updated_at=updated,
            source=source or anime.source,
            source_id=resolved_source_id,
            progress_seconds=progress_seconds,
            duration_seconds=duration_seconds,
            title_snapshot=anime.display_title(),
        )

    def remove_history(self, entry_id: str) -> bool:
        row = self.connection.execute(
            "SELECT source, ani_cli_source_id, anime_id FROM history WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if not row:
            return False
        if row["source"] == "ani-cli" and row["ani_cli_source_id"]:
            self.connection.execute(
                "INSERT OR REPLACE INTO ignored_ani_cli_source_id(source_id, removed_at) "
                "VALUES (?, ?)",
                (row["ani_cli_source_id"], utcnow().isoformat()),
            )
        self.connection.execute("DELETE FROM history WHERE id = ?", (entry_id,))
        self.connection.execute(
            "DELETE FROM episode_progress WHERE anime_id = ?", (row["anime_id"],)
        )
        self.connection.commit()
        return True

    def clear_history(self) -> int:
        rows = self.connection.execute(
            "SELECT source, ani_cli_source_id FROM history"
        ).fetchall()
        for row in rows:
            if row["source"] == "ani-cli" and row["ani_cli_source_id"]:
                self.connection.execute(
                    "INSERT OR REPLACE INTO ignored_ani_cli_source_id(source_id, removed_at) "
                    "VALUES (?, ?)",
                    (row["ani_cli_source_id"], utcnow().isoformat()),
                )
        removed = self.connection.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        self.connection.execute("DELETE FROM history")
        self.connection.execute("DELETE FROM episode_progress")
        self.connection.commit()
        return int(removed)

    def ignored_sources(self) -> set[str]:
        return {
            str(row["source_id"])
            for row in self.connection.execute("SELECT source_id FROM ignored_ani_cli_source_id")
        }

    # ------------------------------------------------------------------
    # Episode progress

    def set_episode_state(
        self,
        anime_id: str,
        episode: str,
        state: str,
        *,
        progress_seconds: float | None = None,
        duration_seconds: float | None = None,
        commit: bool = True,
    ) -> None:
        """Persist one episode's state, preserving known progress values."""
        self.connection.execute(
            """
            INSERT INTO episode_progress(
                anime_id, episode, state, progress_seconds, duration_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(anime_id, episode) DO UPDATE SET
                state = excluded.state,
                progress_seconds = COALESCE(
                    excluded.progress_seconds, episode_progress.progress_seconds
                ),
                duration_seconds = COALESCE(
                    excluded.duration_seconds, episode_progress.duration_seconds
                ),
                updated_at = excluded.updated_at
            """,
            (
                anime_id,
                str(episode),
                state,
                progress_seconds,
                duration_seconds,
                utcnow().isoformat(),
            ),
        )
        if commit:
            self.connection.commit()

    def get_episode_states(self, anime_id: str) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT episode, state FROM episode_progress WHERE anime_id = ?",
            (anime_id,),
        ).fetchall()
        return {str(row["episode"]): str(row["state"]) for row in rows}

    def get_episode_records(self, anime_id: str) -> dict[str, dict[str, Any]]:
        """Return persisted state and progress for every known episode."""
        rows = self.connection.execute(
            "SELECT episode, state, progress_seconds, duration_seconds "
            "FROM episode_progress WHERE anime_id = ?",
            (anime_id,),
        ).fetchall()
        return {
            str(row["episode"]): {
                "state": str(row["state"]),
                "progress_seconds": row["progress_seconds"],
                "duration_seconds": row["duration_seconds"],
            }
            for row in rows
        }

    def mark_episode_completed(
        self, anime_id: str, episode: str, *, commit: bool = True
    ) -> None:
        self.set_episode_state(anime_id, episode, COMPLETED, commit=commit)

    def mark_episode_unwatched(
        self, anime_id: str, episode: str, *, commit: bool = True
    ) -> None:
        """Persist an explicit unwatched state and clear its saved progress."""
        self.connection.execute(
            """
            INSERT INTO episode_progress(
                anime_id, episode, state, progress_seconds, duration_seconds, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(anime_id, episode) DO UPDATE SET
                state = excluded.state,
                progress_seconds = NULL,
                duration_seconds = NULL,
                updated_at = excluded.updated_at
            """,
            (anime_id, str(episode), UNWATCHED, utcnow().isoformat()),
        )
        if commit:
            self.connection.commit()

    def mark_episode_watching(
        self,
        anime_id: str,
        episode: str,
        *,
        progress_seconds: float | None = None,
        duration_seconds: float | None = None,
        commit: bool = True,
    ) -> None:
        """Record launch of one episode without inventing completion for others.

        An explicitly completed episode is never downgraded when replayed.
        """
        self.connection.execute(
            """
            INSERT INTO episode_progress(
                anime_id, episode, state, progress_seconds, duration_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(anime_id, episode) DO UPDATE SET
                state = CASE
                    WHEN episode_progress.state = 'completed' THEN 'completed'
                    ELSE excluded.state
                END,
                progress_seconds = COALESCE(
                    excluded.progress_seconds, episode_progress.progress_seconds
                ),
                duration_seconds = COALESCE(
                    excluded.duration_seconds, episode_progress.duration_seconds
                ),
                updated_at = excluded.updated_at
            """,
            (
                anime_id,
                str(episode),
                WATCHING,
                progress_seconds,
                duration_seconds,
                utcnow().isoformat(),
            ),
        )
        if commit:
            self.connection.commit()

    def sync_episode_states(self, anime_id: str, last_episode: str | None) -> None:
        """Infer completed/watching states from a reliable last-watched value.

        Episodes before ``last_episode`` are completed and the last episode is
        watching.  Explicitly completed rows are never downgraded.
        """
        last = episode_number(last_episode)
        if last is None or last <= 0:
            return
        now = utcnow().isoformat()
        rows = [
            (anime_id, str(number), COMPLETED, now)
            for number in range(1, last)
        ]
        rows.append((anime_id, str(last), WATCHING, now))
        self.connection.executemany(
            """
            INSERT INTO episode_progress(anime_id, episode, state, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(anime_id, episode) DO UPDATE SET
                state = CASE
                    WHEN episode_progress.state = 'completed' THEN 'completed'
                    ELSE excluded.state
                END,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        self.connection.commit()

    def clear_episode_progress(self, anime_id: str) -> int:
        cursor = self.connection.execute(
            "DELETE FROM episode_progress WHERE anime_id = ?", (anime_id,)
        )
        self.connection.commit()
        return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Watchlist

    def add_watchlist(
        self,
        anime: Anime,
        created_at: datetime | None = None,
        *,
        commit: bool = True,
    ) -> bool:
        """Add an anime unless the same ID or primary title is already saved."""
        for entry in self.list_watchlist():
            if is_same_anime(entry.anime, anime):
                return False
        self.upsert_anime(anime, commit=False)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO watchlist(anime_id, created_at, normalized_title)
            VALUES (?, ?, ?)
            """,
            (
                anime.id,
                (created_at or utcnow()).isoformat(),
                normalize_title(anime.display_title()),
            ),
        )
        if commit:
            self.connection.commit()
        return True

    def remove_watchlist(self, anime_id: str) -> None:
        self.connection.execute("DELETE FROM watchlist WHERE anime_id = ?", (anime_id,))
        self.connection.commit()

    def remove_watchlist_anime(self, anime: Anime) -> bool:
        """Remove an equivalent watchlist entry, including title matches."""
        for entry in self.list_watchlist():
            if is_same_anime(entry.anime, anime):
                self.remove_watchlist(entry.anime.id)
                return True
        return False

    def is_watchlisted(self, anime_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM watchlist WHERE anime_id = ?", (anime_id,)
            ).fetchone()
            is not None
        )

    def watchlisted_ids(self) -> set[str]:
        """Return all watchlisted anime IDs in one query."""
        return {
            row[0]
            for row in self.connection.execute("SELECT anime_id FROM watchlist").fetchall()
        }

    def list_watchlist(self) -> list[WatchlistEntry]:
        rows = self.connection.execute(
            """
            SELECT w.created_at, a.payload
            FROM watchlist w
            LEFT JOIN anime a ON a.id = w.anime_id
            ORDER BY w.created_at DESC
            """
        ).fetchall()
        entries: list[WatchlistEntry] = []
        for row in rows:
            if row["payload"]:
                anime = Anime.from_mapping(_loads(row["payload"]))
            else:
                anime = Anime(
                    id=row["anime_id"], title=AnimeTitle(romaji="Unknown anime")
                )
            entries.append(
                WatchlistEntry(
                    anime=anime,
                    created_at=parse_iso(row["created_at"]) or utcnow(),
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Metadata cache

    def get_cache(self, key: str, ttl_hours: float = 24) -> Any | None:
        row = self.connection.execute(
            "SELECT payload, cached_at, expires_at FROM metadata_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        expires = parse_iso(row["expires_at"])
        if expires is None:
            cached = parse_iso(row["cached_at"])
            expires = cached + timedelta(hours=ttl_hours) if cached else utcnow()
        if expires <= utcnow():
            # Keep the stale payload available for offline fallback.
            return None
        return _loads(row["payload"])

    def get_stale_cache(self, key: str) -> Any | None:
        row = self.connection.execute(
            "SELECT payload FROM metadata_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        return _loads(row["payload"]) if row else None

    def set_cache(
        self,
        key: str,
        value: Any,
        ttl_hours: float,
        *,
        cached_at: datetime | None = None,
        endpoint_hash: str | None = None,
        query_kind: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
        search_text: str | None = None,
        commit: bool = True,
    ) -> None:
        fetched = cached_at or utcnow()
        expires = fetched + timedelta(hours=max(0, ttl_hours))
        self.connection.execute(
            """
            INSERT INTO metadata_cache(
                cache_key, payload, cached_at, expires_at, endpoint_hash,
                query_kind, page, per_page, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload = excluded.payload,
                cached_at = excluded.cached_at,
                expires_at = excluded.expires_at,
                endpoint_hash = excluded.endpoint_hash,
                query_kind = excluded.query_kind,
                page = excluded.page,
                per_page = excluded.per_page,
                search_text = excluded.search_text
            """,
            (
                key,
                _dumps(value),
                fetched.isoformat(),
                expires.isoformat(),
                endpoint_hash,
                query_kind,
                page,
                per_page,
                search_text,
            ),
        )
        if commit:
            self.connection.commit()

    def clear_metadata_cache(self) -> None:
        self.connection.execute("DELETE FROM metadata_cache")
        self.connection.commit()

    # ------------------------------------------------------------------
    # Migration

    def migrate_legacy(self, data_dir: Path, ttl_hours: int = 12) -> int:
        """Import legacy JSON files once in a single atomic transaction."""
        if self._migration_done("legacy-json-v1"):
            return int(self._meta("legacy_history_rows", "0")) + int(
                self._meta("legacy_watchlist_rows", "0")
            )
        counts = {
            "history": 0,
            "watchlist": 0,
            "ignored": 0,
            "metadata": 0,
            "metadata_items": 0,
        }
        try:
            ignored_path = data_dir / "history.json"
            if ignored_path.exists():
                raw_ignored = self._read_json(ignored_path)
                if isinstance(raw_ignored, Mapping):
                    for source_id in raw_ignored.get("ignoredAniCliSourceIds") or ():
                        if source_id:
                            self.connection.execute(
                                "INSERT OR IGNORE INTO ignored_ani_cli_source_id"
                                "(source_id, removed_at) VALUES (?, ?)",
                                (str(source_id), utcnow().isoformat()),
                            )
                            counts["ignored"] += 1

            history_path = data_dir / "history.json"
            if history_path.exists():
                for entry in self._read_json_list(history_path):
                    try:
                        parsed = HistoryEntry.from_mapping(entry)
                    except (TypeError, ValueError):
                        continue
                    if not parsed.anime.title.values():
                        continue
                    if parsed.source_id in self.ignored_sources():
                        continue
                    self.record_history(
                        parsed.anime,
                        parsed.episode,
                        entry_id=parsed.id,
                        source_id=parsed.source_id,
                        last_watched=parsed.last_watched,
                        updated_at=parsed.updated_at,
                        source=parsed.source,
                        progress_seconds=parsed.progress_seconds,
                        duration_seconds=parsed.duration_seconds,
                        commit=False,
                    )
                    counts["history"] += 1

            watchlist_path = data_dir / "watchlist.json"
            if watchlist_path.exists():
                for entry in self._read_json_list(watchlist_path):
                    try:
                        parsed = WatchlistEntry.from_mapping(entry)
                    except (TypeError, ValueError):
                        continue
                    if not parsed.anime.title.values():
                        continue
                    if self.add_watchlist(parsed.anime, parsed.created_at, commit=False):
                        counts["watchlist"] += 1

            metadata_path = data_dir / "cache" / "metadata.json"
            if metadata_path.exists():
                counts["metadata"], counts["metadata_items"] = self._import_metadata_cache(
                    metadata_path, ttl_hours
                )

            self.last_migration_counts = counts
            for key, value in counts.items():
                self._set_meta(f"legacy_{key}", str(value), commit=False)
            self._mark_migration("legacy-json-v1", commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            self.last_migration_counts = {}
            raise
        return counts["history"] + counts["watchlist"]

    def _import_metadata_cache(self, path: Path, ttl_hours: int) -> tuple[int, int]:
        raw = self._read_json(path)
        if not isinstance(raw, Mapping):
            return 0, 0
        entries = raw.get("entries")
        if not isinstance(entries, Mapping):
            return 0, 0
        imported = 0
        items = 0
        for raw_key, value in entries.items():
            if not isinstance(value, Mapping):
                continue
            key = str(raw_key)
            endpoint_hash: str | None = None
            first, separator, remainder = key.partition(":")
            if separator and len(first) == 12 and all(c in "0123456789abcdef" for c in first.lower()):
                endpoint_hash = first
                key = remainder
            parsed = self._parse_metadata_key(key)
            if parsed is None:
                continue
            query_kind, page, per_page, search_text = parsed
            canonical_key = self._metadata_key(query_kind, page, per_page, search_text)
            cached_at = parse_iso(value.get("timestamp")) or utcnow()
            media = value.get("items")
            payload = {"Page": {"media": media if isinstance(media, list) else []}}
            items += len(payload["Page"]["media"])
            self.set_cache(
                canonical_key,
                payload,
                ttl_hours,
                cached_at=cached_at,
                endpoint_hash=endpoint_hash,
                query_kind=query_kind,
                page=page,
                per_page=per_page,
                search_text=search_text,
                commit=False,
            )
            imported += 1
        return imported, items

    @staticmethod
    def _metadata_key(
        kind: str, page: int, per_page: int, search_text: str | None
    ) -> str:
        if kind == "trending":
            return f"anilist:trending:{page}:{per_page}"
        return f"anilist:search:{normalize_title(search_text or '')}:{page}:{per_page}"

    @staticmethod
    def _browse_key(
        category: str, genre: str, sort: str, page: int, per_page: int
    ) -> str:
        """Build a stable cache key for a Discover browse request."""
        return (
            "anilist:browse:"
            f"{normalize_title(category) or 'trending'}:"
            f"{normalize_title(genre) or 'all'}:"
            f"{normalize_title(sort) or 'default'}:"
            f"{page}:{per_page}"
        )

    @staticmethod
    def _parse_metadata_key(
        key: str,
    ) -> tuple[str, int, int, str | None] | None:
        parts = key.split(":")
        try:
            if len(parts) >= 3 and parts[0] == "trending":
                return "trending", int(parts[1]), int(parts[2]), None
            if len(parts) >= 4 and parts[0] == "search":
                page = int(parts[-2])
                per_page = int(parts[-1])
                search_text = ":".join(parts[1:-2])
                return "search", page, per_page, search_text
        except ValueError:
            return None
        return None

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _read_json_list(path: Path) -> Iterable[Mapping[str, Any]]:
        raw = Database._read_json(path)
        if isinstance(raw, Mapping):
            for key in ("entries", "history", "items", "watchlist"):
                value = raw.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
            return []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, Mapping)]
        return []

    def _migration_done(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM migrations WHERE name = ?", (name,)
            ).fetchone()
            is not None
        )

    def _mark_migration(self, name: str, *, commit: bool = True) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO migrations(name, applied_at) VALUES (?, ?)",
            (name, utcnow().isoformat()),
        )
        if commit:
            self.connection.commit()

    def _set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO migration_meta(key, value) VALUES (?, ?)",
            (key, value),
        )
        if commit:
            self.connection.commit()

    def _meta(self, key: str, default: str) -> str:
        row = self.connection.execute(
            "SELECT value FROM migration_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default
