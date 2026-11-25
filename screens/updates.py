"""Update center with mouse and keyboard controls."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from components.shell import ShellScreen
from components.ui import SectionHeading
from services.updater import UpdateState


class UpdateScreen(ShellScreen):
    """Displays release state and drives the app's update workers."""

    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("c", "check", "Check"),
        Binding("u", "update", "Update"),
        Binding("x", "cancel", "Cancel"),
        Binding("r", "restart", "Restart + install"),
        Binding("n", "notes", "Release notes"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._state: UpdateState | None = None

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Vertical(id="update-panel"):
                yield Static("ANIXO UPDATES", classes="section-heading")
                yield Static(
                    "Checks use the configured public release feed. Recent results are "
                    "cached; no credentials are stored.",
                    id="update-intro",
                    classes="meta-text",
                )
                with Horizontal(id="update-actions"):
                    yield Button("↻  Check", id="update-check", classes="action-button")
                    yield Button("⇩  Update ANIXO", id="update-download", classes="action-button")
                    yield Button("■  Cancel", id="update-cancel", classes="action-button")
                    yield Button(
                        "⟳  Restart + install",
                        id="update-restart",
                        classes="action-button",
                    )
                    yield Button(
                        "☰  Release notes",
                        id="update-notes-button",
                        classes="action-button",
                    )
                with VerticalScroll(id="update-scroll"):
                    yield Static("", id="update-status", classes="state-message")
                    yield Static("", id="update-installed", classes="update-field")
                    yield Static("", id="update-latest", classes="update-field")
                    yield Static("", id="update-channel", classes="update-field")
                    yield Static("", id="update-verification", classes="update-field")
                    yield Static("", id="update-progress", classes="meta-text")
                    yield SectionHeading("Release notes")
                    yield Static("", id="update-notes", classes="diagnostics-text")

        yield from self.compose_shell(
            active="nav-settings",
            title="Updates",
            hint="Check state",
            content=content(),
        )

    def on_mount(self) -> None:
        # Widget queries are not ready while the mount message is handled.
        self.call_after_refresh(
            lambda: self.set_state(getattr(self.app, "update_state", UpdateState()))
        )

    def set_state(self, state: UpdateState) -> None:
        """Refresh all displayed update information."""
        self._state = state
        if not self.is_mounted:
            return
        self.query_one("#update-status", Static).update(Text(state.status))
        self.query_one("#update-installed", Static).update(f"Installed:  {state.current}")
        self.query_one("#update-latest", Static).update(f"Latest:     {state.latest or '—'}")
        self.query_one("#update-channel", Static).update(f"Channel:    {state.channel}")
        self.query_one("#update-verification", Static).update(
            f"Verification: {state.verification or 'not started'}"
        )
        self.query_one("#update-progress", Static).update(state.progress or "")
        self.query_one("#update-notes", Static).update(
            Text(state.notes or "No release notes are available yet.")
        )
        self.query_one("#update-cancel", Button).disabled = not state.can_cancel
        self.query_one("#update-restart", Button).disabled = not state.restart_required
        self.query_one("#update-download", Button).disabled = state.busy

    def set_status(self, status: str, progress: str = "") -> None:
        """Lightweight update used during long operations."""
        if self._state is None:
            return
        self._state.status = status
        if progress:
            self._state.progress = progress
        self.set_state(self._state)

    @on(Button.Pressed, "#update-check")
    async def on_check_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_check()

    @on(Button.Pressed, "#update-download")
    async def on_download_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_update()

    @on(Button.Pressed, "#update-cancel")
    async def on_cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_cancel()

    @on(Button.Pressed, "#update-restart")
    async def on_restart_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_restart()

    @on(Button.Pressed, "#update-notes-button")
    async def on_notes_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_notes()

    async def action_check(self) -> None:
        await self.app.action_check_updates()

    async def action_update(self) -> None:
        await self.app.action_update_anixo()

    async def action_cancel(self) -> None:
        self.app.action_cancel_update()

    async def action_restart(self) -> None:
        self.app.action_restart_update()

    async def action_notes(self) -> None:
        self.app.action_view_release_notes()

    def action_back(self) -> None:
        self.app.action_goto_settings()
