"""Centralized manager for external components ANIXO detects and installs.

All installs flow through this service so ownership is recorded in the
persistent manifest.  Components found on the machine but not installed by
ANIXO are explicitly marked unmanaged and are never uninstalled.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from config import Config
from platforms import PlatformAdapter, get_platform_adapter
from services import setup as setup_service
from services.ani_cli import _KNOWN_PROVIDER_SHA256
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    COMPONENT_APP,
    COMPONENT_FFMPEG,
    COMPONENT_FZF,
    COMPONENT_GIT_BASH,
    COMPONENT_MPV,
    COMPONENT_RUNTIME_PREFIX,
    InstallRecord,
    ManifestStore,
    app_record,
)
from version import VERSION

_DEPENDENCY_KEYS = {
    "ani-cli": COMPONENT_ANI_CLI,
    "mpv": COMPONENT_MPV,
    "git_bash": COMPONENT_GIT_BASH,
    "ffmpeg": COMPONENT_FFMPEG,
    "fzf": COMPONENT_FZF,
}


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest().upper()
    except OSError:
        return ""


class DependencyManager:
    """Installs dependencies and keeps the ownership manifest current."""

    def __init__(
        self,
        config: Config,
        data_dir: Path,
        cache_dir: Path,
        adapter: PlatformAdapter | None = None,
        store: ManifestStore | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or get_platform_adapter()
        self.data_dir = Path(data_dir).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.tools_dir = self.adapter.tools_dir(self.cache_dir)
        self.store = store or ManifestStore(self.data_dir, self.adapter)
        loaded = self.store.load()
        self.manifest = loaded.manifest
        self.warning = loaded.warning
        self.sync_generated_artifacts()

    # ------------------------------------------------------------------
    # Manifest helpers

    def _record_for(self, key: str) -> InstallRecord | None:
        return self.manifest.get(_DEPENDENCY_KEYS.get(key, key))

    def _save(self) -> None:
        self.store.save(self.manifest)

    def sync_generated_artifacts(self) -> None:
        """Record verified ANIXO-generated runtime copies of ani-cli."""
        runtime = self.cache_dir / "runtime"
        if not runtime.exists():
            return
        ani_owned = bool(
            (self.manifest.get(COMPONENT_ANI_CLI) or InstallRecord(
                key="", name="", platform="", installed_by_anixo=False,
                managed=False, safe_to_remove=False, install_location="",
                version="", installed_at="", source="", install_method="",
            )).installed_by_anixo
        )
        changed = False
        for path in sorted(runtime.glob("ani-cli-exact-*")):
            if not path.is_file():
                continue
            key = f"{COMPONENT_RUNTIME_PREFIX}{path.name}"
            if self.manifest.get(key):
                continue
            digest = _digest(path)
            if digest not in _KNOWN_PROVIDER_SHA256:
                continue
            self.manifest.upsert(
                InstallRecord(
                    key=key,
                    name=f"ani-cli exact-play runtime ({path.name})",
                    platform=self.adapter.id,
                    installed_by_anixo=True,
                    managed=True,
                    safe_to_remove=ani_owned,
                    install_location=str(path.resolve()),
                    version="v5.1-patched",
                    installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    source="ANIXO exact-provider patch",
                    install_method="verified-runtime-copy",
                    observed_sha256=digest,
                    paths=(str(path.resolve()),),
                    notes="Generated only for exact episode playback.",
                )
            )
            changed = True
        if changed:
            self._save()

    def annotate_statuses(
        self, statuses: Iterable[Any]
    ) -> list[Any]:
        """Attach ownership metadata to setup dependency statuses."""
        result = []
        for status in statuses:
            record = self._record_for(status.key)
            if record:
                result.append(
                    replace(
                        status,
                        owned_by_anixo=record.installed_by_anixo,
                        managed=record.managed,
                        safe_to_remove=record.safe_to_remove,
                        install_location=record.install_location,
                        source=record.source,
                    )
                )
            else:
                result.append(
                    replace(
                        status,
                        owned_by_anixo=False,
                        managed=False,
                        safe_to_remove=False,
                        install_location="",
                        source=status.official_url,
                    )
                )
        return result

    # ------------------------------------------------------------------
    # App installation receipt

    def record_app_install(
        self,
        install_dir: Path,
        *,
        installation_kind: str,
        installer_source: str,
        app_version: str = VERSION,
    ) -> InstallRecord:
        record = app_record(
            adapter=self.adapter,
            install_dir=install_dir,
            installation_kind=installation_kind,
            installer_source=installer_source,
            app_version=app_version,
        )
        self.manifest.installation_kind = installation_kind
        self.manifest.app_version = app_version
        self.manifest.upsert(record)
        self._save()
        return record

    # ------------------------------------------------------------------
    # Dependency installation

    def install(self, key: str, status: Any) -> setup_service.InstallResult:
        """Install one explicitly selected dependency and record ownership."""
        if key == "ani-cli":
            return self._install_ani_cli()
        if key in {"legacy_data", "migrate"}:
            return setup_service.InstallResult(False, "Unknown dependency.", "")
        if not self.adapter.is_windows or not getattr(status, "winget_id", None):
            return setup_service.InstallResult(
                False,
                f"Automatic installation of {getattr(status, 'label', key)} is not available on this OS.",
                f"Official source: {getattr(status, 'official_url', '')}",
            )
        return self._install_winget_dependency(key, status)

    def _install_ani_cli(self) -> setup_service.InstallResult:
        existing = self.manifest.get(COMPONENT_ANI_CLI)
        overwrite = bool(
            existing
            and existing.installed_by_anixo
            and existing.managed
            and existing.safe_to_remove
        )
        result = setup_service.install_ani_cli(self.tools_dir, overwrite=overwrite)
        if not result.ok:
            return result

        paths = [Path(result.install_location)]
        wrapper = self.tools_dir / "ani-cli.cmd"
        if wrapper.exists():
            paths.append(wrapper)
        sha256 = result.sha256 or _digest(paths[0])
        self.manifest.upsert(
            InstallRecord(
                key=COMPONENT_ANI_CLI,
                name="ani-cli",
                platform=self.adapter.id,
                installed_by_anixo=True,
                managed=True,
                safe_to_remove=True,
                install_location=str(paths[0].resolve()),
                version=result.version or "v5.1",
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source=result.source or setup_service.ANI_CLI_URL,
                install_method="pinned-script",
                expected_sha256=setup_service.ANI_CLI_SHA256,
                observed_sha256=sha256,
                license="GPL-3.0",
                paths=tuple(str(path.resolve()) for path in paths),
                config_keys=("ani_cli_path",),
            )
        )
        self.config.ani_cli_path = str(paths[0].resolve())
        self._save()
        self.sync_generated_artifacts()
        return result

    def _winget_installed(self, winget_id: str) -> bool:
        executable = shutil.which("winget")
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [
                    executable,
                    "list",
                    "--id",
                    winget_id,
                    "--exact",
                    "--disable-interactivity",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and winget_id.lower() in (
            completed.stdout or ""
        ).lower()

    def _install_winget_dependency(self, key: str, status: Any) -> setup_service.InstallResult:
        winget_id = str(status.winget_id)
        if self._winget_installed(winget_id):
            self.manifest.upsert(
                InstallRecord(
                    key=_DEPENDENCY_KEYS[key],
                    name=str(status.label),
                    platform=self.adapter.id,
                    installed_by_anixo=False,
                    managed=False,
                    safe_to_remove=False,
                    install_location=f"winget://{winget_id}",
                    version=str(status.version),
                    installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    source=f"winget:{winget_id}",
                    install_method="pre-existing",
                    notes="Detected before install; ANIXO will never remove it.",
                )
            )
            self._save()
            return setup_service.InstallResult(
                True,
                f"{status.label} is already installed.",
                "It was detected before ANIXO installed anything, so it is marked "
                "pre-existing and will not be removed by uninstall.",
            )

        result = setup_service.install_with_winget(status)
        if not result.ok:
            return result
        self.manifest.upsert(
            InstallRecord(
                key=_DEPENDENCY_KEYS[key],
                name=str(status.label),
                platform=self.adapter.id,
                installed_by_anixo=True,
                managed=True,
                safe_to_remove=True,
                install_location=f"winget://{winget_id}",
                version=result.version or str(status.version),
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source=result.source or f"winget:{winget_id}",
                install_method="winget-user-scope",
                uninstall_commands=(
                    (
                        "winget",
                        "uninstall",
                        "--id",
                        winget_id,
                        "--scope",
                        "user",
                        "--disable-interactivity",
                    ),
                ),
                notes="Installed at user scope at the user's explicit request.",
            )
        )
        self._save()
        return result

    # ------------------------------------------------------------------
    # Safe updates (never ani-cli -U)

    def update_ani_cli(self) -> setup_service.InstallResult:
        record = self.manifest.get(COMPONENT_ANI_CLI)
        if not record or not record.installed_by_anixo:
            return setup_service.InstallResult(
                False,
                "ani-cli was not installed by ANIXO.",
                "ANIXO will not replace a script owned by another installer.",
            )
        source = Path(record.install_location)
        if source.exists():
            backup = source.with_name(
                f"ani-cli.anixo-backup-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                shutil.copy2(source, backup)
            except OSError:
                pass
        result = setup_service.install_ani_cli(self.tools_dir, overwrite=True)
        if result.ok:
            self.manifest.upsert(
                replace(
                    record,
                    version=result.version or record.version,
                    source=result.source or record.source,
                    expected_sha256=setup_service.ANI_CLI_SHA256,
                    observed_sha256=result.sha256 or _digest(Path(result.install_location)),
                    installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            )
            self.config.ani_cli_path = result.install_location
            self._save()
            self.sync_generated_artifacts()
        return result

    # ------------------------------------------------------------------
    # Backfill for installs from older ANIXO builds

    def backfill_from_config(self) -> bool:
        if self.manifest.get(COMPONENT_ANI_CLI) or not self.config.ani_cli_path:
            return False
        path = Path(self.config.ani_cli_path).expanduser().resolve()
        tools = self.tools_dir.resolve()
        if tools not in path.parents or not path.exists():
            return False
        digest = _digest(path)
        if digest != setup_service.ANI_CLI_SHA256:
            return False
        self.manifest.upsert(
            InstallRecord(
                key=COMPONENT_ANI_CLI,
                name="ani-cli",
                platform=self.adapter.id,
                installed_by_anixo=True,
                managed=True,
                safe_to_remove=True,
                install_location=str(path),
                version="v5.1",
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source=setup_service.ANI_CLI_URL,
                install_method="backfilled-from-config",
                expected_sha256=setup_service.ANI_CLI_SHA256,
                observed_sha256=digest,
                paths=(str(path),),
                config_keys=("ani_cli_path",),
                notes="Ownership inferred from ANIXO's tools directory and official digest.",
            )
        )
        self._save()
        return True
