from __future__ import annotations

import json
from pathlib import Path

import pytest

from platforms import MACOS, WINDOWS, PlatformAdapter
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    COMPONENT_APP,
    MANIFEST_SCHEMA_VERSION,
    InstallRecord,
    ManifestStore,
    app_record,
)
from version import APP_NAME, VERSION


def _record(key: str = COMPONENT_ANI_CLI, owned: bool = True) -> InstallRecord:
    return InstallRecord(
        key=key,
        name="ani-cli",
        platform="windows",
        installed_by_anixo=owned,
        managed=owned,
        safe_to_remove=owned,
        install_location=r"C:\Tools\ani-cli",
        version="v5.1",
        installed_at="2026-11-21T00:00:00+00:00",
        source="https://example.invalid/ani-cli",
        install_method="pinned-script",
        expected_sha256="A" * 64,
        observed_sha256="A" * 64,
        paths=(r"C:\Tools\ani-cli",),
        uninstall_commands=(),
        config_keys=("ani_cli_path",),
    )


def test_manifest_location_is_platform_specific(tmp_path: Path) -> None:
    assert ManifestStore.default_filename(PlatformAdapter(WINDOWS)) == "install-manifest-windows.json"
    assert ManifestStore.default_filename(PlatformAdapter(MACOS)) == "install-manifest-macos.json"


def test_new_manifest_has_expected_metadata(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    manifest = store.new_manifest("windows-installer")
    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.app_name == APP_NAME
    assert manifest.app_version == VERSION
    assert manifest.platform == "windows"
    assert manifest.installation_kind == "windows-installer"
    assert manifest.records == {}


def test_round_trip_preserves_ownership_fields(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    manifest = store.new_manifest("windows-installer")
    manifest.upsert(_record(owned=False))
    store.save(manifest)
    loaded = store.load().manifest
    record = loaded.get(COMPONENT_ANI_CLI)
    assert record is not None
    assert record.installed_by_anixo is False
    assert record.managed is False
    assert record.safe_to_remove is False
    assert record.config_keys == ("ani_cli_path",)
    assert record.expected_sha256 == "A" * 64


def test_missing_manifest_is_not_an_error(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    result = store.load()
    assert result.warning == ""
    assert result.manifest.records == {}


def test_corrupt_manifest_is_quarantined(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    store.path.write_text("{not json", encoding="utf-8")
    result = store.load()
    assert "quarantined" in result.warning
    assert result.manifest.records == {}
    assert len(list(tmp_path.glob("install-manifest-windows.json.corrupt-*.bak"))) == 1


def test_schema_mismatch_is_quarantined(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    payload = store.new_manifest().to_mapping()
    payload["schema_version"] = 999
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    result = store.load()
    assert "quarantined" in result.warning
    assert result.manifest.schema_version == MANIFEST_SCHEMA_VERSION


def test_app_record_only_declares_program_root(tmp_path: Path) -> None:
    install_dir = tmp_path / "Programs" / "ANIXO"
    install_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    record = app_record(
        adapter=PlatformAdapter(WINDOWS),
        install_dir=install_dir,
        installation_kind="windows-installer",
        installer_source="test",
    )
    assert record.key == COMPONENT_APP
    assert record.safe_to_remove is True
    assert record.paths == (str(install_dir.resolve()),)
    assert str(data_dir) not in json.dumps(record.to_mapping())


def test_windows_and_macos_records_are_separate(tmp_path: Path) -> None:
    windows = ManifestStore(tmp_path, PlatformAdapter(WINDOWS))
    macos = ManifestStore(tmp_path, PlatformAdapter(MACOS))
    windows.save(windows.new_manifest("windows-installer"))
    macos.save(macos.new_manifest("drag-and-drop"))
    assert windows.path != macos.path
    assert windows.load().manifest.platform == "windows"
    assert macos.load().manifest.platform == "macos"


def test_manifest_rejects_unknown_record_fields(tmp_path: Path) -> None:
    record = InstallRecord.from_mapping(
        {
            **_record().to_mapping(),
            "dangerous_field": "ignore me",
        }
    )
    assert not hasattr(record, "dangerous_field")
