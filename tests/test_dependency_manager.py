from __future__ import annotations

import hashlib
from pathlib import Path

from config import Config
from platforms import WINDOWS, PlatformAdapter
from services import setup as setup_service
from services.dependency_manager import DependencyManager
from services.install_manifest import COMPONENT_ANI_CLI, ManifestStore
from services.managed_ani_cli import ManagedAniCli
from version import APP_VERSION


def _status(key: str = "ani-cli", label: str = "ani-cli") -> setup_service.DependencyStatus:
    return setup_service.DependencyStatus(
        key=key,
        label=label,
        description="test",
        required=True,
        installed=False,
        version="not found",
        detail="",
        official_url="https://example.invalid",
        winget_id=None,
        installable=True,
    )


def test_statuses_are_annotated_as_unmanaged(tmp_path: Path) -> None:
    manager = DependencyManager(
        Config(), tmp_path / "data", tmp_path / "cache", PlatformAdapter(WINDOWS)
    )
    annotated = manager.annotate_statuses([_status()])
    assert annotated[0].owned_by_anixo is False
    assert annotated[0].managed is False
    assert annotated[0].safe_to_remove is False
    assert annotated[0].source == "https://example.invalid"


def test_install_ani_cli_records_ownership(tmp_path: Path, monkeypatch) -> None:
    config = Config()
    manager = DependencyManager(
        config, tmp_path / "data", tmp_path / "cache", PlatformAdapter(WINDOWS)
    )
    script = manager.managed_ani_cli.script_path
    payload = b"official bytes"
    digest = hashlib.sha256(payload).hexdigest().upper()
    seen: dict[str, bool] = {}

    def fake_install(destination: Path, *, overwrite: bool = False):
        seen["overwrite"] = overwrite
        destination.mkdir(parents=True, exist_ok=True)
        script.write_bytes(payload)
        return setup_service.InstallResult(
            True,
            "installed",
            "verified",
            source=setup_service.ANI_CLI_URL,
            version="v5.1",
            install_location=str(script),
            sha256=digest,
        )

    monkeypatch.setattr(setup_service, "install_ani_cli", fake_install)
    result = manager.install("ani-cli", _status())

    assert result.ok
    assert seen["overwrite"] is False
    record = manager.manifest.get(COMPONENT_ANI_CLI)
    assert record is not None
    assert record.installed_by_anixo is True
    assert record.safe_to_remove is True
    assert record.expected_sha256 == setup_service.ANI_CLI_SHA256
    assert config.ani_cli_path == str(script.resolve())
    assert (tmp_path / "data" / "install-manifest-windows.json").exists()


def test_existing_unowned_ani_cli_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    manager = DependencyManager(
        Config(), tmp_path / "data", tmp_path / "cache", PlatformAdapter(WINDOWS)
    )
    seen: dict[str, bool] = {}

    def fake_install(destination: Path, *, overwrite: bool = False):
        seen["overwrite"] = overwrite
        return setup_service.InstallResult(False, "already exists", "not replaced")

    monkeypatch.setattr(setup_service, "install_ani_cli", fake_install)
    manager.install("ani-cli", _status())
    assert seen["overwrite"] is False


def test_backfill_requires_local_ownership_receipt(tmp_path: Path) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    managed_root = cache.parent / "ani-cli"
    script = managed_root / "ani-cli"
    script.parent.mkdir(parents=True)
    payload = b"verified official bytes"
    script.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest().upper()
    ManagedAniCli(managed_root).record(
        script=script,
        sha256=digest,
        version=APP_VERSION,
        source="https://example.invalid/managed-ani-cli",
        install_method="verified-install",
    )
    config = Config(ani_cli_path=str(script))
    manager = DependencyManager(config, data, cache, PlatformAdapter(WINDOWS))
    assert manager.backfill_from_config() is True
    record = manager.manifest.get(COMPONENT_ANI_CLI)
    assert record is not None
    assert record.install_method == "receipt-backfill"
    assert manager.backfill_from_config() is False


def test_winget_install_is_refused_off_windows(tmp_path: Path, monkeypatch) -> None:
    manager = DependencyManager(
        Config(), tmp_path / "data", tmp_path / "cache", PlatformAdapter("Linux")
    )
    status = setup_service.DependencyStatus(
        key="mpv",
        label="mpv",
        description="test",
        required=True,
        installed=False,
        version="not found",
        detail="",
        official_url="https://example.invalid",
        winget_id="mpv.io.mpv",
        installable=True,
    )
    called = False

    def forbidden(*args, **kwargs):  # pragma: no cover - should never run
        nonlocal called
        called = True
        return setup_service.InstallResult(True, "wrong", "")

    monkeypatch.setattr(setup_service, "install_with_winget", forbidden)
    result = manager.install("mpv", status)
    assert result.ok is False
    assert called is False


def test_update_creates_managed_ani_cli_without_existing_record(
    tmp_path: Path, monkeypatch
) -> None:
    manager = DependencyManager(
        Config(), tmp_path / "data", tmp_path / "cache", PlatformAdapter(WINDOWS)
    )
    payload = b"managed official bytes"
    digest = hashlib.sha256(payload).hexdigest().upper()

    def fake_install(destination: Path, *, overwrite: bool = False):
        assert overwrite is False
        destination.mkdir(parents=True, exist_ok=True)
        script = destination / "ani-cli"
        script.write_bytes(payload)
        return setup_service.InstallResult(
            True,
            "managed copy created",
            "verified",
            source="https://example.invalid/managed-ani-cli",
            version="managed-revision",
            install_location=str(script),
            sha256=digest,
        )

    monkeypatch.setattr(setup_service, "install_ani_cli", fake_install)
    result = manager.update_ani_cli()
    assert result.ok is True
    assert "managed copy created" in result.message
    assert manager.managed_ani_cli.script_path.read_bytes() == payload
    assert manager.managed_ani_cli.read() is not None


def test_manifest_store_can_be_injected(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "data", PlatformAdapter(WINDOWS))
    manager = DependencyManager(
        Config(),
        tmp_path / "data",
        tmp_path / "cache",
        PlatformAdapter(WINDOWS),
        store,
    )
    assert manager.store is store
    assert manager.warning == ""
