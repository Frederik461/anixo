"""Application shell: navigation rail, top bar, and responsive screen base."""

from __future__ import annotations

from typing import Iterable

from textual import on
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

_ROUTES = {
    "nav-home": "action_goto_dashboard",
    "nav-discover": "action_goto_discover",
    "nav-watchlist": "action_goto_watchlist",
    "nav-history": "action_goto_history",
    "nav-settings": "action_goto_settings",
}


class NavItem(Button):
    """Flat navigation button with compact and full labels."""

    def __init__(
        self,
        *,
        button_id: str,
        label: str,
        compact_label: str,
        active: bool = False,
    ) -> None:
        super().__init__(
            label,
            id=button_id,
            classes=f"nav-item {'active' if active else ''}".strip(),
            compact=True,
            flat=True,
        )
        self.full_label = label
        self.compact_text = compact_label
        self.compact_mode = False

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact_mode:
            return
        self.compact_mode = compact
        self.label = self.compact_text if compact else self.full_label


class NavRail(Container, can_focus=False):
    """Persistent left navigation used by every primary screen."""

    def __init__(self, active: str) -> None:
        super().__init__(id="nav-rail")
        self.active = active

    def compose(self) -> ComposeResult:
        yield Static("◆ ANIXO", classes="nav-brand")
        yield Static("CINEMATIC TUI", classes="nav-brand-sub")
        yield Static("", classes="nav-divider")
        for button_id, label, compact_label in (
            ("nav-home", "⌂  Home", "⌂"),
            ("nav-discover", "◇  Discover", "◇"),
            ("nav-watchlist", "▣  Watchlist", "▣"),
            ("nav-history", "◷  History", "◷"),
            ("nav-settings", "⚙  Settings", "⚙"),
        ):
            yield NavItem(
                button_id=button_id,
                label=label,
                compact_label=compact_label,
                active=button_id == self.active,
            )
        yield Static("", classes="nav-divider")
        yield Static("D G W H S", classes="nav-hint")
        yield Static("? help  Q quit", classes="nav-hint")

    @on(Button.Pressed)
    def on_nav_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        action_name = _ROUTES.get(button_id)
        if not action_name:
            return
        event.stop()
        # Route actions ignore keyboard shortcuts while an input is focused.
        # A deliberate nav click must always work, so release focus first.
        focused = self.app.focused
        if isinstance(focused, Input):
            focused.blur()
        getattr(self.app, action_name)()

    def set_compact(self, compact: bool) -> None:
        for item in self.query(NavItem):
            item.set_compact(compact)


class FocusPane(Vertical, can_focus=True):
    """Main content area that owns keyboard focus for a shell screen."""

    can_focus = True


class TopBar(Horizontal, can_focus=False):
    """Screen title and lightweight context."""

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__(id="topbar")
        self.title_text = title
        self.hint_text = hint

    def compose(self) -> ComposeResult:
        yield Button("←", id="nav-back", classes="nav-arrow", disabled=True)
        yield Button("→", id="nav-forward", classes="nav-arrow", disabled=True)
        yield Static(self.title_text.upper(), classes="topbar-title")
        yield Static("", classes="topbar-rule")
        yield Static(self.hint_text, classes="topbar-hint")
        yield Static("? HELP", classes="topbar-shortcut")

    @on(Button.Pressed, "#nav-back")
    def on_back_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._release_input_focus()
        self.app.action_navigate_back()

    @on(Button.Pressed, "#nav-forward")
    def on_forward_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._release_input_focus()
        self.app.action_navigate_forward()

    def _release_input_focus(self) -> None:
        focused = self.app.focused
        if isinstance(focused, Input):
            focused.blur()


class ShellScreen(Screen[None]):
    """Base for primary screens with shared shell and responsive tiers."""

    def compose_shell(
        self,
        *,
        active: str,
        title: str,
        hint: str,
        content: Iterable[ComposeResult],
    ) -> ComposeResult:
        with Horizontal(id="shell"):
            yield NavRail(active)
            with FocusPane(id="content-pane"):
                yield TopBar(title, hint)
                yield from content
                yield Footer()

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        width = event.size.width
        height = event.size.height
        compact = width < 96 or height < 34
        medium = not compact and width < 140
        wide = width >= 140 and height >= 40
        self.set_class(compact, "compact")
        self.set_class(medium, "medium")
        self.set_class(wide, "wide")
        for rail in self.query(NavRail):
            rail.set_compact(compact)
