"""Six-step first-launch setup wizard for ANIXO."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widgets import Button, Static

from components.shell import ShellScreen
from components.ui import SectionHeading, plain_text
from services.health_checks import from_dependency_statuses, health_lines
from services.setup import DependencyStatus, SetupSnapshot

_STEP_COUNT = 6
_STEP_TITLES = (
    "Welcome to ANIXO.",
    "System check",
    "Playback dependencies",
    "Automatic setup",
    "Image rendering",
    "Complete",
)
_STEP_BODIES = (
    "Your anime. Your world.",
    "Here is what ANIXO detected on this machine.",
    "ani-cli and mpv drive playback. Git Bash is only needed for .cmd/.bat wrappers.",
    "Install only what you choose. Nothing is silent and no admin rights are required.",
    "ANIXO adapts artwork to the image protocol your terminal supports.",
    "ANIXO is ready!",
)


class DependencyRow(Static):
    """Clickable dependency choice in the setup wizard."""

    class Chosen(Message):
        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    def __init__(self, status: DependencyStatus) -> None:
        state = "ready" if status.installed else (
            "required" if status.required else "optional"
        )
        marker = "✓" if status.installed else ("❯" if status.installable else "·")
        text = (
            f"{marker}  {status.label:<10} {state:<8} {status.version}\n"
            f"    {plain_text(status.description)}"
        )
        super().__init__(text, classes="setup-dependency-row")
        self.dependency_key = status.key

    def on_click(self, event: Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Chosen(self.dependency_key))


class SetupScreen(ShellScreen):
    """Presents a captured snapshot and delegates installs to the app."""

    AUTO_FOCUS = ""
    BINDINGS = [
        Binding("right,enter", "next", "Next", show=False),
        Binding("left", "back", "Back", show=False),
        Binding("escape", "back", "Back", show=False),
        Binding("i", "install", "Install selected", show=False),
        Binding("m", "migrate", "Migrate legacy data", show=False),
        Binding("up", "select(-1)", "Previous dependency", show=False),
        Binding("down", "select(1)", "Next dependency", show=False),
    ]

    def __init__(self, snapshot: SetupSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.current_step = 0
        self.selected_dependency: str | None = self._default_dependency()

    # ------------------------------------------------------------------
    # Composition and state

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Vertical(id="setup-panel"):
                yield Static("", id="setup-step", classes="meta-text")
                yield SectionHeading("First-launch setup", "6 steps")
                yield Static("", id="setup-title", classes="section-title")
                yield Static("", id="setup-body", classes="meta-text")
                with Vertical(id="setup-items-pane"):
                    yield Static("", id="setup-items", classes="setup-items")
                    yield VerticalScroll(id="setup-dependency-list")
                with Horizontal(id="setup-actions"):
                    yield Button("← Back", id="setup-back", classes="action-button ghost")
                    yield Button(
                        "Install selected (I)",
                        id="setup-install",
                        classes="action-button primary",
                    )
                    yield Button("Next →", id="setup-next", classes="action-button primary")
                    yield Button("Finish", id="setup-finish", classes="action-button primary")

        yield from self.compose_shell(
            active="nav-settings",
            title="Setup",
            hint="First launch",
            content=content(),
        )

    def on_mount(self) -> None:
        # Screens pushed from the app's on_mount can receive their own Mount
        # event before ``is_mounted`` flips true; defer until after refresh so
        # render_step's mounted guard does not suppress the first paint.
        self.call_after_refresh(self.render_step)

    def set_snapshot(self, snapshot: SetupSnapshot) -> None:
        """Replace the snapshot and refresh the current step."""
        self.snapshot = snapshot
        known = {item.key for item in snapshot.dependencies}
        if self.selected_dependency not in known:
            self.selected_dependency = self._default_dependency()
        self.render_step()

    def render_step(self) -> None:
        """Repaint all setup widgets from the current step and snapshot."""
        self.current_step = max(0, min(_STEP_COUNT - 1, int(self.current_step)))
        if not self.is_mounted:
            return

        index = self.current_step
        snapshot = self.snapshot
        self.query_one("#setup-step", Static).update(f"STEP {index + 1} / {_STEP_COUNT}")
        self.query_one("#setup-title", Static).update(plain_text(_STEP_TITLES[index]))
        self.query_one("#setup-body", Static).update(plain_text(_STEP_BODIES[index]))
        items = self.query_one("#setup-items", Static)
        dependency_list = self.query_one("#setup-dependency-list", VerticalScroll)
        if index in (2, 3):
            items.display = False
            dependency_list.display = True
            dependency_list.remove_children()
            for status in snapshot.dependencies:
                row = DependencyRow(status)
                row.set_class(status.key == self.selected_dependency, "selected")
                if not status.installable:
                    row.add_class("unavailable")
                dependency_list.mount(row)
        else:
            items.display = True
            dependency_list.display = False
            items.update(self._items_text(index))

        status = self._selected_status()
        installable = bool(status and status.installable and not status.installed)
        self.query_one("#setup-back", Button).display = index > 0
        self.query_one("#setup-next", Button).display = index < _STEP_COUNT - 1
        self.query_one("#setup-finish", Button).display = index == _STEP_COUNT - 1
        install = self.query_one("#setup-install", Button)
        install.display = index in (2, 3) and installable
        install.disabled = not installable
        if status:
            install.label = f"Install {status.label} (I)"

    # ------------------------------------------------------------------
    # Selection helpers

    def _default_dependency(self) -> str | None:
        missing = [
            item
            for item in self.snapshot.dependencies
            if item.installable and not item.installed
        ]
        candidate = next((item for item in missing if item.required), None) or (
            missing[0] if missing else None
        )
        return candidate.key if candidate else None

    def _selected_status(self) -> DependencyStatus | None:
        if not self.selected_dependency:
            return None
        return next(
            (
                item
                for item in self.snapshot.dependencies
                if item.key == self.selected_dependency
            ),
            None,
        )

    def _items_text(self, index: int) -> str:
        snapshot = self.snapshot
        lines: list[str]
        if index == 0:
            lines = [
                f"Version: {snapshot.app_version}",
                f"Setup steps: {_STEP_COUNT}",
                "Installs only run when you press Install.",
            ]
        elif index == 1:
            counts = ", ".join(
                f"{name}: {count}"
                for name, count in sorted(snapshot.legacy_data_counts.items())
            )
            legacy = snapshot.legacy_data_path or "none"
            if counts:
                legacy = f"{legacy} ({counts})"
            lines = [
                f"OS: {snapshot.os_name} {snapshot.os_version}",
                f"Architecture: {snapshot.architecture}",
                f"App version: {snapshot.app_version}",
                f"Terminal: {snapshot.terminal_summary}",
                f"Renderer: {snapshot.renderer}",
                f"Network: {'online' if snapshot.network_ok else 'offline'}",
                f"Legacy data: {legacy}",
                "",
                "Health checks:",
                *health_lines(from_dependency_statuses(snapshot.dependencies)),
            ]
        elif index in (2, 3):
            lines = []
            for item in snapshot.dependencies:
                marker = "❯" if item.key == self.selected_dependency else " "
                state = "ready" if item.installed else (
                    "required" if item.required else "optional"
                )
                lines.append(f"{marker} {item.label:<10} {state:<8} {item.version}")
                lines.append(f"    {item.description}")
                if item.detail:
                    lines.append(f"    {item.detail}")
            if index == 3:
                lines.extend(
                    (
                        "",
                        "Press I to install the selected dependency with approval.",
                    )
                )
                if snapshot.legacy_data_path:
                    lines.append("Press M to import legacy app data (with a backup).")
        elif index == 4:
            lines = [
                f"Renderer: {snapshot.renderer}",
                f"Terminal: {snapshot.terminal_summary}",
                "Change this later in Settings → Image rendering.",
            ]
        else:
            ready = sum(1 for item in snapshot.dependencies if item.installed)
            total = len(snapshot.dependencies)
            lines = [
                f"Playback components ready: {ready}/{total}",
                f"ANIXO {snapshot.app_version}",
                "Press Finish to enter the app.",
            ]
        return "\n".join(plain_text(line) for line in lines)

    # ------------------------------------------------------------------
    # Pointer and button actions

    @on(DependencyRow.Chosen)
    def on_dependency_chosen(self, event: DependencyRow.Chosen) -> None:
        event.stop()
        self.selected_dependency = event.key
        self.render_step()

    @on(Button.Pressed, "#setup-back")
    def _on_back_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.action_setup_back()

    @on(Button.Pressed, "#setup-next")
    async def _on_next_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.app.action_setup_next()

    @on(Button.Pressed, "#setup-install")
    def _on_install_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.run_worker(self._install_selected())

    @on(Button.Pressed, "#setup-finish")
    def _on_finish_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        # Confirmation and screen changes must run outside the message pump.
        self.app.run_worker(self.app.action_setup_finish())

    async def _install_selected(self) -> None:
        key = self.selected_dependency
        if key:
            await self.app.action_setup_install(key)

    # ------------------------------------------------------------------
    # Bindings

    async def action_next(self) -> None:
        # The final step calls action_setup_finish(), which may show a modal.
        self.app.run_worker(self.app.action_setup_next())

    def action_back(self) -> None:
        self.app.action_setup_back()

    async def action_install(self) -> None:
        self.app.run_worker(self._install_selected())

    async def action_migrate(self) -> None:
        self.app.run_worker(self.app.action_setup_install("legacy_data"))

    def action_select(self, delta: int) -> None:
        keys = [item.key for item in self.snapshot.dependencies if item.installable]
        if not keys:
            self.selected_dependency = None
        elif self.selected_dependency not in keys:
            self.selected_dependency = keys[0]
        else:
            position = keys.index(self.selected_dependency)
            self.selected_dependency = keys[(position + delta) % len(keys)]
        self.render_step()
