"""Settings organized into Appearance, Images, Playback, Data, App and System groups."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widgets import Input, Static

from components.shell import ShellScreen
from config import IMAGE_MODES, RENDERERS, THEMES
from screens.uninstall import UninstallScreen
from services.dependencies import diagnostics
from services.terminal import resolve_renderer
from services.uninstall import UninstallSafetyError

_ACCENTS = ("#A78BFA", "#5EEAD4", "#FB7185", "#60A5FA", "#F59E0B")
_INSTALLATION_SUBSECTION = "Installation & uninstallation"

# label, key, kind, options
_SECTIONS: dict[str, tuple[tuple[str, str, str, tuple[str, ...] | None], ...]] = {
    "Appearance": (
        ("Theme", "theme", "choice", THEMES),
        ("Accent", "accent", "choice", _ACCENTS),
        ("Animations", "animations", "bool", None),
    ),
    "Image rendering": (
        ("Images", "images", "choice", IMAGE_MODES),
        ("Artwork renderer", "artwork_renderer", "choice", RENDERERS),
        ("Active protocol", "active_protocol", "readonly", None),
        ("Detected cell size", "cell_size", "readonly", None),
        ("Clear artwork cache", "clear_artwork", "action", None),
    ),
    "Playback": (
        ("Audio", "preferred_audio", "choice", ("sub", "dub")),
        ("ani-cli path", "ani_cli_path", "text", None),
        ("Git Bash path", "git_bash_path", "text", None),
        ("mpv path", "mpv_path", "text", None),
        ("CMD wrapper", "ani_cli_cmd_wrapper", "bool", None),
    ),
    "Data & cache": (
        ("Metadata endpoint", "metadata_endpoint", "text", None),
        ("Metadata TTL hours", "metadata_cache_ttl_hours", "int", None),
        ("Provider URL", "ani_cli_base_url", "text", None),
        ("ani-cli history", "ani_cli_history_path", "text", None),
        ("Page size", "page_size", "int", None),
        ("Cache entries", "cache_max_entries", "int", None),
        ("Clear metadata cache", "clear_metadata", "action", None),
    ),
    "Dependencies": (
        ("ani-cli installed", "ani_cli_version", "readonly", None),
        ("ani-cli latest", "ani_cli_latest", "readonly", None),
        ("ani-cli health", "ani_cli_status", "readonly", None),
        ("ani-cli update status", "ani_cli_update_status", "readonly", None),
        ("Last ani-cli check", "ani_cli_last_check", "readonly", None),
        ("Check automatically", "check_for_ani_cli_updates", "bool", None),
        ("Check ani-cli now", "check_ani_cli_updates", "action", None),
        ("Update ani-cli", "update_ani_cli", "action", None),
    ),
    "Updates": (
        ("Update channel", "update_channel", "choice", ("stable", "beta")),
        ("Check automatically", "check_for_app_updates", "bool", None),
        ("Check interval (min)", "update_check_interval_minutes", "int", None),
        ("Cache max age (days)", "update_cache_max_age_days", "int", None),
        ("Installed version", "app_installed_version", "readonly", None),
        ("Latest version", "app_latest_version", "readonly", None),
        ("Update status", "update_status", "readonly", None),
        ("Last check", "last_update_check", "readonly", None),
        ("Check for updates", "check_updates", "action", None),
        ("Update ANIXO", "update_anixo", "action", None),
        ("Cancel download", "cancel_update", "action", None),
        ("Restart and install", "restart_update", "action", None),
        ("View release notes", "view_release_notes", "action", None),
        ("Open update center", "open_update_center", "action", None),
        ("Update feed URL", "update_feed_url", "text", None),
        ("Manifest public key", "update_public_key", "text", None),
    ),
    "Application": (
        ("Confirm history clear", "confirm_before_clear_history", "bool", None),
        ("Migrate legacy data", "migrate_legacy", "action", None),
        ("Open setup wizard", "setup_wizard", "action", None),
        ("Full diagnostics", "diagnostics_screen", "action", None),
        ("Diagnostics snapshot", "diagnostics", "action", None),
    ),
    "System": (
        ("Platform", "platform_info", "readonly", None),
        ("Installation & uninstallation", "open_installation", "action", None),
    ),
    _INSTALLATION_SUBSECTION: (
        ("Installation kind", "installation_kind", "readonly", None),
        ("Installation manifest", "installation_manifest", "readonly", None),
        ("ANIXO-managed components", "managed_components", "readonly", None),
        ("Uninstall ANIXO", "uninstall", "action", None),
        ("Back to System", "back_system", "action", None),
    ),
}
_SECTION_NAMES = tuple(name for name in _SECTIONS if name != _INSTALLATION_SUBSECTION)


@dataclass(frozen=True, slots=True)
class _Row:
    label: str
    key: str
    kind: str
    options: tuple[str, ...] | None = None


class SettingsScreen(ShellScreen):
    BINDINGS = [
        Binding("left", "section(-1)", "Previous category", show=False),
        Binding("right", "section(1)", "Next category", show=False),
        Binding("up", "move(-1)", "Previous setting", show=False),
        Binding("down", "move(1)", "Next setting", show=False),
        Binding("enter", "activate", "Edit / toggle"),
        Binding("t", "diagnostics", "Refresh diagnostics"),
        Binding("escape", "cancel_edit", "Cancel / back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.section_index = 0
        self.row_index = 0
        self._editing_key: str | None = None
        self._editing_kind = "text"
        self._diagnostics: list[str] = []
        self._section_override: str | None = None

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Horizontal(id="settings-layout"):
                with Vertical(id="settings-categories"):
                    yield Static("CATEGORIES", classes="settings-header")
                    for name in _SECTION_NAMES:
                        item = Static(name, classes="settings-category")
                        item.tooltip = f"Open {name}"
                        yield item
                with VerticalScroll(id="settings-panel"):
                    yield Static("", id="settings-body")
                    field = Input(
                        placeholder="Edit value, then press Enter…",
                        id="setting-input",
                    )
                    field.can_focus = False
                    yield field

        yield from self.compose_shell(
            active="nav-settings",
            title="Settings",
            hint="Appearance • images • playback • data • system",
            content=content(),
        )

    async def on_mount(self) -> None:
        self._diagnostics = await asyncio.to_thread(
            diagnostics, self.app.config, self.app.ani_cli
        )
        field = self.query_one("#setting-input", Input)
        field.display = False
        field.can_focus = False
        field.blur()
        # Child widgets are not mounted yet while on_mount is running; defer
        # the first render until the DOM is ready.
        self.call_after_refresh(self._refresh_settings)

    async def _refresh_settings(self) -> None:
        await self.render_settings()

    @property
    def section_name(self) -> str:
        return self._section_override or _SECTION_NAMES[self.section_index]

    @property
    def rows(self) -> tuple[_Row, ...]:
        return tuple(_Row(*row) for row in _SECTIONS[self.section_name])

    @property
    def current_row(self) -> _Row | None:
        rows = self.rows
        if not rows:
            return None
        self.row_index = max(0, min(self.row_index, len(rows) - 1))
        return rows[self.row_index]

    def _value(self, key: str) -> Any:
        if key == "diagnostics":
            return "\n".join(self._diagnostics)
        state = getattr(self.app, "update_state", None)
        if key == "update_status":
            return str(getattr(state, "status", "No update check yet.")) or "—"
        if key in {"app_installed_version", "app_latest_version"}:
            return str(
                getattr(state, "current", "") if key == "app_installed_version"
                else getattr(state, "latest", "")
            ) or "—"
        if key in {
            "ani_cli_version",
            "ani_cli_latest",
            "ani_cli_status",
            "ani_cli_update_status",
            "ani_cli_last_check",
        }:
            installed = getattr(self.app, "ani_cli_status", None)
            check = getattr(self.app, "ani_cli_check", None)
            if key == "ani_cli_version":
                return getattr(installed, "version", "—") if installed else "—"
            if key == "ani_cli_latest":
                return getattr(check, "latest_version", "") or "not checked" if check else "not checked"
            if key == "ani_cli_status":
                return getattr(installed, "health", "—") if installed else "—"
            if key == "ani_cli_update_status":
                return (getattr(check, "message", "") or getattr(check, "error", "")) if check else "not checked"
            return self.app.config.last_ani_cli_check or "never"
        if key == "active_protocol":
            renderer, note = resolve_renderer(
                self.app.config.artwork_renderer, self.app.capabilities
            )
            return f"{renderer.upper()} — {note or self.app.capabilities.summary()}"
        if key == "cell_size":
            width, height = self.app.cell_size
            return f"{width}x{height} px (detected)"
        if key == "platform_info":
            adapter = getattr(self.app, "platform", None)
            return getattr(adapter, "display_name", "Unknown")
        if key in {
            "installation_kind",
            "installation_manifest",
            "managed_components",
        }:
            loaded = self.app.manifest_store.load()
            manifest = loaded.manifest
            if key == "installation_kind":
                return manifest.installation_kind
            if key == "installation_manifest":
                return str(self.app.manifest_store.path)
            return f"{len(manifest.records)} recorded ({len([r for r in manifest.records.values() if r.installed_by_anixo and r.managed])} owned)"
        config = self.app.config
        if hasattr(config, key):
            return getattr(config, key)
        return ""

    def _formatted(self, row: _Row) -> str:
        value = self._value(row.key)
        if row.kind == "bool":
            value = "on" if value else "off"
        elif isinstance(value, str) and len(value) > 46:
            value = value[:43] + "…"
        return str(value)

    async def render_settings(self) -> None:
        if not self.is_mounted:
            return
        active_category = self.section_name
        for index, item in enumerate(self.query(".settings-category")):
            item.set_class(_SECTION_NAMES[index] == active_category, "active")
        body = self.query_one("#settings-body", Static)
        container: list[Any] = []
        for index, row in enumerate(self.rows):
            if row.key == "diagnostics":
                text = str(self._value(row.key))
            else:
                text = f"{row.label:<22}{self._formatted(row)}"
            item = Static(
                f"❯ {text}" if index == self.row_index else f"  {text}",
                classes="settings-row",
            )
            item.set_class(index == self.row_index, "selected")
            item.data = {"index": index, "key": row.key}
            item.tooltip = f"Press Enter to {row.kind} {row.label}"
            container.append(item)
        container.append(
            Static(
                "←/→ category  •  ↑/↓ setting  •  Enter edit  •  T refresh",
                classes="meta-text",
            )
        )
        await body.remove_children()
        await body.mount_all(container)

    async def action_section(self, delta: int) -> None:
        if self._editing_key:
            return
        if self._section_override:
            await self._back_to_system()
            return
        self.section_index = (self.section_index + delta) % len(_SECTION_NAMES)
        self.row_index = 0
        await self.render_settings()

    async def action_move(self, delta: int) -> None:
        if self._editing_key:
            return
        self.row_index = max(0, min(len(self.rows) - 1, self.row_index + delta))
        await self.render_settings()

    async def action_activate(self) -> None:
        row = self.current_row
        if not row or self._editing_key:
            return
        if row.kind == "readonly":
            return
        if row.key == "open_installation":
            self._section_override = _INSTALLATION_SUBSECTION
            self.row_index = 0
            await self.render_settings()
            return
        if row.key == "back_system":
            await self._back_to_system()
            return
        if row.kind == "choice" and row.options:
            current = str(self._value(row.key))
            try:
                index = row.options.index(current)
            except ValueError:
                index = -1
            setattr(self.app.config, row.key, row.options[(index + 1) % len(row.options)])
            self._persist()
        elif row.kind == "bool":
            setattr(self.app.config, row.key, not bool(self._value(row.key)))
            self._persist()
        elif row.kind in {"text", "int"}:
            self._begin_edit(row)
            return
        elif row.key == "clear_artwork":
            await self.app.artwork.clear_cache()
            self.app.notify_user("Artwork cache cleared.")
        elif row.key == "clear_metadata":
            await asyncio.to_thread(self.app.database.clear_metadata_cache)
            self.app.notify_user("Metadata cache cleared.")
        elif row.key == "update_ani_cli":
            self.run_worker(self._update_ani_cli())
            return
        elif row.key == "check_ani_cli_updates":
            self.run_worker(self._check_ani_cli_updates())
            return
        elif row.key == "diagnostics":
            await self.action_diagnostics()
            return
        elif row.key == "check_updates":
            self.run_worker(self._check_for_updates())
            return
        elif row.key == "update_anixo":
            self.run_worker(self._update_anixo())
            return
        elif row.key == "cancel_update":
            self.app.action_cancel_update()
        elif row.key == "restart_update":
            self.app.action_restart_update()
        elif row.key == "view_release_notes":
            self.app.action_view_release_notes()
        elif row.key == "open_update_center":
            self.app.action_open_updates()
            return
        elif row.key == "migrate_legacy":
            self.run_worker(self._migrate_legacy())
            return
        elif row.key == "setup_wizard":
            self.run_worker(self._open_setup())
            return
        elif row.key == "uninstall":
            self.run_worker(self._uninstall())
            return
        elif row.key == "diagnostics_screen":
            self.app.action_goto_diagnostics()
        await self.render_settings()

    async def _check_ani_cli_updates(self) -> None:
        await self.app.action_check_ani_cli_updates()
        await self.render_settings()

    async def _update_ani_cli(self) -> None:
        confirmed = await self.app.confirm(
            "Update ani-cli?",
            "ANIXO checks the official upstream release, downloads into staging, "
            "verifies the SHA-256 digest, backs up the ANIXO-managed script, then "
            "replaces it. ani-cli's own -U updater is never used. User-managed "
            "scripts are never overwritten.",
        )
        if confirmed:
            result = await self.app.action_update_ani_cli()
            self.app.notify_user(result.message)
            if result.detail:
                self.app.notify_user(result.detail[:240])
        await self.render_settings()

    async def _update_anixo(self) -> None:
        await self.app.action_update_anixo()
        await self.render_settings()

    async def _uninstall(self) -> None:
        try:
            plan = await asyncio.to_thread(self.app.build_uninstall_plan, "app_only")
        except UninstallSafetyError as exc:
            self.app.notify_user(str(exc))
            return
        choice = await self.app.push_screen_wait(UninstallScreen(plan))
        if not choice:
            return
        mode = str(choice.get("mode", "app_only"))
        if mode == "complete":
            approved = await self.app.confirm(
                "Delete personal data?",
                "This permanently removes settings, history, watchlist, progress, "
                "API configuration, caches, and application data. This cannot be "
                "undone.",
            )
            if not approved:
                return
        selections = choice.get("selections", {})
        await self.app.action_uninstall(mode, selections if isinstance(selections, dict) else {})
        await self.render_settings()

    async def _check_for_updates(self) -> None:
        await self.app.action_check_updates()
        await self.render_settings()

    async def _migrate_legacy(self) -> None:
        await self.app.migrate_legacy_data()
        await self.render_settings()

    async def _open_setup(self) -> None:
        await self.app.action_open_setup()
        await self.render_settings()

    def _begin_edit(self, row: _Row) -> None:
        self._editing_key = row.key
        self._editing_kind = row.kind
        field = self.query_one("#setting-input", Input)
        field.value = str(self._value(row.key))
        field.placeholder = f"Edit {row.label} (Enter to save, Esc to cancel)"
        field.display = True
        field.can_focus = True
        field.focus()

    async def _end_edit(self, save: bool) -> None:
        field = self.query_one("#setting-input", Input)
        if save and self._editing_key:
            value: Any = field.value.strip()
            if self._editing_kind == "int":
                try:
                    value = int(value)
                except ValueError:
                    self.app.notify_user("Enter a whole number.")
                    return
                if value < 1 or value > 168:
                    self.app.notify_user("Value must be between 1 and 168.")
                    return
            setattr(self.app.config, self._editing_key, value)
            self._persist()
        self._editing_key = None
        field.display = False
        field.can_focus = False
        field.blur()
        field.value = ""
        await self.render_settings()

    @on(Input.Submitted, "#setting-input")
    async def on_setting_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self._end_edit(True)

    @on(Input.Changed, "#setting-input")
    def on_setting_changed(self, event: Input.Changed) -> None:
        event.stop()

    @on(Click, ".settings-category")
    async def _on_category_click(self, event: Click) -> None:
        event.stop()
        if self._editing_key:
            return
        categories = list(self.query(".settings-category"))
        try:
            index = categories.index(event.widget)
        except ValueError:
            return
        self.section_index = index
        self.row_index = 0
        await self.render_settings()

    @on(Click, ".settings-row")
    async def _on_row_click(self, event: Click) -> None:
        event.stop()
        if self._editing_key:
            return
        data = event.widget.data or {}
        try:
            index = int(data["index"])
        except (KeyError, TypeError, ValueError):
            return
        self.row_index = index
        await self.action_activate()

    async def _back_to_system(self) -> None:
        self._section_override = None
        self.section_index = _SECTION_NAMES.index("System")
        self.row_index = 0
        await self.render_settings()

    async def action_cancel_edit(self) -> None:
        if self._editing_key:
            await self._end_edit(False)
        elif self._section_override:
            await self._back_to_system()
        else:
            self.app.action_goto_dashboard()

    async def action_diagnostics(self) -> None:
        self._diagnostics = await asyncio.to_thread(
            diagnostics, self.app.config, self.app.ani_cli
        )
        await self.render_settings()

    def _persist(self) -> None:
        self.app.save_config()
