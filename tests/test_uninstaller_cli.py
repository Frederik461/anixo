from __future__ import annotations

from pathlib import Path

import pytest

from config import Config, ConfigStore
from platforms import get_platform_adapter
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    COMPONENT_APP,
    COMPONENT_MPV,
    InstallRecord,
    ManifestStore,
)
import services.uninstall as uninstall_module
import uninstaller


def _manifest(tmp_path: Path) -> tuple[ManifestStore, Path, Path, Path, Path, Path]:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    logs = tmp_path / "logs"
    app = tmp_path / "Programs" / "ANIXO"
    unrelated = tmp_path / "unrelated"
    for root in (data, cache, logs, app, unrelated):
        root.mkdir(parents=True)
    script = cache / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    script.write_text("official", encoding="utf-8")
    preexisting = unrelated / "preexisting-mpv"
    preexisting.write_bytes(b"installed by somebody else")
    outside_sentinel = unrelated / "outside-sentinel.txt"
    outside_sentinel.write_text("unrelated personal file", encoding="utf-8")

    # The standalone CLI always uses the active platform adapter, so the
    # fixture must write the same manifest filename and platform identifier.
    adapter = get_platform_adapter()
    store = ManifestStore(data, adapter)
    manifest = store.new_manifest(f"{adapter.id}-installer")
    manifest.upsert(
        InstallRecord(
            key=COMPONENT_APP,
            name="ANIXO",
            platform=adapter.id,
            installed_by_anixo=True,
            managed=True,
            safe_to_remove=True,
            install_location=str(app),
            version="0.1.0-beta.4",
            installed_at="2026-11-21T00:00:00+00:00",
            source="test",
            install_method="test",
            paths=(str(app),),
        )
    )
    manifest.upsert(
        InstallRecord(
            key=COMPONENT_ANI_CLI,
            name="ani-cli",
            platform=adapter.id,
            installed_by_anixo=True,
            managed=True,
            safe_to_remove=True,
            install_location=str(script),
            version="v5.1",
            installed_at="2026-11-21T00:00:00+00:00",
            source="test",
            install_method="test",
            paths=(str(script),),
            config_keys=("ani_cli_path",),
        )
    )
    manifest.upsert(
        InstallRecord(
            key=COMPONENT_MPV,
            name="pre-existing mpv",
            platform=adapter.id,
            installed_by_anixo=False,
            managed=False,
            safe_to_remove=False,
            install_location=str(preexisting),
            version="unknown",
            installed_at="2020-01-01T00:00:00+00:00",
            source="observed",
            install_method="pre-existing",
            paths=(str(preexisting),),
        )
    )
    store.save(manifest)
    ConfigStore(path=data / "config.json", data_dir=data).save(
        Config(ani_cli_path=str(script))
    )
    return store, data, cache, logs, app, unrelated


@pytest.fixture(autouse=True)
def _trust_tmp_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        uninstall_module.UninstallPlanner,
        "_is_trusted",
        lambda self, path, portable=False: True,
    )


def test_parser_accepts_all_uninstall_options() -> None:
    parser = uninstaller.build_parser()
    args = parser.parse_args(
        [
            "--mode",
            "complete",
            "--data-dir",
            "D",
            "--cache-dir",
            "C",
            "--log-dir",
            "L",
            "--config",
            "C.json",
            "--exclude",
            "cache",
            "--include",
            "settings",
            "--dry-run",
            "--yes",
            "--json",
            "--execute-pending",
        ]
    )
    assert args.mode == "complete"
    assert args.exclude == ["cache"]
    assert args.include == ["settings"]
    assert args.config == Path("C.json")
    assert args.dry_run and args.yes and args.json and args.execute_pending


def test_dry_run_prints_plan_without_deleting(tmp_path: Path, capsys) -> None:
    _store, data, cache, _logs, app, unrelated = _manifest(tmp_path)
    code = uninstaller.main(
        [
            "--mode",
            "app-only",
            "--data-dir",
            str(data),
            "--cache-dir",
            str(cache),
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Dry run only" in output
    assert "Remove ANIXO only" not in output  # screen text is not CLI output
    assert app.exists()
    assert (cache / "tools" / "ani-cli").exists()
    assert (unrelated / "outside-sentinel.txt").exists()


def test_missing_manifest_returns_safety_exit_code(tmp_path: Path, capsys) -> None:
    code = uninstaller.main(["--data-dir", str(tmp_path / "empty"), "--yes"])
    assert code == 2
    assert "No ANIXO installation manifest" in capsys.readouterr().err


def test_invalid_manifest_returns_safety_exit_code(tmp_path: Path, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = ManifestStore(data, get_platform_adapter())
    store.path.write_text("{ this is not valid JSON", encoding="utf-8")
    code = uninstaller.main(["--data-dir", str(data), "--yes"])
    assert code == 2
    assert "manifest" in capsys.readouterr().err.lower()


def test_app_only_cli_preserves_data(tmp_path: Path, capsys) -> None:
    _store, data, cache, _logs, app, unrelated = _manifest(tmp_path)
    code = uninstaller.main(
        ["--mode", "app-only", "--data-dir", str(data), "--cache-dir", str(cache), "--yes"]
    )
    assert code == 0
    assert "Uninstall complete." in capsys.readouterr().out
    assert not app.exists()
    assert not (cache / "tools" / "ani-cli").exists()
    assert data.exists()
    assert (data / "config.json").exists()
    assert not (
        data / f"install-manifest-{get_platform_adapter().id}.json"
    ).exists()
    assert (unrelated / "preexisting-mpv").exists()
    assert (unrelated / "outside-sentinel.txt").exists()


def test_complete_cli_deletes_data(tmp_path: Path, capsys) -> None:
    _store, data, cache, _logs, app, unrelated = _manifest(tmp_path)
    code = uninstaller.main(
        ["--mode", "complete", "--data-dir", str(data), "--cache-dir", str(cache), "--yes"]
    )
    assert code == 0
    assert not data.exists()
    assert not app.exists()
    assert (unrelated / "preexisting-mpv").exists()
    assert (unrelated / "outside-sentinel.txt").exists()


def test_pending_request_restores_custom_paths(tmp_path: Path, capsys) -> None:
    _store, data, _cache, _logs, _app, _unrelated = _manifest(tmp_path)
    alternate_cache = tmp_path / "alternate-cache"
    alternate_logs = tmp_path / "alternate-logs"
    alternate_config = tmp_path / "alternate-config.json"
    alternate_cache.mkdir()
    alternate_logs.mkdir()
    (alternate_cache / "marker.txt").write_text("owned", encoding="utf-8")
    (alternate_logs / "session.log").write_text("owned", encoding="utf-8")
    ConfigStore(path=alternate_config, data_dir=data).save(
        Config(ani_cli_path=str(_cache / "tools" / "ani-cli"))
    )
    uninstall_module.write_pending(
        data,
        mode="app_only",
        selections={"app": True, "dependencies": True, "cache": True, "settings": False},
        cache_dir=alternate_cache,
        log_dir=alternate_logs,
        config_path=alternate_config,
    )

    code = uninstaller.main(
        ["--execute-pending", "--data-dir", str(data), "--yes", "--exclude", "app"]
    )
    assert code == 0, capsys.readouterr().err
    assert not alternate_cache.exists()
    assert not alternate_logs.exists()
    assert not (data / uninstall_module.PENDING_FILENAME).exists()
    assert data.exists()
    assert (data / "config.json").exists()
    assert alternate_config.is_file()
    assert ConfigStore(path=alternate_config, data_dir=data).load().ani_cli_path == ""


def test_json_output_is_machine_readable(tmp_path: Path, capsys) -> None:
    _store, data, cache, _logs, _app, _unrelated = _manifest(tmp_path)
    code = uninstaller.main(
        [
            "--mode",
            "app-only",
            "--data-dir",
            str(data),
            "--cache-dir",
            str(cache),
            "--dry-run",
            "--json",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert '"mode": "app_only"' in output
    assert '"success"' not in output  # dry run emits a plan, not a report
