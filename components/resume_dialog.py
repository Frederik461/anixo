"""Resume-or-select modal shown when a title already has progress."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ResumeDialog(ModalScreen[str | None]):
    """Returns ``"resume"``, ``"select"``, or ``None``."""

    BINDINGS = [
        Binding("r,enter", "resume", "Resume"),
        Binding("s", "select", "Select episode"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        title: str,
        episode: str,
        total: int | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.episode = episode
        self.total = total

    def compose(self) -> ComposeResult:
        where = f"Episode {self.episode}"
        if self.total:
            where += f" of {self.total}"
        with Container(id="confirm-body"):
            yield Static("CONTINUE WATCHING", classes="confirm-title")
            yield Static(
                f"{self.title_text}\n{where}",
                classes="confirm-message",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button(
                    f"▶  Resume EP {self.episode}",
                    id="resume-yes",
                    classes="action-button primary",
                )
                yield Button(
                    "▤  Select Episode",
                    id="resume-select",
                    classes="action-button",
                )
                yield Button(
                    "Cancel",
                    id="resume-cancel",
                    classes="action-button ghost",
                )
            yield Static(
                "R / Enter  resume    S  select    Esc  cancel",
                classes="confirm-hint",
            )

    def action_resume(self) -> None:
        self.dismiss("resume")

    def action_select(self) -> None:
        self.dismiss("select")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#resume-yes")
    def on_resume(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss("resume")

    @on(Button.Pressed, "#resume-select")
    def on_select(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss("select")

    @on(Button.Pressed, "#resume-cancel")
    def on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
