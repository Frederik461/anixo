from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app import AnixoApp
from config import Config, ConfigStore
from platforms import MACOS, PlatformAdapter
from screens.dashboard import DashboardScreen
from services import setup as setup_service
from services.dependency_manager import DependencyManager
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    COMPONENT_APP,
    COMPONENT_MPV,
    InstallRecord,
    ManifestStore,
)
from services.uninstall import UninstallPlanner


def _clean_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for name in (
        "ANIXO_DATA_DIR",
        "ANIME_HUB_DATA_DIR",
        "ANIXO_CONFIG",
        "ANIME_HUB_CONFIG",
        "ANIXO_CACHE_DIR",
        "ANIXO_LOG_DIR",
        "ANIXO_ANI_CLI_DIR",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HOME",
        "USERPROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_macos_app_starts_and_quits_cleanly(tmp_path: Path) -> None:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    app = AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            ani_cli_history_path=str(history),
        ),
        data_dir=tmp_path / "data",
    )

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("q")

    asyncio.run(run())
    assert not app.is_running


def test_macos_dependency_install_records_macos_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    manager = DependencyManager(Config(), data, cache, PlatformAdapter(MACOS))
    script = manager.managed_ani_cli.script_path
    payload = b"official bytes"
    digest = hashlib.sha256(payload).hexdigest().upper()

    def fake_install(destination: Path, *, overwrite: bool = False):
        assert overwrite is False
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
    result = manager.install("ani-cli", setup_service.DependencyStatus(
        key="ani-cli",
        label="ani-cli",
        description="test",
        required=True,
        installed=False,
        version="not found",
        detail="",
        official_url=setup_service.ANI_CLI_URL,
        winget_id=None,
        installable=True,
    ))
    assert result.ok
    record = manager.manifest.get(COMPONENT_ANI_CLI)
    assert record is not None
    assert record.platform == "macos"
    assert record.installed_by_anixo is True
    assert record.safe_to_remove is True
    assert (data / "install-manifest-macos.json").exists()


def test_macos_never_uses_winget(tmp_path: Path) -> None:
    manager = DependencyManager(
        Config(), tmp_path / "data", tmp_path / "cache", PlatformAdapter(MACOS)
    )
    status = setup_service.DependencyStatus(
        key="mpv",
        label="mpv",
        description="test",
        required=False,
        installed=False,
        version="not found",
        detail="",
        official_url="https://example.invalid",
        winget_id="mpv.io.mpv",
        installable=True,
    )
    result = manager.install("mpv", status)
    assert result.ok is False
    assert "not available on this OS" in result.message


def _record(
    key: str,
    name: str,
    path: Path,
    *,
    owned: bool = True,
    config_keys: tuple[str, ...] = (),
) -> InstallRecord:
    return InstallRecord(
        key=key,
        name=name,
        platform="macos",
        installed_by_anixo=owned,
        managed=owned,
        safe_to_remove=owned,
        install_location=str(path),
        version="0.1.0-beta.5",
        installed_at="2026-01-01T00:00:00+00:00",
        source="test",
        install_method="test",
        paths=(str(path),),
        config_keys=config_keys,
    )


def _uninstall_case(tmp_path: Path) -> dict:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    logs = tmp_path / "logs"
    unrelated = tmp_path / "unrelated"
    app_root = tmp_path / "Applications" / "ANIXO.app"
    managed_dep = cache / "tools" / "ani-cli"
    preexisting = unrelated / "preexisting-mpv"
    data_sentinel = data / "sentinel.txt"
    outside_sentinel = unrelated / "outside-sentinel.txt"

    for directory in (data, cache / "tools", logs, unrelated, app_root / "Contents"):
        directory.mkdir(parents=True, exist_ok=True)
    (app_root / "Contents" / "Anixo").write_bytes(b"app")
    managed_dep.write_text("official", encoding="utf-8")
    preexisting.write_bytes(b"other app")
    data_sentinel.write_text("user data", encoding="utf-8")
    outside_sentinel.write_text("unrelated", encoding="utf-8")

    adapter = PlatformAdapter(MACOS)
    store = ManifestStore(data, adapter)
    manifest = store.new_manifest("macos-dmg")
    manifest.upsert(_record(COMPONENT_APP, "ANIXO", app_root))
    manifest.upsert(
        _record(
            COMPONENT_ANI_CLI,
            "ani-cli",
            managed_dep,
            config_keys=("ani_cli_path",),
        )
    )
    manifest.upsert(_record(COMPONENT_MPV, "pre-existing mpv", preexisting, owned=False))
    store.save(manifest)

    config_store = ConfigStore(path=data / "config.json", data_dir=data)
    config_store.save(Config(ani_cli_path=str(managed_dep)))
    planner = UninstallPlanner(
        data_dir=data,
        cache_dir=cache,
        log_dir=logs,
        store=store,
        adapter=adapter,
        config_store=config_store,
        trusted_roots=(tmp_path,),
    )
    return {
        "planner": planner,
        "store": store,
        "app_root": app_root,
        "managed_dep": managed_dep,
        "preexisting": preexisting,
        "data": data,
        "data_sentinel": data_sentinel,
        "outside_sentinel": outside_sentinel,
        "config_store": config_store,
    }


def test_macos_app_only_uninstall_preserves_data_and_unmanaged_files(
    tmp_path: Path,
) -> None:
    context = _uninstall_case(tmp_path)
    report = context["planner"].execute(context["planner"].plan("app_only"))
    assert report.success, report.failed

    assert not context["app_root"].exists()
    assert not context["managed_dep"].exists()
    assert not context["store"].path.exists()
    assert context["data"].exists()
    assert context["data_sentinel"].exists()
    assert context["outside_sentinel"].exists()
    assert context["preexisting"].exists()
    assert context["config_store"].load().ani_cli_path == ""
    assert json.loads(
        (context["data"] / "config.json").read_text(encoding="utf-8")
    )


def test_macos_complete_uninstall_removes_data_but_keeps_outside_files(
    tmp_path: Path,
) -> None:
    context = _uninstall_case(tmp_path)
    report = context["planner"].execute(context["planner"].plan("complete"))
    assert report.success, report.failed

    assert not context["app_root"].exists()
    assert not context["managed_dep"].exists()
    assert not context["data"].exists()
    assert not context["data_sentinel"].exists()
    assert context["outside_sentinel"].exists()
    assert context["preexisting"].exists()
