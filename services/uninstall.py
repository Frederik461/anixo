"""Manifest-driven uninstall planning and execution for ANIXO.

Safety rules in this module are deliberately strict:

* only records positively identified as ANIXO-owned are removable;
* paths must be inside a trusted root or a known ANIXO-managed directory;
* user data is removed only in ``complete`` mode after explicit opt-in;
* failures are reported instead of being silently skipped; and
* the log records paths and statuses, never file contents or config values.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from config import ConfigStore
from platforms import PlatformAdapter, get_platform_adapter
from services.install_manifest import COMPONENT_APP, InstallRecord, ManifestStore
from version import APP_NAME

PENDING_FILENAME = "pending-uninstall.json"
UninstallMode = Literal["app_only", "complete"]

_REMOVE_KINDS = {"file", "directory", "command", "config-pointer"}


class UninstallSafetyError(RuntimeError):
    """Raised when a manifest or target fails validation."""


@dataclass(frozen=True, slots=True)
class RemovalItem:
    """One guarded uninstall action shown in the UI and CLI."""

    key: str
    label: str
    category: str
    kind: str
    target: str
    size: int = 0
    warning: str = ""
    selected: bool = True
    command: tuple[str, ...] = ()
    config_key: str = ""
    matched_value: str = ""

    def describe(self) -> str:
        if self.kind == "command":
            return " ".join(self.command)
        return self.target


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    """Dry-run result consumed by both the UI and standalone uninstaller."""

    mode: UninstallMode
    platform: str
    manifest_path: str
    app_version: str
    items: tuple[RemovalItem, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    manifest_deletable: bool = True

    @property
    def selected_items(self) -> tuple[RemovalItem, ...]:
        return tuple(item for item in self.items if item.selected)

    def total_size(self) -> int:
        return sum(item.size for item in self.selected_items if item.kind != "command")

    def by_category(self, category: str) -> tuple[RemovalItem, ...]:
        return tuple(item for item in self.items if item.category == category)


@dataclass(slots=True)
class UninstallReport:
    """Result of an uninstall execution."""

    plan: UninstallPlan
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    manifest_removed: bool = False
    pending_removed: bool = False
    log_path: str = ""
    completed_at: str = ""

    @property
    def success(self) -> bool:
        return not self.failed

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.plan.mode,
            "platform": self.plan.platform,
            "manifest": self.plan.manifest_path,
            "app_version": self.plan.app_version,
            "success": self.success,
            "removed": list(self.removed),
            "failed": [{"item": item, "reason": reason} for item, reason in self.failed],
            "kept": [{"item": item, "reason": reason} for item, reason in self.kept],
            "warnings": list(self.plan.warnings),
            "manual_steps": self.manual_steps(),
            "log": self.log_path,
            "completed_at": self.completed_at,
        }

    def manual_steps(self) -> list[str]:
        steps: list[str] = []
        for warning in self.plan.warnings:
            steps.append(warning)
        for item, reason in self.failed:
            steps.append(f"Remove manually ({reason}): {item}")
        if self.failed:
            steps.append(
                "The install manifest was kept so this uninstall can be retried safely."
            )
        return steps


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def _unique_paths(values: Iterable[str | Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for value in values:
        text = str(value)
        if not text or text.startswith(("winget://", "http://", "https://")):
            continue
        path = Path(text).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


@dataclass(slots=True)
class UninstallPlanner:
    """Builds and executes guarded uninstall plans."""

    data_dir: Path
    cache_dir: Path
    log_dir: Path
    store: ManifestStore
    adapter: PlatformAdapter | None = None
    config_store: ConfigStore | None = None
    trusted_roots: Sequence[Path] = ()
    _trusted: tuple[Path, ...] = field(default_factory=tuple, init=False, repr=False)

    def __post_init__(self) -> None:
        self.adapter = self.adapter or get_platform_adapter()
        self.data_dir = Path(self.data_dir).resolve()
        self.cache_dir = Path(self.cache_dir).resolve()
        self.log_dir = Path(self.log_dir).resolve()
        self._trusted = tuple(Path(path).resolve() for path in self.trusted_roots)

    # ------------------------------------------------------------------
    # Validation

    def _is_trusted(self, path: Path, *, portable: bool = False) -> bool:
        candidate = Path(path).resolve()
        for root in self._trusted:
            if candidate == root or root in candidate.parents:
                return True
        if self.adapter.is_expected_install_root(candidate, portable=portable):
            return True
        managed = (
            self.adapter.tools_dir(self.cache_dir),
            self.cache_dir / "runtime",
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            Path.home() / "Desktop",
            Path.home() / "Applications",
            Path.home() / ".local" / "share" / "applications",
        )
        if self.adapter.is_windows:
            appdata = os.environ.get("APPDATA")
            if appdata:
                managed += (
                    Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                )
        return any(root == candidate or root in candidate.parents for root in managed)

    def _load_valid_manifest(self) -> tuple[Any, str]:
        if not self.store.path.exists():
            raise UninstallSafetyError(
                "No ANIXO installation manifest was found. Nothing was removed. "
                "Use the manual cleanup instructions in INSTALLATION.md instead."
            )
        loaded = self.store.load()
        if loaded.warning:
            raise UninstallSafetyError(f"{loaded.warning} Nothing was removed.")
        manifest = loaded.manifest
        if manifest.app_name != APP_NAME:
            raise UninstallSafetyError(
                "The installation manifest belongs to a different application. "
                "Nothing was removed."
            )
        if manifest.platform != self.adapter.id:
            raise UninstallSafetyError(
                f"The manifest is for {manifest.platform}, not {self.adapter.id}. "
                "Nothing was removed."
            )
        if not manifest.records:
            raise UninstallSafetyError(
                "The installation manifest has no owned components. Nothing was removed."
            )
        return manifest, str(self.store.path.resolve())

    @staticmethod
    def _owned(record: InstallRecord) -> bool:
        return (
            record.installed_by_anixo and record.managed and record.safe_to_remove
        )

    # ------------------------------------------------------------------
    # Planning

    def plan(
        self,
        mode: UninstallMode,
        *,
        include_app: bool = True,
        include_dependencies: bool = True,
        include_cache: bool = True,
        include_settings: bool | None = None,
    ) -> UninstallPlan:
        if mode not in {"app_only", "complete"}:
            raise UninstallSafetyError(f"Unknown uninstall mode: {mode}")
        if include_settings is None:
            include_settings = mode == "complete"
        if mode == "app_only" and include_settings:
            raise UninstallSafetyError(
                "Settings and personal data can only be removed in complete mode."
            )

        manifest, manifest_path = self._load_valid_manifest()
        items: list[RemovalItem] = []
        warnings: list[str] = []
        notes = [
            "ANIXO does not create Windows services or background processes.",
            "Pre-existing software is never removed.",
        ]

        app_record = manifest.get(COMPONENT_APP)
        if include_app:
            if app_record and self._owned(app_record):
                portable = manifest.installation_kind == "portable"
                for path in _unique_paths((app_record.install_location, *app_record.paths)):
                    if self._is_trusted(path, portable=portable) and path.exists():
                        items.append(
                            RemovalItem(
                                key="app",
                                label=f"Application files: {path}",
                                category="app",
                                kind="directory" if path.is_dir() else "file",
                                target=str(path),
                                size=_path_size(path),
                            )
                        )
                    else:
                        warnings.append(
                            f"Application path '{path}' is outside the trusted install "
                            "root and was not scheduled for removal. Delete it manually."
                        )
            else:
                warnings.append(
                    "No ANIXO-owned application record was found in the manifest."
                )
            items.extend(self._shortcut_items())

        if include_dependencies:
            for key, record in sorted(manifest.records.items()):
                if key == COMPONENT_APP:
                    continue
                if not self._owned(record):
                    warnings.append(
                        f"'{record.name}' was not installed by ANIXO or is not marked "
                        "safe to remove; it was left untouched."
                    )
                    continue
                for path in _unique_paths((record.install_location, *record.paths)):
                    if not self._is_trusted(path):
                        warnings.append(
                            f"Dependency path '{path}' is outside ANIXO-managed "
                            "directories and was left untouched."
                        )
                        continue
                    if not path.exists():
                        continue
                    items.append(
                        RemovalItem(
                            key=f"dependency:{key}",
                            label=f"{record.name}: {path}",
                            category="dependencies",
                            kind="directory" if path.is_dir() else "file",
                            target=str(path),
                            size=_path_size(path),
                            warning=record.notes,
                        )
                    )
                for command in record.uninstall_commands:
                    if not command:
                        continue
                    items.append(
                        RemovalItem(
                            key=f"command:{key}",
                            label=f"Uninstall {record.name} (user scope)",
                            category="dependencies",
                            kind="command",
                            target=" ".join(command),
                            command=tuple(command),
                            warning=record.notes,
                        )
                    )
                items.extend(self._config_pointer_items(record))

        if include_cache:
            for path in (self.cache_dir, self.log_dir):
                if not self._is_trusted(path) or not path.exists():
                    continue
                items.append(
                    RemovalItem(
                        key=f"cache:{path.name}",
                        label=f"Cache/log: {path}",
                        category="cache",
                        kind="directory",
                        target=str(path),
                        size=_path_size(path),
                    )
                )

        if include_settings:
            if not self._is_trusted(self.data_dir) or not self.data_dir.exists():
                warnings.append(
                    f"Data path '{self.data_dir}' is not a trusted ANIXO data root; "
                    "personal data was left untouched."
                )
            else:
                items.append(
                    RemovalItem(
                        key="data",
                        label=f"Settings and application data: {self.data_dir}",
                        category="settings",
                        kind="directory",
                        target=str(self.data_dir),
                        size=_path_size(self.data_dir),
                        warning=(
                            "Complete removal deletes history, watchlist, progress, "
                            "API settings, and image cache."
                        ),
                    )
                )

        if not app_record:
            notes.append(
                "Standalone uninstaller is available even when ANIXO cannot start."
            )

        return UninstallPlan(
            mode=mode,
            platform=self.adapter.id,
            manifest_path=manifest_path,
            app_version=str(manifest.app_version),
            items=tuple(items),
            warnings=tuple(dict.fromkeys(warnings)),
            notes=tuple(notes),
        )

    def _shortcut_items(self) -> list[RemovalItem]:
        candidates: list[Path] = []
        if self.adapter.is_windows:
            appdata = os.environ.get("APPDATA")
            if appdata:
                root = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
                candidates.extend((root, Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"))
            desktop = Path.home() / "Desktop" / f"{APP_NAME}.lnk"
            candidates.append(desktop)
        elif self.adapter.is_macos:
            candidates.append(Path.home() / "Desktop" / f"{APP_NAME}.alias")
        else:
            candidates.append(
                Path.home() / ".local" / "share" / "applications" / "anixo.desktop"
            )
        result: list[RemovalItem] = []
        for path in _unique_paths(candidates):
            if not path.exists():
                continue
            result.append(
                RemovalItem(
                    key=f"shortcut:{path.name}",
                    label=f"Shortcut: {path}",
                    category="app",
                    kind="directory" if path.is_dir() else "file",
                    target=str(path),
                    size=_path_size(path),
                )
            )
        return result

    def _config_pointer_items(self, record: InstallRecord) -> list[RemovalItem]:
        if not self.config_store or not self.config_store.path.exists():
            return []
        try:
            config = self.config_store.load()
        except (OSError, ValueError):
            return []
        targets = {path for path in _unique_paths((record.install_location, *record.paths))}
        items: list[RemovalItem] = []
        for key in record.config_keys:
            if not hasattr(config, key):
                continue
            value = str(getattr(config, key) or "")
            if not value:
                continue
            try:
                resolved = Path(value).expanduser().resolve()
            except OSError:
                continue
            if resolved in targets:
                items.append(
                    RemovalItem(
                        key=f"config:{key}",
                        label=f"Clear {key} pointer after removal",
                        category="dependencies",
                        kind="config-pointer",
                        target=str(self.config_store.path),
                        config_key=key,
                        matched_value=value,
                    )
                )
        return items

    # ------------------------------------------------------------------
    # Execution

    def execute(
        self,
        plan: UninstallPlan,
        progress: Callable[[int, int, RemovalItem, str], None] | None = None,
    ) -> UninstallReport:
        """Execute a plan, optionally reporting per-item progress.

        ``progress`` receives ``(completed, total, item, error)`` after each
        selected item.  An empty ``error`` means the item was removed.  The
        callback is advisory only: it cannot change what is removed.
        """
        report = UninstallReport(plan=plan, completed_at=_timestamp())
        selected = plan.selected_items
        total = len(selected)
        for completed, item in enumerate(selected, 1):
            error = ""
            try:
                self._apply(item)
                report.removed.append(item.describe())
            except (OSError, subprocess.SubprocessError, UninstallSafetyError) as exc:
                error = str(exc) or exc.__class__.__name__
                report.failed.append((item.describe(), error))
            except Exception as exc:  # pragma: no cover - defensive boundary
                error = f"unexpected: {exc}"
                report.failed.append((item.describe(), error))
            if progress:
                progress(completed, total, item, error)

        for item in plan.items:
            if not item.selected:
                report.kept.append((item.describe(), "deselected"))

        pending = self.data_dir / PENDING_FILENAME
        if report.success and pending.exists():
            try:
                pending.unlink()
                report.pending_removed = True
            except OSError as exc:
                report.failed.append((str(pending), f"pending request: {exc}"))

        if report.success:
            try:
                if self.store.path.exists():
                    self.store.path.unlink()
                    report.manifest_removed = True
            except OSError as exc:
                report.failed.append((str(self.store.path), f"manifest: {exc}"))

        report.log_path = self._write_log(report)
        return report

    def _apply(self, item: RemovalItem) -> None:
        if item.kind == "command":
            if not item.command:
                raise UninstallSafetyError("empty uninstall command")
            completed = subprocess.run(
                list(item.command),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[:200]
                raise UninstallSafetyError(
                    f"command exited {completed.returncode}{': ' + detail if detail else ''}"
                )
            return
        if item.kind == "config-pointer":
            if not self.config_store or not item.config_key:
                raise UninstallSafetyError("no configuration store available")
            config = self.config_store.load()
            if str(getattr(config, item.config_key, "") or "") == item.matched_value:
                setattr(config, item.config_key, "")
                self.config_store.save(config)
            return
        if item.kind not in _REMOVE_KINDS:
            raise UninstallSafetyError(f"unsupported removal kind: {item.kind}")
        path = Path(item.target)
        if not path.exists() and not path.is_symlink():
            return
        if not self._is_trusted(path, portable=item.key == "app"):
            raise UninstallSafetyError("path outside trusted roots")
        if item.kind == "directory" and path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _write_log(self, report: UninstallReport) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = Path(tempfile.gettempdir())
        path = directory / f"{APP_NAME.lower()}-uninstall-{stamp}.log"
        lines = [
            f"{APP_NAME} uninstall report",
            f"completed_at={report.completed_at}",
            f"mode={report.plan.mode}",
            f"platform={report.plan.platform}",
            f"app_version={report.plan.app_version}",
            "",
        ]
        lines.extend(f"REMOVED: {item}" for item in report.removed)
        lines.extend(f"FAILED: {item} | {reason}" for item, reason in report.failed)
        lines.extend(f"KEPT: {item} | {reason}" for item, reason in report.kept)
        lines.extend(f"WARNING: {warning}" for warning in report.plan.warnings)
        lines.append(f"SUCCESS: {report.success}")
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return ""
        return str(path)


def write_pending(
    data_dir: Path,
    *,
    mode: UninstallMode,
    selections: Mapping[str, bool] | None = None,
    wait_for_pid: int | None = None,
    cache_dir: Path | None = None,
    log_dir: Path | None = None,
    config_path: Path | None = None,
) -> Path:
    """Persist an in-app uninstall request for the standalone uninstaller."""
    if mode not in {"app_only", "complete"}:
        raise UninstallSafetyError(f"Unknown uninstall mode: {mode}")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "mode": mode,
        "selections": dict(selections or {}),
        "created_at": _timestamp(),
        "data_dir": str(root),
    }
    if cache_dir is not None:
        payload["cache_dir"] = str(Path(cache_dir).resolve())
    if log_dir is not None:
        payload["log_dir"] = str(Path(log_dir).resolve())
    if config_path is not None:
        payload["config_path"] = str(Path(config_path).resolve())
    if wait_for_pid is not None:
        payload["wait_for_pid"] = int(wait_for_pid)
    path = root / PENDING_FILENAME
    temporary = root / f"{PENDING_FILENAME}.{os.getpid()}.{time.time_ns()}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def read_pending(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / PENDING_FILENAME
    if not path.exists():
        raise UninstallSafetyError("No pending uninstall request was found.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise UninstallSafetyError(f"Pending uninstall request is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping) or int(payload.get("version", 0)) != 1:
        raise UninstallSafetyError("Pending uninstall request has an unknown version.")
    mode = str(payload.get("mode", ""))
    if mode not in {"app_only", "complete"}:
        raise UninstallSafetyError("Pending uninstall request has an invalid mode.")
    selections = payload.get("selections", {})
    return {
        "mode": mode,
        "selections": selections if isinstance(selections, Mapping) else {},
        "wait_for_pid": payload.get("wait_for_pid"),
        "created_at": str(payload.get("created_at", "")),
        "data_dir": str(payload.get("data_dir") or ""),
        "cache_dir": str(payload.get("cache_dir") or ""),
        "log_dir": str(payload.get("log_dir") or ""),
        "config_path": str(payload.get("config_path") or ""),
    }


def wait_for_pid(pid: int, *, timeout: float = 300.0) -> None:
    """Wait for ANIXO to exit before removing locked program files."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if platform.system() == "Windows":
            try:
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if str(pid) not in (completed.stdout or ""):
                    return
            except (OSError, subprocess.SubprocessError):
                return
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                return
        time.sleep(0.5)
