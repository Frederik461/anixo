"""Confirmation screen for the manifest-driven uninstaller."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from components.ui import plain_text
from services.uninstall import UninstallMode, UninstallPlan

_CATEGORIES = ("app", "dependencies", "cache", "settings")
_LABELS = {
    "app": "Application files and shortcuts",
    "dependencies": "ANIXO-managed dependencies",
    "cache": "ANIXO caches and logs",
    "settings": "Settings and personal data",
}


def _size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class UninstallScreen(ModalScreen[dict[str, Any] | None]):
    """Lets the user choose removal scope before leaving the app."""

    BINDINGS = [
        Binding("1", "select_app_only", "Remove ANIXO only", show=False),
        Binding("2", "select_complete", "Complete removal", show=False),
        Binding("enter", "continue", "Continue", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, plan: UninstallPlan) -> None:
        super().__init__()
        self.plan = plan
        self.mode: UninstallMode = "app_only"

    def compose(self) -> ComposeResult:
        with Vertical(id="uninstall-dialog"):
            yield Static("UNINSTALL ANIXO", classes="uninstall-title")
            yield Static(
                "Choose what ANIXO should remove. The standalone uninstaller "
                "finishes the job after this window closes. Its console shows "
                "live progress, the completion report, failed removals, and any "
                "manual cleanup steps.",
                classes="uninstall-message",
            )
            yield Static(
                "1  Remove ANIXO only — keeps settings, history, watchlist, and progress",
                id="uninstall-option-app",
                classes="uninstall-option",
            )
            yield Static(
                "2  Complete removal — also deletes settings and personal data",
                id="uninstall-option-complete",
                classes="uninstall-option",
            )
            with VerticalScroll(id="uninstall-scroll"):
                yield Static("", id="uninstall-summary", classes="uninstall-summary")
                for category in _CATEGORIES:
                    items = self.plan.by_category(category)
                    size = sum(item.size for item in items if item.selected)
                    checkbox = Checkbox(
                        f"{_LABELS[category]} ({_size_text(size)})",
                        value=category != "settings" and bool(items),
                        id=f"uninstall-{category}",
                    )
                    if not items:
                        checkbox.disabled = True
                    if category == "settings":
                        checkbox.disabled = True
                    yield checkbox
                yield Static("", id="uninstall-warnings", classes="uninstall-warnings")
            with Horizontal(id="uninstall-buttons"):
                yield Button("Continue", id="uninstall-continue", variant="error")
                yield Button("Cancel", id="uninstall-cancel")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#uninstall-option-app", Static).set_class(
            self.mode == "app_only", "active"
        )
        self.query_one("#uninstall-option-complete", Static).set_class(
            self.mode == "complete", "active"
        )
        settings = self.query_one("#uninstall-settings", Checkbox)
        if self.mode == "complete":
            if self.plan.by_category("settings"):
                settings.disabled = False
                settings.value = True
        else:
            settings.value = False
            settings.disabled = True

        summary = self.query_one("#uninstall-summary", Static)
        summary.update(
            plain_text(
                "Selected: "
                + _size_text(
                    sum(
                        item.size
                        for item in self.plan.items
                        if item.selected
                        and item.category != "settings"
                        or (
                            self.mode == "complete"
                            and item.category == "settings"
                            and item.selected
                        )
                    )
                )
            )
        )
        warning_lines = list(self.plan.warnings)
        if self.mode == "complete":
            warning_lines.append(
                "Complete removal permanently deletes personal data. A final "
                "confirmation is required."
            )
        self.query_one("#uninstall-warnings", Static).update(
            plain_text("\n".join(warning_lines), "No warnings.")
        )

    def action_select_app_only(self) -> None:
        self.mode = "app_only"
        self._refresh()

    def action_select_complete(self) -> None:
        self.mode = "complete"
        self._refresh()

    @on(Checkbox.Changed)
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()

    def action_continue(self) -> None:
        selections = {
            category: bool(self.query_one(f"#uninstall-{category}", Checkbox).value)
            for category in _CATEGORIES
        }
        if self.mode == "complete":
            selections["settings"] = True
        self.dismiss({"mode": self.mode, "selections": selections})

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#uninstall-continue")
    def on_continue(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_continue()

    @on(Button.Pressed, "#uninstall-cancel")
    def on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()
