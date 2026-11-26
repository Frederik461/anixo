"""Offline ownership tests for ANIXO's dedicated ani-cli installation.

Every fixture lives under ``tmp_path``.  No test downloads or executes ani-cli,
uses a live network, depends on an ANIXO version literal, or targets an
installed application directory.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

import updater_helper
from config import Config
from platforms import WINDOWS, PlatformAdapter
from services import setup as setup_service
from services.ani_cli import _MENU_ANCHOR, _SEARCH_ANCHOR
from services.ani_cli_updater import AniCliUpdateCheck, AniCliUpdateService
from services.dependency_manager import DependencyManager
from services.install_manifest import COMPONENT_ANI_CLI, InstallRecord, ManifestStore
from services.managed_ani_cli import (
    METADATA_FILE,
    ManagedAniCli,
    ManagedAniCliState,
)
from version import APP_VERSION


@pytest.fixture(autouse=True)
def _isolated_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANIXO_ANI_CLI_DIR", "ANIXO_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _derived_version(base: str, increment: int = 1) -> str:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", base)
    assert match is not None
    return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + increment}"


def _source(version: str) -> bytes:
    head = (
        f"#!/usr/bin/env bash\n# ani-cli {version}\n"
        "base_api=https://example.invalid\n"
    ).encode("utf-8")
    return head + (_SEARCH_ANCHOR + _MENU_ANCHOR).encode("utf-8") + (
        b"\n" + b"# padding line\n" * 400
    )


def _check(version: str, payload: bytes, available: bool) -> AniCliUpdateCheck:
    return AniCliUpdateCheck(
        APP_VERSION,
        version,
        available,
        "",
        "https://example.invalid/ani-cli",
        _sha(payload),
        "",
    )


class _StubAniCli:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def installed_script(self) -> Path | None:
        return self.path


def _status() -> setup_service.DependencyStatus:
    return setup_service.DependencyStatus(
        key="ani-cli",
        label="ani-cli",
        description="test",
        required=True,
        installed=False,
        version="not found",
        detail="",
        official_url="https://example.invalid/ani-cli",
        winget_id=None,
        installable=True,
    )


def _manager(
    tmp_path: Path, managed_root: Path, config: Config | None = None
) -> tuple[Config, DependencyManager, ManifestStore]:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    store = ManifestStore(data, PlatformAdapter(WINDOWS))
    active_config = config or Config()
    manager = DependencyManager(
        active_config,
        data,
        cache,
        PlatformAdapter(WINDOWS),
        store,
        managed_ani_cli_dir=managed_root,
    )
    return active_config, manager, store


def _service(
    manager: DependencyManager, installed_path: Path | None
) -> AniCliUpdateService:
    return AniCliUpdateService(
        manager.config,
        manager.data_dir,
        ani_cli=_StubAniCli(installed_path),
        manager=manager,
        adapter=PlatformAdapter(WINDOWS),
    )


def _seed_managed(
    manager: DependencyManager, payload: bytes, version: str
) -> ManagedAniCliState:
    script = manager.managed_ani_cli.script_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(payload)
    digest = _sha(payload)
    state = manager.managed_ani_cli.record(
        script=script,
        sha256=digest,
        version=version,
        source="https://example.invalid/managed-ani-cli",
        install_method="verified-install",
    )
    manager.manifest.upsert(
        InstallRecord(
            key=COMPONENT_ANI_CLI,
            name="ani-cli",
            platform="windows",
            installed_by_anixo=True,
            managed=True,
            safe_to_remove=True,
            install_location=str(script),
            version=version,
            installed_at="synthetic-time",
            source="https://example.invalid/managed-ani-cli",
            install_method="verified-install",
            expected_sha256=digest,
            observed_sha256=digest,
            paths=(str(script),),
            config_keys=("ani_cli_path",),
        )
    )
    manager.config.ani_cli_path = str(script)
    return state


def _fake_fresh_install(
    payload: bytes, version: str
) -> Callable[..., setup_service.InstallResult]:
    def install(destination: Path, *, overwrite: bool = False):
        assert overwrite is False
        target_dir = Path(destination)
        target_dir.mkdir(parents=True, exist_ok=True)
        script = target_dir / "ani-cli"
        script.write_bytes(payload)
        return setup_service.InstallResult(
            True,
            "managed copy created",
            "verified",
            source="https://example.invalid/managed-ani-cli",
            version=version,
            install_location=str(script),
            sha256=_sha(payload),
        )

    return install


def _patch_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_service, "_find_git_bash", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1. Fresh install
# ---------------------------------------------------------------------------


def test_fresh_install_creates_dedicated_managed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    config, manager, _ = _manager(tmp_path, managed_root)
    payload = _source(APP_VERSION)
    monkeypatch.setattr(
        setup_service, "install_ani_cli", _fake_fresh_install(payload, APP_VERSION)
    )

    result = manager.install("ani-cli", _status())
    state = manager.managed_ani_cli.read()

    assert result.ok is True
    assert state is not None
    assert state.script == managed_root / "ani-cli"
    assert state.script.read_bytes() == payload
    assert state.installed_by_anixo is True
    assert state.managed is True
    assert manager.manifest.get(COMPONENT_ANI_CLI).installed_by_anixo is True
    assert config.ani_cli_path == str(state.script)
    assert (managed_root / METADATA_FILE).is_file()


# ---------------------------------------------------------------------------
# 2. Manual installation is left alone
# ---------------------------------------------------------------------------


def test_manual_install_is_not_replaced_by_managed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = tmp_path / "manual" / "ani-cli"
    manual.parent.mkdir(parents=True)
    payload = _source(APP_VERSION)
    manual.write_bytes(payload)
    config = Config(ani_cli_path=str(manual))
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root, config)
    service = _service(manager, manual)
    monkeypatch.setattr(
        setup_service, "install_ani_cli", _fake_fresh_install(payload, APP_VERSION)
    )

    result = service.update(_check(APP_VERSION, payload, available=False))
    state = service.managed.read()

    assert result.ok is True
    assert "left untouched" in result.message
    assert manual.read_bytes() == payload
    assert state is not None
    assert state.script.parent == managed_root
    assert state.script.read_bytes() == payload
    assert config.ani_cli_path == str(state.script)
    assert not service.managed.is_owner(manual)


# ---------------------------------------------------------------------------
# 3. Independent installation is left alone
# ---------------------------------------------------------------------------


def test_independent_install_is_not_claimed_or_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    independent = tmp_path / "tools" / "ani-cli"
    independent.parent.mkdir(parents=True)
    payload = _source(APP_VERSION)
    independent.write_bytes(payload)
    managed_root = tmp_path / "persistent" / "ani-cli"
    config, manager, store = _manager(
        tmp_path, managed_root, Config(ani_cli_path=str(independent))
    )
    digest = _sha(payload)
    manifest = store.new_manifest("windows-installer")
    manifest.upsert(
        InstallRecord(
            key=COMPONENT_ANI_CLI,
            name="ani-cli",
            platform="windows",
            installed_by_anixo=False,
            managed=False,
            safe_to_remove=False,
            install_location=str(independent),
            version=APP_VERSION,
            installed_at="synthetic-time",
            source="https://example.invalid/independent-ani-cli",
            install_method="manual",
            expected_sha256=digest,
            observed_sha256=digest,
            paths=(str(independent),),
            config_keys=("ani_cli_path",),
        )
    )
    store.save(manifest)
    service = _service(manager, independent)
    monkeypatch.setattr(
        setup_service, "install_ani_cli", _fake_fresh_install(payload, APP_VERSION)
    )

    detected = service.detect_installed()
    result = service.update(_check(APP_VERSION, payload, available=False))
    state = service.managed.read()

    assert detected.owned_by_anixo is False
    assert "user or another installer" in detected.detail
    assert result.ok is True
    assert independent.read_bytes() == payload
    assert state is not None
    assert state.script.parent == managed_root
    assert not service.managed.is_owner(independent)
    assert config.ani_cli_path == str(state.script)


# ---------------------------------------------------------------------------
# 4. Successful staged update
# ---------------------------------------------------------------------------


def test_successful_update_verifies_and_records_managed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    config, manager, _ = _manager(tmp_path, managed_root)
    previous = _source(APP_VERSION)
    _seed_managed(manager, previous, APP_VERSION)
    candidate_version = _derived_version(APP_VERSION)
    candidate = _source(candidate_version)
    service = _service(manager, manager.managed_ani_cli.script_path)
    _patch_bash(monkeypatch)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: candidate)

    result = service.update(_check(candidate_version, candidate, available=True))
    state = service.managed.read()
    record = manager.manifest.get(COMPONENT_ANI_CLI)

    assert result.ok is True
    assert result.version == candidate_version
    assert result.digest == _sha(candidate)
    assert state is not None
    assert state.script.read_bytes() == candidate
    assert state.sha256 == _sha(candidate)
    assert state.version == candidate_version
    assert record is not None and record.version == candidate_version
    assert config.ani_cli_path == str(state.script)
    assert Path(result.backup).is_dir()


# ---------------------------------------------------------------------------
# 5. No update available
# ---------------------------------------------------------------------------


def test_no_update_leaves_managed_copy_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    payload = _source(APP_VERSION)
    state = _seed_managed(manager, payload, APP_VERSION)
    service = _service(manager, state.script)

    def forbidden_download(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("no download may occur when no update is available")

    monkeypatch.setattr(service, "_download", forbidden_download)
    result = service.update(_check(APP_VERSION, payload, available=False))
    after = service.managed.read()

    assert result.ok is True
    assert "already current" in result.message
    assert state.script.read_bytes() == payload
    assert after is not None and after.sha256 == state.sha256
    assert after.version == state.version


# ---------------------------------------------------------------------------
# 6. Failed download
# ---------------------------------------------------------------------------


def test_failed_download_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    previous = _source(APP_VERSION)
    state = _seed_managed(manager, previous, APP_VERSION)
    candidate = _source(_derived_version(APP_VERSION))
    service = _service(manager, state.script)

    def failing_download(*args: Any, **kwargs: Any) -> bytes:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(service, "_download", failing_download)
    result = service.update(_check(_derived_version(APP_VERSION), candidate, True))
    after = service.managed.read()

    assert result.ok is False
    assert "failed" in result.message.lower()
    assert state.script.read_bytes() == previous
    assert after is not None and after.sha256 == state.sha256
    assert after.version == state.version


# ---------------------------------------------------------------------------
# 7. Failed installation
# ---------------------------------------------------------------------------


def test_failed_install_keeps_previous_managed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    previous = _source(APP_VERSION)
    state = _seed_managed(manager, previous, APP_VERSION)
    candidate = _source(_derived_version(APP_VERSION))
    service = _service(manager, state.script)
    _patch_bash(monkeypatch)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: candidate)

    real_install = setup_service.install_verified_ani_cli_payload

    def refusing_install(destination: Path, payload: bytes, **kwargs: Any):
        if Path(destination) == managed_root:
            return setup_service.InstallResult(False, "install refused", "no files written")
        return real_install(destination, payload, **kwargs)

    monkeypatch.setattr(
        setup_service, "install_verified_ani_cli_payload", refusing_install
    )
    result = service.update(_check(_derived_version(APP_VERSION), candidate, True))
    after = service.managed.read()

    assert result.ok is False
    assert "no files written" in result.detail
    assert state.script.read_bytes() == previous
    assert after is not None and after.sha256 == state.sha256
    assert after.version == state.version


# ---------------------------------------------------------------------------
# 8. Rollback after post-install verification failure
# ---------------------------------------------------------------------------


def test_rollback_restores_managed_script_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    previous = _source(APP_VERSION)
    state = _seed_managed(manager, previous, APP_VERSION)
    candidate = _source(_derived_version(APP_VERSION))
    service = _service(manager, state.script)
    _patch_bash(monkeypatch)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: candidate)

    real_install = setup_service.install_verified_ani_cli_payload

    def corrupting_install(destination: Path, payload: bytes, **kwargs: Any):
        result = real_install(destination, payload, **kwargs)
        if Path(destination) == managed_root and result.ok:
            (managed_root / "ani-cli").write_bytes(b"post-install corruption")
        return result

    monkeypatch.setattr(
        setup_service, "install_verified_ani_cli_payload", corrupting_install
    )
    result = service.update(_check(_derived_version(APP_VERSION), candidate, True))
    after = service.managed.read()
    record = manager.manifest.get(COMPONENT_ANI_CLI)

    assert result.ok is False
    assert "restored" in result.detail.lower()
    assert state.script.read_bytes() == previous
    assert after is not None and after.sha256 == state.sha256
    assert after.version == state.version
    assert record is not None and record.version == state.version


# ---------------------------------------------------------------------------
# 9. ANIXO application update preserves the managed copy
# ---------------------------------------------------------------------------


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def test_anixo_update_preserves_managed_ani_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    payload = _source(APP_VERSION)
    state = _seed_managed(manager, payload, APP_VERSION)

    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"old application")
    package = tmp_path / "ANIXO-Portable-next.zip"
    package.write_bytes(
        _zip_bytes({"ANIXO/Anixo.exe": b"new application"})
    )
    plan_path = tmp_path / "update-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": _derived_version(APP_VERSION),
                "channel": "stable",
                "notes": "",
                "published_at": "",
                "install_kind": "portable",
                "install_dir": str(install),
                "executable": str(install / "Anixo.exe"),
                "package_path": str(package),
                "asset_name": package.name,
                "asset_kind": "portable",
                "platform": "windows",
                "arch": "x86_64",
                "sha256": _sha(package.read_bytes()).lower(),
                "signature_status": "checksum verified",
                "signature_detail": "",
                "data_dir": str(tmp_path / "data"),
                "cache_dir": str(tmp_path / "cache"),
                "log_dir": str(tmp_path / "logs"),
                "config_path": str(tmp_path / "data" / "config.json"),
                "backup_dir": str(tmp_path / "data" / "updates" / "backups"),
                "source": "local-test-plan",
                "parent_pid": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater_helper, "_wait_for_parent", lambda plan: None)
    monkeypatch.setattr(updater_helper, "_health_check", lambda plan: "ok")
    monkeypatch.setattr(updater_helper, "_restart", lambda plan: None)

    result = updater_helper.apply_plan(plan_path)
    after = manager.managed_ani_cli.read()

    assert result["ok"] is True
    assert (install / "Anixo.exe").read_bytes() == b"new application"
    assert state.script.read_bytes() == payload
    assert after is not None and after.sha256 == state.sha256
    assert after.version == state.version
    assert (managed_root / METADATA_FILE).is_file()


# ---------------------------------------------------------------------------
# 10. Different ANIXO releases never change ownership
# ---------------------------------------------------------------------------


def test_release_parser_rejects_credential_bearing_urls() -> None:
    payload = {
        "tag_name": _derived_version(APP_VERSION),
        "assets": [
            {
                "name": "ani-cli",
                "browser_download_url": "https://user:password@example.invalid/ani-cli?sig=opaque",
                "digest": "sha256:" + "C" * 64,
            }
        ],
    }
    with pytest.raises(ValueError, match="clean HTTPS"):
        AniCliUpdateService._parse_release(payload)

    payload["assets"][0]["browser_download_url"] = (
        "https://example.invalid/ani-cli?X-Amz-Signature=opaque"
    )
    with pytest.raises(ValueError, match="clean HTTPS"):
        AniCliUpdateService._parse_release(payload)


def test_update_cache_never_persists_signed_urls(tmp_path: Path) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    service = _service(manager, None)
    check = AniCliUpdateCheck(
        APP_VERSION,
        _derived_version(APP_VERSION),
        True,
        "synthetic-time",
        "https://example.invalid/ani-cli?sig=opaque",
        "C" * 64,
        "",
    )

    service._write_cache(check)
    raw = service._cache_path.read_text(encoding="utf-8")
    cached = json.loads(raw)
    assert "opaque" not in raw
    assert cached["asset_url"] == ""


def test_backfill_never_infers_ownership_from_path_or_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    independent = tmp_path / "tools" / "ani-cli"
    independent.parent.mkdir(parents=True)
    payload = _source(APP_VERSION)
    independent.write_bytes(payload)
    monkeypatch.setattr(setup_service, "ANI_CLI_SHA256", _sha(payload))
    config = Config(ani_cli_path=str(independent))
    _, manager, _ = _manager(tmp_path, tmp_path / "persistent" / "ani-cli", config)

    assert manager.backfill_from_config() is False
    assert manager.manifest.get(COMPONENT_ANI_CLI) is None


def test_multiple_anixo_releases_keep_the_same_managed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "persistent" / "ani-cli"
    _, manager, _ = _manager(tmp_path, managed_root)
    payload = _source(APP_VERSION)
    state = _seed_managed(manager, payload, APP_VERSION)
    service = _service(manager, state.script)

    def forbidden_install(*args: Any, **kwargs: Any) -> setup_service.InstallResult:
        raise AssertionError("a different ANIXO release must not reinstall ani-cli")

    monkeypatch.setattr(setup_service, "install_ani_cli", forbidden_install)

    for release in (
        APP_VERSION,
        _derived_version(APP_VERSION),
        _derived_version(APP_VERSION, 2),
    ):
        monkeypatch.setattr(setup_service, "APP_VERSION", release)
        detected = service.detect_installed()
        ensured = manager.ensure_managed_ani_cli()
        current = service.managed.read()

        assert detected.owned_by_anixo is True
        assert detected.path == str(state.script)
        assert ensured.ok is True
        assert ensured.install_location == str(state.script)
        assert current is not None and current.sha256 == state.sha256
        assert state.script.read_bytes() == payload
