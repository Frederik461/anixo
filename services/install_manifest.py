"""Persistent, platform-specific installation manifest for ANIXO.

The manifest is an ownership receipt.  It records only what ANIXO actually
installed or generated, plus components explicitly observed as pre-existing so
the uninstaller can prove it must leave them alone.  It lives in the user data
directory so it survives application updates and is readable by the standalone
uninstaller when the app itself cannot start.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from platforms import PlatformAdapter, get_platform_adapter
from version import APP_NAME, VERSION

MANIFEST_SCHEMA_VERSION = 1

COMPONENT_APP = "app"
COMPONENT_ANI_CLI = "ani-cli"
COMPONENT_MPV = "mpv"
COMPONENT_GIT_BASH = "git_bash"
COMPONENT_FFMPEG = "ffmpeg"
COMPONENT_FZF = "fzf"
COMPONENT_RUNTIME_PREFIX = "runtime:"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """One installed or observed component."""

    key: str
    name: str
    platform: str
    installed_by_anixo: bool
    managed: bool
    safe_to_remove: bool
    install_location: str
    version: str
    installed_at: str
    source: str
    install_method: str
    expected_sha256: str = ""
    observed_sha256: str = ""
    license: str = ""
    paths: tuple[str, ...] = ()
    uninstall_commands: tuple[tuple[str, ...], ...] = ()
    config_keys: tuple[str, ...] = ()
    notes: str = ""

    def to_mapping(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["paths"] = list(self.paths)
        payload["uninstall_commands"] = [list(item) for item in self.uninstall_commands]
        payload["config_keys"] = list(self.config_keys)
        return payload

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> InstallRecord:
        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["paths"] = tuple(str(item) for item in values.get("paths", ()))
        values["uninstall_commands"] = tuple(
            tuple(str(part) for part in item)
            for item in values.get("uninstall_commands", ())
        )
        values["config_keys"] = tuple(str(item) for item in values.get("config_keys", ()))
        return cls(**values)


@dataclass(slots=True)
class InstallationManifest:
    """Versioned set of ownership records for one platform."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    app_name: str = APP_NAME
    app_version: str = VERSION
    platform: str = "windows"
    installation_kind: str = "unknown"
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    records: dict[str, InstallRecord] = field(default_factory=dict)

    def upsert(self, record: InstallRecord) -> InstallRecord:
        self.records[record.key] = record
        self.updated_at = _timestamp()
        return record

    def get(self, key: str) -> InstallRecord | None:
        return self.records.get(key)

    def remove(self, key: str) -> None:
        if key in self.records:
            del self.records[key]
            self.updated_at = _timestamp()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_name": self.app_name,
            "app_version": self.app_version,
            "platform": self.platform,
            "installation_kind": self.installation_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "records": {
                key: record.to_mapping() for key, record in sorted(self.records.items())
            },
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> InstallationManifest:
        records: dict[str, InstallRecord] = {}
        raw_records = data.get("records", {})
        if isinstance(raw_records, Mapping):
            for key, value in raw_records.items():
                if isinstance(value, Mapping):
                    record = InstallRecord.from_mapping(value)
                    records[str(key)] = record
        return cls(
            schema_version=int(data.get("schema_version", 0)),
            app_name=str(data.get("app_name") or APP_NAME),
            app_version=str(data.get("app_version") or VERSION),
            platform=str(data.get("platform") or "unknown"),
            installation_kind=str(data.get("installation_kind") or "unknown"),
            created_at=str(data.get("created_at") or _timestamp()),
            updated_at=str(data.get("updated_at") or _timestamp()),
            records=records,
        )


@dataclass(frozen=True, slots=True)
class ManifestLoadResult:
    manifest: InstallationManifest
    warning: str = ""


class ManifestStore:
    """Atomic reader/writer for the platform-specific manifest file."""

    def __init__(
        self,
        data_dir: Path | str,
        adapter: PlatformAdapter | None = None,
        filename: str | None = None,
    ) -> None:
        self.adapter = adapter or get_platform_adapter()
        self.data_dir = Path(data_dir).resolve()
        self.filename = filename or f"install-manifest-{self.adapter.id}.json"
        self.path = self.data_dir / self.filename

    @staticmethod
    def default_filename(adapter: PlatformAdapter | None = None) -> str:
        active = adapter or get_platform_adapter()
        return f"install-manifest-{active.id}.json"

    def new_manifest(self, installation_kind: str = "unknown") -> InstallationManifest:
        return InstallationManifest(
            app_version=VERSION,
            platform=self.adapter.id,
            installation_kind=installation_kind,
        )

    def load(self) -> ManifestLoadResult:
        if not self.path.exists():
            return ManifestLoadResult(self.new_manifest())
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            warning = self._quarantine("could not be parsed")
            return ManifestLoadResult(self.new_manifest(), warning)
        if not isinstance(raw, Mapping):
            warning = self._quarantine("had an invalid structure")
            return ManifestLoadResult(self.new_manifest(), warning)
        manifest = InstallationManifest.from_mapping(raw)
        if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
            warning = self._quarantine(
                f"uses schema {manifest.schema_version}; expected {MANIFEST_SCHEMA_VERSION}"
            )
            return ManifestLoadResult(self.new_manifest(), warning)
        if manifest.platform != self.adapter.id:
            return ManifestLoadResult(
                manifest,
                f"Manifest platform '{manifest.platform}' does not match this OS.",
            )
        return ManifestLoadResult(manifest)

    def save(self, manifest: InstallationManifest) -> InstallationManifest:
        manifest.platform = self.adapter.id
        manifest.app_version = VERSION
        manifest.updated_at = _timestamp()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        temporary.write_text(
            json.dumps(manifest.to_mapping(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return manifest

    def _quarantine(self, reason: str) -> str:
        warning = f"Existing installation manifest {reason}; it was quarantined."
        try:
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{time.time_ns()}.bak"
            )
            self.path.replace(backup)
        except OSError:
            return warning + " The unreadable file was left in place."
        return warning


def app_record(
    *,
    adapter: PlatformAdapter,
    install_dir: Path,
    installation_kind: str,
    installer_source: str,
    app_version: str = VERSION,
) -> InstallRecord:
    """Create the app ownership record written by installers."""
    root = Path(install_dir).resolve()
    paths = tuple(str(item) for item in (root,) if item.exists())
    return InstallRecord(
        key=COMPONENT_APP,
        name=APP_NAME,
        platform=adapter.id,
        installed_by_anixo=True,
        managed=True,
        safe_to_remove=True,
        install_location=str(root),
        version=app_version,
        installed_at=_timestamp(),
        source=installer_source or "ANIXO installer",
        install_method=installation_kind,
        paths=paths,
        notes="Program files and launchers only. User data is not part of this record.",
    )
