"""Small yes/no confirmation modal used by destructive actions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmDialog(ModalScreen[bool]):
    """Returns True for Y/Enter and False for N/Escape."""

    BINDINGS = [
        Binding("y,enter", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, title: str, message: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.message_text = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-body"):
            yield Static(self.title_text.upper(), classes="confirm-title")
            if self.message_text:
                yield Static(self.message_text, classes="confirm-message")
            yield Static("Y  confirm    N / Esc  cancel", classes="confirm-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
