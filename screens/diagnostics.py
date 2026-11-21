"""Environment report screen with refresh and export actions."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from components.shell import ShellScreen
from components.ui import SectionHeading


class DiagnosticsScreen(ShellScreen):
    """Displays the latest redacted ANIXO environment report."""

    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("e", "export", "Export"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._report = ""
        self._status = "Press R to refresh the environment report."

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Vertical(id="diagnostics-panel"):
                yield Static(self._status, id="diagnostics-status", classes="meta-text")
                yield SectionHeading("Environment report", "R refresh • E export")
                with VerticalScroll(id="diagnostics-scroll"):
                    yield Static(
                        Text(self._report),
                        id="diagnostics-report",
                        classes="diagnostics-text",
                    )

        yield from self.compose_shell(
            active="nav-settings",
            title="Diagnostics",
            hint="Environment report",
            content=content(),
        )

    def set_report(self, text: str) -> None:
        """Replace the displayed report."""
        self._report = str(text)
        if self.is_mounted:
            self.query_one("#diagnostics-report", Static).update(Text(self._report))

    def set_status(self, text: str) -> None:
        """Replace the short status line above the report."""
        self._status = str(text)
        if self.is_mounted:
            self.query_one("#diagnostics-status", Static).update(self._status)

    # ------------------------------------------------------------------
    # Bindings -> app actions

    async def action_refresh(self) -> None:
        await self.app.action_diagnostics_refresh()

    async def action_export(self) -> None:
        await self.app.action_diagnostics_export()

    def action_back(self) -> None:
        """Leave the screen via an app-specific action, else pop the stack."""
        app = self.app
        custom = getattr(app, "action_diagnostics_back", None)
        if callable(custom):
            custom()
            return
        pop = getattr(app, "pop_screen", None)
        if callable(pop):
            pop()
            return
        goto_settings = getattr(app, "action_goto_settings", None)
        if callable(goto_settings):
            goto_settings()
