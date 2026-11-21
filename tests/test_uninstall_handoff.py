from __future__ import annotations

import asyncio
import os
from pathlib import Path

import app as app_module
from app import AnixoApp
from config import Config
from services.uninstall import read_pending


def _app(tmp_path: Path) -> AnixoApp:
    return AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "data",
    )


def test_uninstall_handoff_writes_pending_starts_uninstaller_and_exits(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path)
    calls: list[tuple[object, object]] = []

    def fake_popen(command, **options):
        calls.append((command, options))

    monkeypatch.setattr(app_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(app, "notify_user", lambda _message: None)
    monkeypatch.setattr(app, "exit", lambda: calls.append(("exit", {})))

    asyncio.run(app.action_uninstall("complete", {"app": True, "settings": True}))

    pending = read_pending(app.data_dir)
    assert pending["mode"] == "complete"
    assert pending["wait_for_pid"] == os.getpid()
    assert pending["selections"]["settings"] is True
    assert pending["data_dir"] == str(app.data_dir.resolve())
    assert pending["cache_dir"] == str(app.cache_dir.resolve())
    assert pending["log_dir"] == str(app.log_dir.resolve())
    assert pending["config_path"] == str(app.config_path.resolve())

    command = calls[0][0]
    assert command[-1] == "--execute-pending"  # type: ignore[index]
    assert str(command[1]).endswith("uninstaller.py")  # type: ignore[index]
    assert command[2:10] == (
        "--data-dir",
        str(app.data_dir),
        "--cache-dir",
        str(app.cache_dir),
        "--log-dir",
        str(app.log_dir),
        "--config",
        str(app.config_path),
    )  # type: ignore[index]
    assert calls[0][1]["cwd"] == str(app.install_dir)  # type: ignore[index]
    assert ("exit", {}) in calls


def test_packaged_uninstaller_is_preferred(tmp_path: Path) -> None:
    app = _app(tmp_path)
    install_dir = tmp_path / "Programs" / "ANIXO"
    install_dir.mkdir(parents=True)
    executable = install_dir / (
        "ANIXO-Uninstaller.exe" if app.platform.is_windows else "ANIXO-Uninstaller"
    )
    executable.write_bytes(b"uninstaller")
    app.install_dir = install_dir

    assert app._uninstaller_command()[:1] == (str(executable),)
    assert app._uninstaller_command()[-1] == "--execute-pending"
