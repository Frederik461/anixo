from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from config import Config, ConfigStore
from platforms import WINDOWS, PlatformAdapter
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    COMPONENT_APP,
    COMPONENT_MPV,
    InstallRecord,
    ManifestStore,
)
from services.uninstall import (
    UninstallPlanner,
    UninstallSafetyError,
    read_pending,
    write_pending,
)

_ADAPTER = PlatformAdapter(WINDOWS)


def _record(
    key: str,
    name: str,
    location: Path,
    *,
    owned: bool = True,
    config_keys: tuple[str, ...] = (),
    commands: tuple[tuple[str, ...], ...] = (),
) -> InstallRecord:
    return InstallRecord(
        key=key,
        name=name,
        platform="windows",
        installed_by_anixo=owned,
        managed=owned,
        safe_to_remove=owned,
        install_location=str(location),
        version="v5.1",
        installed_at="2026-11-21T00:00:00+00:00",
        source="test",
        install_method="test",
        paths=(str(location),),
        uninstall_commands=commands,
        config_keys=config_keys,
    )


def _setup(tmp_path: Path, *, include_unmanaged: bool = False) -> dict:
    app_root = tmp_path / "Programs" / "ANIXO"
    app_root.mkdir(parents=True)
    (app_root / "Anixo.exe").write_bytes(b"app")
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    logs = tmp_path / "logs"
    for root in (data, cache, logs):
        root.mkdir(parents=True)
    tools = cache / "tools"
    tools.mkdir()
    script = tools / "ani-cli"
    script.write_text("official", encoding="utf-8")

    store = ManifestStore(data, _ADAPTER)
    manifest = store.new_manifest("windows-installer")
    manifest.upsert(_record(COMPONENT_APP, "ANIXO", app_root))
    manifest.upsert(
        _record(
            COMPONENT_ANI_CLI,
            "ani-cli",
            script,
            config_keys=("ani_cli_path",),
        )
    )
    if include_unmanaged:
        preexisting = tmp_path / "preexisting" / "mpv.exe"
        preexisting.parent.mkdir(parents=True)
        preexisting.write_bytes(b"other app")
        manifest.upsert(_record(COMPONENT_MPV, "mpv", preexisting, owned=False))
    store.save(manifest)

    config_store = ConfigStore(path=data / "config.json", data_dir=data)
    config_store.save(Config(ani_cli_path=str(script)))
    planner = UninstallPlanner(
        data_dir=data,
        cache_dir=cache,
        log_dir=logs,
        store=store,
        adapter=_ADAPTER,
        config_store=config_store,
        trusted_roots=(tmp_path,),
    )
    return {
        "planner": planner,
        "store": store,
        "app_root": app_root,
        "data": data,
        "cache": cache,
        "logs": logs,
        "script": script,
        "config_store": config_store,
    }


def test_plan_app_only_preserves_data_and_annotates_unmanaged(tmp_path: Path) -> None:
    context = _setup(tmp_path, include_unmanaged=True)
    plan = context["planner"].plan("app_only")
    categories = {item.category for item in plan.items}
    assert "settings" not in categories
    assert any(item.key == "app" for item in plan.items)
    assert any(item.key == f"dependency:{COMPONENT_ANI_CLI}" for item in plan.items)
    assert any(item.key == f"config:ani_cli_path" for item in plan.items)
    assert any("mpv" in warning and "not installed by ANIXO" in warning for warning in plan.warnings)
    assert plan.total_size() > 0


def test_plan_does_not_mutate_filesystem(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    plan = context["planner"].plan("app_only")
    assert context["app_root"].exists()
    assert context["script"].exists()
    assert context["store"].path.exists()
    assert plan.items


def test_complete_plan_includes_personal_data(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    plan = context["planner"].plan("complete")
    data_items = plan.by_category("settings")
    assert len(data_items) == 1
    assert data_items[0].target == str(context["data"].resolve())


def test_app_only_execution_removes_owned_files_and_keeps_data(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    plan = context["planner"].plan("app_only")
    report = context["planner"].execute(plan)
    assert report.success, report.failed
    assert not context["app_root"].exists()
    assert not context["script"].exists()
    assert not context["cache"].exists()
    assert context["data"].exists()
    assert context["config_store"].load().ani_cli_path == ""
    assert not context["store"].path.exists()
    assert report.log_path


def test_execute_reports_progress_for_each_selected_item(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    plan = context["planner"].plan("app_only")
    observed: list[tuple[int, int, str, str]] = []

    def progress(completed: int, total: int, item: object, error: str) -> None:
        observed.append((completed, total, item.key, error))  # type: ignore[attr-defined]

    report = context["planner"].execute(plan, progress=progress)
    assert report.success, report.failed
    assert observed
    assert [entry[0] for entry in observed] == list(range(1, len(plan.selected_items) + 1))
    assert {entry[1] for entry in observed} == {len(plan.selected_items)}
    assert all(entry[3] == "" for entry in observed)


def test_complete_execution_removes_data_root(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    report = context["planner"].execute(context["planner"].plan("complete"))
    assert report.success, report.failed
    assert not context["data"].exists()
    assert not context["app_root"].exists()


def test_unowned_dependency_survives_and_is_reported(tmp_path: Path) -> None:
    context = _setup(tmp_path, include_unmanaged=True)
    preexisting = tmp_path / "preexisting" / "mpv.exe"
    report = context["planner"].execute(context["planner"].plan("app_only"))
    assert report.success
    assert preexisting.exists()
    assert not any("mpv" in item for item in report.removed)
    assert any("mpv" in warning for warning in report.plan.warnings)


def test_outside_trusted_root_is_not_scheduled(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    manifest = context["store"].load().manifest
    record = manifest.get(COMPONENT_APP)
    assert record is not None
    outside = Path("C:/Outside/ANIXO")
    from dataclasses import replace

    manifest.upsert(replace(record, install_location=str(outside), paths=(str(outside),)))
    context["store"].save(manifest)
    planner = UninstallPlanner(
        data_dir=context["data"],
        cache_dir=context["cache"],
        log_dir=context["logs"],
        store=context["store"],
        adapter=_ADAPTER,
        trusted_roots=(tmp_path,),
    )
    plan = planner.plan("app_only")
    assert not any(item.key == "app" for item in plan.items)
    assert any("outside the trusted install root" in warning for warning in plan.warnings)


def test_missing_manifest_refuses_uninstall(tmp_path: Path) -> None:
    planner = UninstallPlanner(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        store=ManifestStore(tmp_path / "data", _ADAPTER),
        adapter=_ADAPTER,
    )
    with pytest.raises(UninstallSafetyError, match="No ANIXO installation manifest"):
        planner.plan("app_only")


def test_corrupt_manifest_refuses_uninstall(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = ManifestStore(data, _ADAPTER)
    store.path.write_text("not json", encoding="utf-8")
    planner = UninstallPlanner(
        data_dir=data,
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        store=store,
        adapter=_ADAPTER,
    )
    with pytest.raises(UninstallSafetyError, match="quarantined"):
        planner.plan("app_only")


def test_failed_removal_is_reported_and_manifest_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.uninstall as uninstall_module

    context = _setup(tmp_path)

    def boom(path, *args, **kwargs):  # pragma: no cover - invoked by test
        raise OSError("locked")

    monkeypatch.setattr(uninstall_module.shutil, "rmtree", boom)
    report = context["planner"].execute(context["planner"].plan("app_only"))
    assert report.success is False
    assert report.failed
    assert context["store"].path.exists()
    assert report.manual_steps()


def test_winget_command_is_planned_and_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _setup(tmp_path)
    store = context["store"]
    manifest = store.load().manifest
    manifest.upsert(
        _record(
            COMPONENT_MPV,
            "mpv",
            Path("winget://mpv.io.mpv"),
            commands=(("winget", "uninstall", "--id", "mpv.io.mpv", "--scope", "user"),),
        )
    )
    store.save(manifest)
    planner = UninstallPlanner(
        data_dir=context["data"],
        cache_dir=context["cache"],
        log_dir=context["logs"],
        store=store,
        adapter=_ADAPTER,
        config_store=context["config_store"],
        trusted_roots=(tmp_path,),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(uninstall_module_subprocess(), "run", fake_run)
    plan = planner.plan("app_only")
    assert any(item.kind == "command" for item in plan.items)
    report = planner.execute(plan)
    assert report.success
    assert any("winget" in call for call in calls)


def uninstall_module_subprocess():
    import services.uninstall as module

    return module.subprocess


def test_pending_request_round_trip(tmp_path: Path) -> None:
    path = write_pending(
        tmp_path,
        mode="complete",
        selections={"include_app": True, "include_settings": True},
        wait_for_pid=1234,
    )
    payload = read_pending(tmp_path)
    assert path.exists()
    assert payload["mode"] == "complete"
    assert payload["wait_for_pid"] == 1234
    assert payload["selections"]["include_settings"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
