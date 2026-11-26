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
from services.managed_ani_cli import ManagedAniCli
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
        managed_ani_cli_dir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or get_platform_adapter()
        self.data_dir = Path(data_dir).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.tools_dir = self.adapter.tools_dir(self.cache_dir)
        self.managed_ani_cli_dir = (
            Path(managed_ani_cli_dir).resolve()
            if managed_ani_cli_dir is not None
            else self.adapter.managed_ani_cli_dir(self.data_dir, self.cache_dir)
        )
        self.managed_ani_cli = ManagedAniCli(self.managed_ani_cli_dir)
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

    def save(self) -> None:
        """Public persistence seam for safe updater services."""
        self._save()

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
            if getattr(status, "key", "") == "ani-cli":
                state = self.managed_ani_cli.read()
                owned = bool(state and state.script.exists())
                result.append(
                    replace(
                        status,
                        owned_by_anixo=owned,
                        managed=owned,
                        safe_to_remove=owned,
                        install_location=str(state.script) if state else "",
                        source=state.source if state else status.official_url,
                    )
                )
                continue
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
        """Install into the dedicated, persistent ANIXO-managed directory.

        Overwrite permission comes from the receipt beside that copy.  An
        independently managed script anywhere else is left untouched; ANIXO
        simply points its own configuration at the copy it owns.
        """
        overwrite = self.managed_ani_cli.is_owner(self.managed_ani_cli.script_path)
        result = setup_service.install_ani_cli(
            self.managed_ani_cli_dir, overwrite=overwrite
        )
        if not result.ok:
            return result

        script = Path(result.install_location)
        paths = [script]
        wrapper = self.managed_ani_cli.wrapper_path
        if wrapper.exists():
            paths.append(wrapper)
        sha256 = result.sha256 or _digest(script)
        self.managed_ani_cli.record(
            script=script,
            sha256=sha256,
            version=result.version or "v5.1",
            source=result.source or setup_service.ANI_CLI_URL,
            install_method="managed-pinned-script",
        )
        self.manifest.upsert(
            InstallRecord(
                key=COMPONENT_ANI_CLI,
                name="ani-cli",
                platform=self.adapter.id,
                installed_by_anixo=True,
                managed=True,
                safe_to_remove=True,
                install_location=str(script.resolve()),
                version=result.version or "v5.1",
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source=result.source or setup_service.ANI_CLI_URL,
                install_method="managed-pinned-script",
                expected_sha256=setup_service.ANI_CLI_SHA256,
                observed_sha256=sha256,
                license="GPL-3.0",
                paths=tuple(str(path.resolve()) for path in paths),
                config_keys=("ani_cli_path",),
            )
        )
        self.config.ani_cli_path = str(script.resolve())
        self._save()
        self.sync_generated_artifacts()
        return result

    def ensure_managed_ani_cli(self) -> setup_service.InstallResult:
        """Return the managed copy, creating it without touching other installs."""
        state = self.managed_ani_cli.read()
        if state and state.script.exists() and _digest(state.script) == state.sha256:
            return setup_service.InstallResult(
                True,
                "ani-cli is already ANIXO-managed.",
                f"Managed directory: {self.managed_ani_cli_dir}",
                source=state.source,
                version=state.version,
                install_location=str(state.script),
                sha256=state.sha256,
            )
        return self._install_ani_cli()

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
        """Refresh the pinned managed copy without touching an independent one."""
        existing = self.managed_ani_cli.read()
        already_managed = bool(
            existing
            and existing.script.exists()
            and _digest(existing.script) == existing.sha256
        )
        if not already_managed:
            return self.ensure_managed_ani_cli()
        state = self.managed_ani_cli.read()
        if state is None:
            return setup_service.InstallResult(
                False,
                "ani-cli ownership could not be verified.",
                "No script was replaced.",
            )
        backup = (
            self.cache_dir
            / "updates"
            / "ani-cli"
            / "backups"
            / time.strftime("%Y%m%d-%H%M%S")
        )
        try:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state.script, backup / state.script.name)
            if state.wrapper.exists():
                shutil.copy2(state.wrapper, backup / state.wrapper.name)
            self.managed_ani_cli.backup(backup)
        except OSError:
            pass
        result = setup_service.install_ani_cli(
            self.managed_ani_cli_dir, overwrite=True
        )
        if result.ok:
            self.record_ani_cli_update(
                version=result.version or state.version,
                source=result.source or state.source,
                install_location=str(result.install_location),
                sha256=result.sha256 or _digest(Path(result.install_location)),
                config_path=str(result.install_location),
            )
        return result

    def record_ani_cli_update(
        self,
        *,
        version: str,
        source: str,
        install_location: str,
        sha256: str,
        config_path: str = "",
    ) -> InstallRecord:
        """Record a completed safe update in both the local receipt and manifest."""
        state = self.managed_ani_cli.read()
        if state is None or not self.managed_ani_cli.is_owner(install_location):
            raise ValueError("ani-cli is not ANIXO-managed; refusing to record an update.")
        record = self.manifest.get(COMPONENT_ANI_CLI)
        method = record.install_method if record else "managed-verified-update"
        updated = replace(
            record,
            version=str(version),
            source=str(source),
            install_location=str(state.script),
            expected_sha256=str(sha256).upper(),
            observed_sha256=str(sha256).upper(),
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            install_method=method or "managed-verified-update",
            installed_by_anixo=True,
            managed=True,
            safe_to_remove=True,
            paths=tuple(path for path in (str(state.script),) if path),
        ) if record else InstallRecord(
            key=COMPONENT_ANI_CLI,
            name="ani-cli",
            platform=self.adapter.id,
            installed_by_anixo=True,
            managed=True,
            safe_to_remove=True,
            install_location=str(state.script),
            version=str(version),
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            source=str(source),
            install_method="managed-verified-update",
            expected_sha256=str(sha256).upper(),
            observed_sha256=str(sha256).upper(),
            license="GPL-3.0",
            paths=(str(state.script),),
            config_keys=("ani_cli_path",),
        )
        self.managed_ani_cli.record(
            script=state.script,
            sha256=sha256,
            version=version,
            source=source,
            install_method=method or "managed-verified-update",
        )
        self.manifest.upsert(updated)
        if config_path:
            self.config.ani_cli_path = str(config_path)
        self.save()
        self.sync_generated_artifacts()
        return updated

    # ------------------------------------------------------------------
    # Backfill for installs from older ANIXO builds

    def backfill_from_config(self) -> bool:
        """Rebuild a manifest record only from the local ownership receipt.

        Paths, pinned digests, timestamps, and ANIXO release names are never
        treated as proof of ownership.  A script without a receipt is left
        independently managed; the updater creates a separate managed copy.
        """
        if self.manifest.get(COMPONENT_ANI_CLI) or not self.config.ani_cli_path:
            return False
        state = self.managed_ani_cli.read()
        if state is None or not state.script.exists():
            return False
        try:
            configured = Path(self.config.ani_cli_path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        if configured != state.script or _digest(state.script) != state.sha256:
            return False
        self.manifest.upsert(
            InstallRecord(
                key=COMPONENT_ANI_CLI,
                name="ani-cli",
                platform=self.adapter.id,
                installed_by_anixo=True,
                managed=True,
                safe_to_remove=True,
                install_location=str(state.script),
                version=state.version or "recorded revision",
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                source=state.source or setup_service.ANI_CLI_URL,
                install_method="receipt-backfill",
                expected_sha256=state.sha256,
                observed_sha256=state.sha256,
                paths=(str(state.script),),
                config_keys=("ani_cli_path",),
                notes="Ownership read from the receipt beside ANIXO's managed copy.",
            )
        )
        self._save()
        return True
