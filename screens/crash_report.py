"""Modal screen for copying the latest redacted crash report."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class CrashReportScreen(ModalScreen[None]):
    """Show a redacted report and let the user copy it for support."""

    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("c", "copy", "Copy report", show=False),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, text: str, *, status: str = "") -> None:
        super().__init__()
        self._text = str(text)
        self._status = str(status) or "Redacted crash report — copy with C."

    def compose(self) -> ComposeResult:
        with Vertical(id="crash-report-dialog"):
            yield Static("ANIXO crash report", classes="crash-report-title")
            yield Static(self._status, id="crash-report-status", classes="meta-text")
            with VerticalScroll(id="crash-report-scroll"):
                yield Static(
                    Text(self._text),
                    id="crash-report-text",
                    classes="diagnostics-text",
                )
            with Horizontal(id="crash-report-actions"):
                yield Button(
                    "⧉  Copy report",
                    id="crash-report-copy",
                    classes="action-button primary",
                )
                yield Button(
                    "Close",
                    id="crash-report-close",
                    classes="action-button",
                )

    @on(Button.Pressed, "#crash-report-copy")
    async def on_copy_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_copy()

    @on(Button.Pressed, "#crash-report-close")
    async def on_close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_close()

    async def action_copy(self) -> None:
        """Copy only the already-redacted text shown in this screen."""
        try:
            self.app.copy_to_clipboard(self._text)
        except Exception:
            self.app.notify_user("Could not copy the crash report.")
            return
        self.app.notify_user("Crash report copied.")

    async def action_close(self) -> None:
        self.dismiss(None)
