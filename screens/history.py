"""Watch history screen with filtering, playback, and removal."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widgets import Input, Static

from components.shell import ShellScreen
from components.ui import EmptyState, SearchField, SectionHeading
from models.anime import HistoryEntry
from screens.dashboard import relative_time


class HistoryRow(Static):
    """A history entry that can be activated with a mouse click."""

    class Chosen(Message):
        def __init__(self, entry: HistoryEntry) -> None:
            self.entry = entry
            super().__init__()

    def __init__(self, entry: HistoryEntry, text: str) -> None:
        super().__init__(text, classes="history-row")
        self.entry = entry

    def on_click(self, event: Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Chosen(self.entry))


class HistoryScreen(ShellScreen):
    BINDINGS = [
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("enter", "watch", "Watch"),
        Binding("d,delete", "remove", "Remove one"),
        Binding("x", "clear_all", "Clear all"),
        Binding("/", "focus_filter", "Filter"),
        Binding("escape", "app.goto_dashboard", "Home", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[HistoryEntry] = []
        self.selection_index = 0

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            yield SearchField(
                input_id="history-filter",
                placeholder="Filter by title, source, or episode…",
            )
            with Vertical(id="history-body"):
                yield SectionHeading("Watch History", "Resume where you left off")
                with VerticalScroll(id="history-list"):
                    yield Static("Loading…", classes="history-row")

        yield from self.compose_shell(
            active="nav-history",
            title="History",
            hint="Recent watch activity",
            content=content(),
        )

    async def on_mount(self) -> None:
        await self.refresh_entries()
        self.query_one(SearchField).blur_field()

    async def refresh_entries(self) -> None:
        self.entries = await asyncio.to_thread(self.app.database.list_history, 500)
        self.selection_index = max(0, min(self.selection_index, max(0, len(self.entries) - 1)))
        self.render_rows()

    @property
    def filtered_entries(self) -> list[HistoryEntry]:
        query = (
            self.query_one("#history-filter", Input).value
            if self.is_mounted
            else ""
        ).strip().casefold()
        if not query:
            return self.entries
        return [
            entry
            for entry in self.entries
            if query in entry.anime.display_title().casefold()
            or query in (entry.title_snapshot or "").casefold()
            or query in (entry.source or "").casefold()
            or query in (entry.episode or "").casefold()
        ]

    def render_rows(self) -> None:
        container = self.query_one("#history-list", VerticalScroll)
        entries = self.filtered_entries
        self.selection_index = max(0, min(self.selection_index, max(0, len(entries) - 1)))
        container.remove_children()
        if not entries:
            container.mount(EmptyState("No matching history entries."))
            return

        body = self.query_one("#history-body", Vertical)
        heading = body.query_one(SectionHeading)
        heading.query_one(".section-hint", Static).update(f"{len(entries)} entries")

        for index, entry in enumerate(entries):
            episode = f"EP {entry.episode}" if entry.episode else "EP —"
            watched = relative_time(entry.watched_at or entry.last_watched)
            source = entry.source or "local"
            text = (
                f"[cyan]{episode}[/cyan]  {entry.anime.display_title()}  "
                f"[dim]• {watched} • {source}[/dim]"
            )
            row = HistoryRow(entry, text)
            row.set_class(index == self.selection_index, "selected")
            container.mount(row)

    def action_move(self, delta: int) -> None:
        self.selection_index = max(
            0, min(len(self.filtered_entries) - 1, self.selection_index + delta)
        )
        self.render_rows()

    def action_focus_filter(self) -> None:
        self.query_one(SearchField).focus_field()

    @on(Input.Changed, "#history-filter")
    def on_filter_changed(self, event: Input.Changed) -> None:
        self.selection_index = 0
        self.render_rows()

    @on(HistoryRow.Chosen)
    async def on_row_chosen(self, event: HistoryRow.Chosen) -> None:
        event.stop()
        entries = self.filtered_entries
        if event.entry in entries:
            self.selection_index = entries.index(event.entry)
            self.render_rows()
        await self._watch_entry(event.entry)

    async def action_watch(self) -> None:
        entries = self.filtered_entries
        if 0 <= self.selection_index < len(entries):
            await self._watch_entry(entries[self.selection_index])

    async def _watch_entry(self, entry: HistoryEntry) -> None:
        await self.app.play_history_entry(entry)

    @work
    async def action_remove(self) -> None:
        entries = self.filtered_entries
        if not 0 <= self.selection_index < len(entries):
            return
        entry = entries[self.selection_index]
        confirmed = await self.app.confirm(
            "Remove from history?",
            f"{entry.anime.display_title()} will be removed from ANIXO history.",
        )
        if not confirmed:
            return
        await asyncio.to_thread(self.app.database.remove_history, entry.id)
        self.app.notify_user(f"Removed {entry.anime.display_title()} from history")
        await self.refresh_entries()

    @work
    async def action_clear_all(self) -> None:
        if not self.entries:
            return
        confirmed = await self.app.confirm(
            "Clear all watch history?",
            f"This removes {len(self.entries)} entries. Removed ani-cli sources stay removed.",
        )
        if not confirmed:
            return
        removed = await asyncio.to_thread(self.app.database.clear_history)
        self.app.notify_user(f"Cleared {removed} history entries")
        await self.refresh_entries()
