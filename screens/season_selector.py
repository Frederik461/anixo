"""ANIXO-owned franchise/season/movie/special selector.

Provider results and their original titles are shown exactly as supplied by the
provider.  ani-cli's own anime picker is never used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Static

from components.navigation import Selection
from components.shell import ShellScreen
from components.ui import EmptyState, SectionHeading, plain_text
from models.anime import Anime
from services.franchise import Franchise
from services.provider import ProviderAnime


@dataclass(frozen=True, slots=True)
class FranchiseChoice:
    franchise: Franchise
    entry: ProviderAnime


class FranchiseCell(Static):
    """A selectable, original provider title."""

    selected = reactive(False)

    class Chosen(Message):
        def __init__(self, choice: FranchiseChoice) -> None:
            self.choice = choice
            super().__init__()

    def __init__(self, choice: FranchiseChoice, *, remembered: bool) -> None:
        super().__init__(classes="franchise-cell")
        self.choice = choice
        self.remembered = remembered

    def on_mount(self) -> None:
        entry = self.choice.entry
        marker = "◆" if self.remembered else "◇"
        bits = [entry.title]
        if entry.format:
            bits.append(entry.format)
        if entry.episode_count:
            bits.append(f"{entry.episode_count} EP")
        self.update(f"{marker}  {bits[0]}\n    {'  •  '.join(bits[1:])}")

    def watch_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")

    def on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Chosen(self.choice))


class SeasonSelectorScreen(ShellScreen):
    """Choose the exact provider result before choosing an episode."""

    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("left", "franchise(-1)", "Previous group", show=False),
        Binding("right", "franchise(1)", "Next group", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("pageup", "page(-1)", "Prev page"),
        Binding("pagedown", "page(1)", "Next page"),
        Binding("home", "first", "First", show=False),
        Binding("end", "last", "Last", show=False),
        Binding("enter", "choose", "Choose"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(
        self,
        anime: Anime,
        franchises: Sequence[Franchise],
        *,
        remembered_source_id: str | None = None,
    ) -> None:
        super().__init__()
        self.anime = anime
        self.franchises = tuple(franchises)
        self.remembered_source_id = remembered_source_id
        self.choices: list[FranchiseChoice] = [
            FranchiseChoice(franchise, entry)
            for franchise in self.franchises
            for entry in franchise.entries
        ]
        self.selection = Selection(page_size=12)
        initial = 0
        if remembered_source_id:
            for index, choice in enumerate(self.choices):
                if choice.entry.source_id == remembered_source_id:
                    initial = index
                    break
        self.selection.index = initial

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Horizontal(id="season-body"):
                with Vertical(id="season-list-pane"):
                    yield SectionHeading(
                        "Seasons & Movies",
                        f"{len(self.franchises)} group(s)  •  {len(self.choices)} result(s)",
                    )
                    yield Static("", id="season-page-label", classes="meta-text")
                    with VerticalScroll(id="season-list-scroll"):
                        yield Vertical(id="season-list")
                with Vertical(id="season-info"):
                    yield Static("", id="season-info-title", classes="season-info-title")
                    yield Static("", id="season-info-meta", classes="meta-text")
                    yield Static(
                        "", id="season-info-description", classes="season-info-description"
                    )
                    yield Static("", id="season-info-source", classes="meta-text")
                    yield Button(
                        "▶  Choose This Entry",
                        id="season-choose",
                        classes="action-button primary",
                    )
                    yield Static(
                        "↑↓  move    ←→  group    Enter  choose    Esc  back",
                        id="season-shortcuts",
                        classes="meta-text",
                    )

        yield from self.compose_shell(
            active="nav-home",
            title="Choose a season",
            hint=self.anime.display_title(),
            content=content(),
        )

    async def on_mount(self) -> None:
        await self._render_page()
        self.set_focus(None)

    @property
    def current_choice(self) -> FranchiseChoice | None:
        if not self.choices:
            return None
        self.selection.clamp(len(self.choices))
        return self.choices[self.selection.index]

    async def _render_page(self) -> None:
        host = self.query_one("#season-list", Vertical)
        await host.remove_children()
        if not self.choices:
            await host.mount(
                EmptyState("The provider did not return any playable entries.")
            )
            self.query_one("#season-page-label", Static).update("No provider results")
            self._update_info()
            return

        self.selection.clamp(len(self.choices))
        start = self.selection.page_start
        end = min(self.selection.page_end, len(self.choices))
        page = self.choices[start:end]
        current_franchise: Franchise | None = None
        for choice in page:
            if choice.franchise is not current_franchise:
                current_franchise = choice.franchise
                await host.mount(
                    Static(current_franchise.label, classes="season-group")
                )
            cell = FranchiseCell(
                choice,
                remembered=choice.entry.source_id == self.remembered_source_id,
            )
            cell.selected = choice is self.current_choice
            await host.mount(cell)

        total_pages = max(
            1, (len(self.choices) + self.selection.page_size - 1) // self.selection.page_size
        )
        self.query_one("#season-page-label", Static).update(
            f"Page {start // self.selection.page_size + 1} of {total_pages}"
        )
        self._update_info()

    def _update_info(self) -> None:
        choice = self.current_choice
        title = self.query_one("#season-info-title", Static)
        meta = self.query_one("#season-info-meta", Static)
        description = self.query_one("#season-info-description", Static)
        source = self.query_one("#season-info-source", Static)
        button = self.query_one("#season-choose", Button)
        if choice is None:
            title.update("NO RESULTS")
            meta.update("")
            description.update("Try another search term from Discover.")
            source.update("")
            button.disabled = True
            return
        entry = choice.entry
        title.update(entry.title)
        bits = [entry.format or "UNKNOWN FORMAT"]
        if entry.episode_count:
            bits.append(f"{entry.episode_count} episodes")
        if entry.metadata.get("duration"):
            bits.append(entry.metadata["duration"])
        if entry.source_id == self.remembered_source_id:
            bits.append("remembered")
        meta.update("  •  ".join(bits))
        description.update(
            plain_text(entry.description, "No provider description available.")
        )
        source.update(f"PROVIDER ID  {entry.source_id}")
        button.disabled = False

    def _move(self, delta: int) -> None:
        if not self.choices:
            return
        old_page = self.selection.page_start
        self.selection.move(delta, len(self.choices))
        if self.selection.page_start != old_page:
            self.run_worker(self._render_page(), exclusive=True, group="season-render")
        else:
            for cell in self.query(FranchiseCell):
                cell.selected = cell.choice is self.current_choice
            self._update_info()

    def _franchise_bounds(self) -> list[tuple[int, int]]:
        bounds: list[tuple[int, int]] = []
        offset = 0
        for franchise in self.franchises:
            count = len(franchise.entries)
            bounds.append((offset, offset + count - 1))
            offset += count
        return bounds

    def action_move(self, delta: int) -> None:
        self._move(delta)

    def action_page(self, delta: int) -> None:
        if not self.choices:
            return
        self.selection.page_move(delta, len(self.choices))
        self.run_worker(self._render_page(), exclusive=True, group="season-render")

    def action_first(self) -> None:
        if not self.choices:
            return
        self.selection.index = 0
        self.run_worker(self._render_page(), exclusive=True, group="season-render")

    def action_last(self) -> None:
        if not self.choices:
            return
        self.selection.index = len(self.choices) - 1
        self.run_worker(self._render_page(), exclusive=True, group="season-render")

    def action_franchise(self, delta: int) -> None:
        bounds = self._franchise_bounds()
        if not bounds:
            return
        current = 0
        for index, (start, end) in enumerate(bounds):
            if start <= self.selection.index <= end:
                current = index
                break
        target = max(0, min(len(bounds) - 1, current + delta))
        self.selection.index = bounds[target][0]
        self.run_worker(self._render_page(), exclusive=True, group="season-render")

    async def action_choose(self) -> None:
        choice = self.current_choice
        if choice is not None:
            await self.app.select_provider_entry(self.anime, choice.franchise, choice.entry)

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(FranchiseCell.Chosen)
    async def on_choice(self, event: FranchiseCell.Chosen) -> None:
        event.stop()
        self.selection.index = self.choices.index(event.choice)
        await self.action_choose()

    @on(Button.Pressed, "#season-choose")
    async def on_choose_button(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_choose()
