"""Custom episode browser and selector.

This screen replaces ani-cli's interactive episode menu: the selected episode
is passed to ani-cli as ``-e <number>`` so its episode picker is never shown.
Episode metadata is rendered only when the source actually supplied it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Input, Static

from components.navigation import Selection
from components.shell import ShellScreen
from components.ui import (
    ComposedGrid,
    EmptyState,
    LoadingState,
    SectionHeading,
    plain_text,
)
from models.anime import Anime
from models.episode import (
    COMPLETED,
    WATCHING,
    Episode,
    build_episodes,
    find_episode,
    grouped_episodes,
    has_groups,
)
from services.health_checks import (
    guard_rows_unchanged,
    message_with_health,
    provider_status,
)
from services.provider import ProviderError

_SHORTCUTS = (
    "↑↓←→  move    PgUp/PgDn  page    Home/End  first/last    "
    "Enter  play    M  mark    Shift+M  unwatch    U  sub/dub    /  jump    Esc  back"
)


class EpisodeCell(Static):
    """Compact, clickable episode tile."""

    selected = reactive(False)

    class Chosen(Message):
        def __init__(self, episode: Episode) -> None:
            self.episode = episode
            super().__init__()

    def __init__(self, episode: Episode, *, show_title: bool) -> None:
        super().__init__(classes=f"episode-cell {episode.state}")
        self.episode = episode
        self.show_title = show_title

    def on_mount(self) -> None:
        marker = "✓" if self.episode.state == COMPLETED else (
            "▶" if self.episode.state == "watching" else "·"
        )
        text = f"{marker}  EP {self.episode.number}"
        if self.show_title and self.episode.title:
            text += f"\n{self.episode.title}"
        self.update(text)

    def watch_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")

    def on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Chosen(self.episode))


class EpisodeSelectorScreen(ShellScreen):
    """Browse, inspect, and launch one exact episode."""

    # The app's default auto-focus lands on the nav rail, whose buttons consume
    # Enter/Space. Screen bindings must own those keys in the episode browser.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("up", "row(-1)", "Up", show=False),
        Binding("down", "row(1)", "Down", show=False),
        Binding("pageup", "page(-1)", "Prev page"),
        Binding("pagedown", "page(1)", "Next page"),
        Binding("home", "first", "First", show=False),
        Binding("end", "last", "Last", show=False),
        Binding("enter", "play", "Play episode"),
        Binding("p", "play", "Play", show=False),
        Binding("m", "mark", "Mark watched / unwatched"),
        Binding("M,backspace", "mark_unwatched", "Mark unwatched"),
        Binding("u", "toggle_audio", "Sub / Dub"),
        Binding("j", "jump", "Jump to episode", show=False),
        Binding("/", "jump", "Jump", show=False),
        Binding("r", "retry", "Retry episodes", show=False),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(
        self,
        anime: Anime,
        *,
        entries: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.anime = anime
        self.source_entries = entries
        self.episodes: list[Episode] = []
        self.entry = None
        self.selection = Selection(page_size=16)
        self.audio = "sub"
        self._last_tier = ""
        self._loading = False
        self._initialized = False
        self._source_error: str | None = None
        self._render_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Composition

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Horizontal(id="episode-body"):
                with Vertical(id="episode-list-pane"):
                    yield SectionHeading("Episodes", self.anime.display_title())
                    yield Static("", id="episode-page-label", classes="meta-text")
                    with VerticalScroll(id="episode-list-scroll"):
                        yield Vertical(id="episode-list")
                with Vertical(id="episode-info"):
                    yield Static("", id="episode-info-number", classes="episode-info-number")
                    yield Static("", id="episode-info-title", classes="episode-info-title")
                    yield Static("", id="episode-info-status", classes="episode-info-status")
                    yield Static(
                        "", id="episode-info-description", classes="episode-info-description"
                    )
                    yield Static("", id="episode-info-duration", classes="meta-text")
                    with Horizontal(id="episode-audio-row"):
                        yield Static("AUDIO", classes="audio-label")
                        yield Button(
                            "◉  SUB",
                            id="episode-audio-sub",
                            classes="action-button audio-option",
                        )
                        yield Button(
                            "○  DUB",
                            id="episode-audio-dub",
                            classes="action-button audio-option",
                        )
                    with Horizontal(id="episode-info-nav"):
                        yield Button("◀  Previous", id="episode-prev", classes="action-button")
                        yield Button("Next  ▶", id="episode-next", classes="action-button")
                    with Horizontal(id="episode-info-actions"):
                        yield Button(
                            "▶  Play Episode",
                            id="episode-play",
                            classes="action-button primary",
                        )
                        yield Button(
                            "✓  Mark Watched",
                            id="episode-mark",
                            classes="action-button",
                        )
                    with Horizontal(id="episode-jump-row"):
                        yield Input(
                            placeholder="Jump to episode number…",
                            id="episode-jump-input",
                        )
                    yield Static(_SHORTCUTS, id="episode-shortcuts", classes="meta-text")

        yield from self.compose_shell(
            active="nav-home",
            title="Episodes",
            hint=self.anime.display_title(),
            content=content(),
        )

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_mount(self) -> None:
        self.audio = self.app.config.preferred_audio
        self._apply_layout()
        self._render_audio()
        await self._show_loading()
        await self._load()
        self.query_one("#episode-jump-input", Input).can_focus = True
        # The nav rail is the first focusable widget; clear focus so Enter and
        # Space reach the episode bindings instead of activating navigation.
        self.set_focus(None)

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        if not self.is_mounted:
            return
        tier = self._tier()
        if tier != self._last_tier:
            self._last_tier = tier
            self._apply_layout()
            if self.episodes:
                self.run_worker(self._render_page(), exclusive=True, group="episode-render")

    def _tier(self) -> str:
        if self.has_class("compact"):
            return "compact"
        if self.has_class("wide"):
            return "wide"
        return "medium"

    def _columns(self) -> int:
        return {"compact": 2, "medium": 4, "wide": 6}[self._tier()]

    def _apply_layout(self) -> None:
        self.selection.page_size = max(1, self._columns() * 4)
        body = self.query_one("#episode-body", Horizontal)
        body.styles.layout = "vertical" if self.has_class("compact") else "horizontal"

    async def _show_loading(self) -> None:
        """Make the slow metadata/provider lookups visibly in progress."""
        host = self.query_one("#episode-list", Vertical)
        await host.remove_children()
        await host.mount(LoadingState("Loading episodes..."))
        self.query_one("#episode-page-label", Static).update("Loading episodes...")

    # ------------------------------------------------------------------
    # Audio

    def _render_audio(self) -> None:
        for language, button_id in (
            ("sub", "#episode-audio-sub"),
            ("dub", "#episode-audio-dub"),
        ):
            active = self.audio == language
            button = self.query_one(button_id, Button)
            button.set_class(active, "active")
            button.label = f"{'◉' if active else '○'}  {language.upper()}"

    def action_set_audio(self, language: str) -> None:
        normalized = str(language).lower()
        if normalized not in {"sub", "dub"}:
            return
        try:
            self.app.set_preferred_audio(normalized)
        except OSError:
            self.app.notify_user("Could not save the audio preference.")
        self.audio = self.app.config.preferred_audio
        self._render_audio()
        self.app.notify_user(f"Audio: {self.audio.upper()}")

    def action_toggle_audio(self) -> None:
        self.action_set_audio("dub" if self.audio == "sub" else "sub")

    # ------------------------------------------------------------------
    # Data

    async def _load(self) -> None:
        if self._loading:
            return
        self._loading = True
        first_load = not self._initialized
        self._source_error = None
        provider_rows: Sequence[Any] | None = None
        rows_before: tuple[Any, ...] = ()
        try:
            self.anime = await self.app.refresh_airing_progress(self.anime)
            entry = await asyncio.to_thread(
                self.app.database.find_history_for_anime, self.anime
            )
            if not self.source_entries:
                source_id = self.anime.source_id or (entry.source_id if entry else None)
                if source_id:
                    try:
                        released = await asyncio.to_thread(
                            self.app.provider.episodes, source_id
                        )
                    except ProviderError as error:
                        self._source_error = f"Provider lookup failed: {error}"
                        released = []
                    provider_rows = released
                    rows_before = tuple(released)
                    if released:
                        self.source_entries = [
                            {
                                "number": episode.number,
                                "title": episode.title,
                                "provider_episode_id": episode.provider_episode_id,
                            }
                            for episode in released
                        ]
                    elif not self._source_error:
                        self._source_error = (
                            "The provider returned no episodes for this title."
                        )
                elif first_load:
                    self._source_error = (
                        "No exact provider match is stored for this title."
                    )
            else:
                provider_rows = self.source_entries
                rows_before = tuple(self.source_entries)
            health = provider_status(rows=provider_rows, error=self._source_error)
            records = await asyncio.to_thread(
                self.app.database.get_episode_records, self.anime.id
            )
            states = {
                number: str(record["state"]) for number, record in records.items()
            }
            self.entry = entry
            last = entry.episode if entry else None
            self.episodes = build_episodes(
                self.anime,
                entries=self.source_entries,
                states=states,
                progress=records,
                last_episode=last,
            )
            episodes_before = tuple(self.episodes)
            # Health data may only decorate messages. Prove that neither the
            # provider rows nor the built episode rows changed afterwards.
            self._source_error = message_with_health(self._source_error, health) or None
            if provider_rows is not None:
                guard_rows_unchanged(rows_before, provider_rows)
            guard_rows_unchanged(episodes_before, self.episodes)
            if self.episodes:
                if not self._initialized:
                    initial = 0
                    if last:
                        found = find_episode(self.episodes, last)
                        if found is not None:
                            initial = found.ordinal
                    self.selection.index = initial
                # Reloads after playback/marking keep the current cursor.
                self.selection.clamp(len(self.episodes))
            else:
                self.selection.index = 0
                if self._source_error and first_load:
                    self.app.notify_user(self._source_error)
            self._initialized = True
            self._last_tier = self._tier()
            await self._render_page()
        finally:
            self._loading = False

    @property
    def current_episode(self) -> Episode | None:
        if not self.episodes:
            return None
        self.selection.clamp(len(self.episodes))
        return self.episodes[self.selection.index]

    # ------------------------------------------------------------------
    # Rendering

    async def _render_page(self) -> None:
        async with self._render_lock:
            await self._render_page_unlocked()

    async def _render_page_unlocked(self) -> None:
        host = self.query_one("#episode-list", Vertical)
        await host.remove_children()
        total = len(self.episodes)
        if total == 0:
            detail = self._source_error or (
                "Episode information is unavailable for this title."
            )
            await host.mount(
                EmptyState(
                    f"{detail} Press R to retry, or enter an episode number below "
                    "to play it directly."
                )
            )
            self.query_one("#episode-page-label", Static).update("No episode list")
            self._update_info()
            return

        self.selection.clamp(total)
        start = self.selection.page_start
        end = min(self.selection.page_end, total)
        page = self.episodes[start:end]
        columns = self._columns()
        show_title = self._tier() != "compact"
        groups = has_groups(self.episodes)

        for label, items in grouped_episodes(page):
            if groups and label:
                await host.mount(Static(label, classes="episode-group"))
            cells = [
                EpisodeCell(episode, show_title=show_title) for episode in items
            ]
            for cell in cells:
                cell.selected = cell.episode.ordinal == self.selection.index
            grid = ComposedGrid(cells, classes="episode-grid")
            grid.styles.grid_size_columns = columns
            await host.mount(grid)

        pages = max(1, (total + self.selection.page_size - 1) // self.selection.page_size)
        page_number = start // self.selection.page_size + 1
        self.query_one("#episode-page-label", Static).update(
            f"Page {page_number} of {pages}  •  EP {page[0].number}–{page[-1].number} "
            f"of {total}"
        )
        self._update_selection()
        self._update_info()

    def _update_selection(self) -> None:
        for cell in self.query(EpisodeCell):
            cell.selected = cell.episode.ordinal == self.selection.index

    def _update_info(self) -> None:
        episode = self.current_episode
        number = self.query_one("#episode-info-number", Static)
        title = self.query_one("#episode-info-title", Static)
        status = self.query_one("#episode-info-status", Static)
        description = self.query_one("#episode-info-description", Static)
        duration = self.query_one("#episode-info-duration", Static)
        previous = self.query_one("#episode-prev", Button)
        following = self.query_one("#episode-next", Button)
        play = self.query_one("#episode-play", Button)
        mark = self.query_one("#episode-mark", Button)

        if episode is None:
            number.update("NO EPISODE DATA")
            title.update("")
            status.update("")
            description.update(
                self._source_error
                or "The source did not provide an episode list. You can still "
                "enter an episode number below and play it directly."
            )
            duration.update("")
            previous.disabled = True
            following.disabled = True
            play.disabled = True
            mark.disabled = True
            return

        number.update(f"EPISODE {episode.number}")
        title.update(episode.display_title)
        group = f"  •  {episode.group}" if episode.group else ""
        status.update(f"●  {episode.status_label}{group}")
        description.update(
            plain_text(episode.description, "No episode description available.")
        )
        bits: list[str] = []
        if episode.duration:
            bits.append(f"Duration: {episode.duration}")
        if episode.progress_text:
            bits.append(episode.progress_text)
        duration.update("  •  ".join(bits))
        previous.disabled = episode.ordinal <= 0
        following.disabled = episode.ordinal >= len(self.episodes) - 1
        play.disabled = False
        mark.label = (
            "↺  Mark Unwatched"
            if episode.state in {COMPLETED, WATCHING}
            else "✓  Mark Watched"
        )
        mark.disabled = False

    # ------------------------------------------------------------------
    # Navigation

    def _move(self, delta: int) -> None:
        if not self.episodes:
            return
        old_page = self.selection.page_start
        self.selection.move(delta, len(self.episodes))
        if self.selection.page_start != old_page:
            self.run_worker(self._render_page(), exclusive=True, group="episode-render")
        else:
            self._update_selection()
            self._update_info()

    def action_move(self, delta: int) -> None:
        self._move(delta)

    def action_row(self, delta: int) -> None:
        self._move(delta * self._columns())

    def action_page(self, delta: int) -> None:
        if not self.episodes:
            return
        self.selection.page_move(delta, len(self.episodes))
        self.run_worker(self._render_page(), exclusive=True, group="episode-render")

    def action_first(self) -> None:
        if not self.episodes:
            return
        self.selection.index = 0
        self.run_worker(self._render_page(), exclusive=True, group="episode-render")

    def action_last(self) -> None:
        if not self.episodes:
            return
        self.selection.index = len(self.episodes) - 1
        self.run_worker(self._render_page(), exclusive=True, group="episode-render")

    # ------------------------------------------------------------------
    # Actions

    async def action_play(self) -> None:
        episode = self.current_episode
        if episode is None:
            await self._submit_jump()
            return
        await self._play(episode.number)

    async def _play(self, number: str) -> None:
        await self.app.play_episode(self.anime, number, dub=self.audio == "dub")
        await self._load()

    async def action_mark(self) -> None:
        episode = self.current_episode
        if episode is None:
            return
        if episode.state in {COMPLETED, WATCHING}:
            await self.app.mark_episode_unwatched(self.anime, episode.number)
            self.app.notify_user(f"Marked episode {episode.number} as unwatched")
        else:
            await self.app.mark_episode_watched(self.anime, episode.number)
            self.app.notify_user(f"Marked episode {episode.number} as watched")
        await self._load()

    async def action_mark_unwatched(self) -> None:
        episode = self.current_episode
        if episode is None:
            return
        await self.app.mark_episode_unwatched(self.anime, episode.number)
        self.app.notify_user(f"Marked episode {episode.number} as unwatched")
        await self._load()

    def action_jump(self) -> None:
        field = self.query_one("#episode-jump-input", Input)
        episode = self.current_episode
        field.value = episode.number if episode else ""
        field.focus()

    async def action_retry(self) -> None:
        if self._loading:
            return
        await self._show_loading()
        await self._load()
        if self.episodes:
            self.app.notify_user(f"Loaded {len(self.episodes)} episodes.")

    def action_back(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
            return
        self.app.pop_screen()

    async def _submit_jump(self) -> None:
        field = self.query_one("#episode-jump-input", Input)
        value = field.value.strip()
        field.blur()
        if not value:
            return
        if not value.lstrip("+-").isdigit():
            self.app.notify_user("Enter a numeric episode number.")
            return
        if self.episodes:
            episode = find_episode(self.episodes, value)
            if episode is None:
                self.app.notify_user(f"Episode {value} is not in the known episode list.")
                return
            self.selection.index = episode.ordinal
            await self._render_page()
            return
        await self._play(value)

    # ------------------------------------------------------------------
    # Events

    @on(Input.Submitted, "#episode-jump-input")
    async def on_jump_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self._submit_jump()

    @on(EpisodeCell.Chosen)
    def on_episode_chosen(self, event: EpisodeCell.Chosen) -> None:
        event.stop()
        self.selection.index = event.episode.ordinal
        self._update_selection()
        self._update_info()

    @on(Button.Pressed, "#episode-prev")
    def on_previous(self, event: Button.Pressed) -> None:
        event.stop()
        self._move(-1)

    @on(Button.Pressed, "#episode-next")
    def on_next(self, event: Button.Pressed) -> None:
        event.stop()
        self._move(1)

    @on(Button.Pressed, "#episode-play")
    async def on_play(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_play()

    @on(Button.Pressed, "#episode-mark")
    async def on_mark(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_mark()

    @on(Button.Pressed, "#episode-audio-sub")
    def on_audio_sub(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_set_audio("sub")

    @on(Button.Pressed, "#episode-audio-dub")
    def on_audio_dub(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_set_audio("dub")
