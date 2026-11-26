"""Phase S safety tests for the ANIXO update flow.

These tests are deterministic and cross-platform.  They use in-memory HTTP
fakes and temporary install/data directories; no test touches a real install,
creates a release, or downloads an installer.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app import AnixoApp
from config import Config
from platforms import WINDOWS, PlatformAdapter
from services import updater as updater_module
from services.update_log import redact_update_text
from services.updater import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateCheck,
    UpdateService,
    detect_target,
    is_trusted_asset_url,
)
from version import APP_VERSION
import updater_helper


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


class _WindowsOsProxy:
    """An ``os`` stand-in that reports Windows without changing pathlib.

    Patching the real ``os.name`` also switches ``pathlib.Path`` to
    ``WindowsPath`` on macOS, which breaks POSIX temporary paths.  Only the
    updater helper's view of the platform needs to change here.
    """

    def __init__(self, real: Any, name: str) -> None:
        self._real = real
        self.name = name

    def __getattr__(self, item: str) -> Any:
        return getattr(self._real, item)


def _windows_os() -> _WindowsOsProxy:
    return _WindowsOsProxy(os, "nt")


def _higher_version() -> str:
    """Return a clearly newer version derived from the installed version."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", APP_VERSION)
    assert match is not None
    return f"{int(match.group(1)) + 100}.{match.group(2)}.{match.group(3)}"


def _service(tmp_path: Path, *, feed: str = "", channel: str = "stable") -> UpdateService:
    config = Config()
    config.update_channel = channel
    config.update_feed_url = feed
    config.extra["update_repo"] = "test/anixo"
    config.check_for_app_updates = False
    config.update_public_key = ""
    return UpdateService(
        config,
        data_dir=tmp_path / "data",
        adapter=PlatformAdapter(WINDOWS),
        target=detect_target("Windows", "AMD64"),
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        location: str = "",
        url: str = "",
        payload: Any = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.url = url
        self._payload = payload if payload is not None else []
        self._body = body

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeAsyncClient:
    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append((url, dict(headers or {})))
        return self.routes[url]

    def stream(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> _FakeStream:
        self.calls.append((url, dict(headers or {})))
        return _FakeStream(self.routes[url])


def _patch_client(monkeypatch: Any, client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(
        updater_module.httpx, "AsyncClient", lambda *args, **kwargs: client
    )


def _write_plan(
    tmp_path: Path,
    install: Path,
    package: Path,
    *,
    platform: str = "windows",
    arch: str = "x86_64",
    parent_pid: int = 0,
) -> Path:
    payload = {
        "schema_version": updater_helper.SCHEMA_VERSION,
        "app_name": "ANIXO",
        "version": APP_VERSION,
        "channel": "stable",
        "notes": "",
        "published_at": "",
        "install_kind": "portable",
        "install_dir": str(install),
        "executable": str(install / "Anixo.exe"),
        "package_path": str(package),
        "asset_name": package.name,
        "asset_kind": "portable",
        "platform": platform,
        "arch": arch,
        "sha256": _sha(package.read_bytes()),
        "signature_status": "checksum verified",
        "signature_detail": "",
        "data_dir": str(tmp_path / "data"),
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "config_path": str(tmp_path / "data" / "config.json"),
        "backup_dir": str(tmp_path / "data" / "updates" / "backups"),
        "source": "feed",
        "parent_pid": parent_pid,
        "parent_started_epoch": time.time() if parent_pid else 0.0,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _log_lines(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "updates" / "update.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# Trusted hosts and redirects


def test_trusted_hosts_come_from_config_and_include_github_cdns(tmp_path: Path) -> None:
    hosts = set(Config().update_trusted_hosts)
    assert {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "githubusercontent.com",
    } <= hosts
    service = _service(tmp_path)
    assert hosts <= service.trusted_hosts
    assert is_trusted_asset_url(
        "https://objects.githubusercontent.com/release/package.exe", hosts
    )
    assert is_trusted_asset_url(
        "https://assets.githubusercontent.com/release/package.zip", hosts
    )


def test_real_style_github_to_cdn_redirect_stays_trusted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    service = _service(tmp_path)
    api = "https://api.github.com/repos/test/anixo/releases"
    github = "https://github.com/test/anixo/releases/download/asset"
    cdn = "https://objects.githubusercontent.com/release/asset"
    client = _FakeAsyncClient(
        {
            api: _FakeResponse(302, location=github, url=api),
            github: _FakeResponse(307, location=cdn, url=github),
            cdn: _FakeResponse(200, url=cdn, payload=[]),
        }
    )
    _patch_client(monkeypatch, client)
    assert asyncio.run(service._fetch_json(api)) == []
    assert [url for url, _ in client.calls] == [api, github, cdn]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("HTTPS://api.github.com/repos/test/anixo/releases", True),
        ("https://api.github.com:443/repos/test/anixo/releases", True),
        ("https://api.github.com:8443/repos/test/anixo/releases", False),
        ("https://github.com.evil.test/asset.exe", False),
        ("https://evilgithub.com/asset.exe", False),
        ("https://github.com@evil.test/asset.exe", False),
        ("https://user:placeholder@github.com/asset.exe", False),
        ("http://objects.githubusercontent.com/asset.exe", False),
    ],
)
def test_host_matching_is_exact_or_dot_boundary(url: str, allowed: bool) -> None:
    hosts = set(Config().update_trusted_hosts)
    assert is_trusted_asset_url(url, hosts) is allowed


def test_redirect_to_http_or_untrusted_host_is_rejected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    service = _service(tmp_path)
    start = "https://api.github.com/repos/test/anixo/releases"
    for location in ("http://objects.githubusercontent.com/asset", "https://evil.test/asset"):
        client = _FakeAsyncClient(
            {start: _FakeResponse(302, location=location, url=start)}
        )
        _patch_client(monkeypatch, client)
        with pytest.raises(ValueError, match="outside the trusted"):
            asyncio.run(service._fetch_json(start))
        assert len(client.calls) == 1


def test_redirect_hop_limit_is_enforced(tmp_path: Path, monkeypatch: Any) -> None:
    service = _service(tmp_path)
    url = "https://api.github.com/repos/test/anixo/releases"
    client = _FakeAsyncClient({url: _FakeResponse(302, location=url, url=url)})
    _patch_client(monkeypatch, client)
    with pytest.raises(ValueError, match="redirect limit"):
        asyncio.run(service._fetch_json(url))
    assert len(client.calls) == updater_module._MAX_REDIRECTS + 1


def test_cross_host_redirect_drops_auth_headers(monkeypatch: Any) -> None:
    start = "https://api.github.com/repos/test/anixo/releases"
    cdn = "https://objects.githubusercontent.com/release/asset"
    client = _FakeAsyncClient(
        {
            start: _FakeResponse(302, location=cdn, url=start),
            cdn: _FakeResponse(200, url=cdn, payload=[]),
        }
    )
    _patch_client(monkeypatch, client)
    headers = {
        "User-Agent": "ANIXO-test",
        "Authorization": "Bearer test-value",
        "X-Amz-Security-Token": "test-value",
    }
    asyncio.run(
        updater_module._fetch_response(
            start, headers=headers, trusted_hosts=set(Config().update_trusted_hosts)
        )
    )
    assert "authorization" not in {key.lower() for key in client.calls[1][1]}
    assert not any(key.lower().startswith("x-amz") for key in client.calls[1][1])
    assert client.calls[1][1]["User-Agent"] == "ANIXO-test"


# ---------------------------------------------------------------------------
# Channel enforcement and redaction


def _feed_payload(version: str, channel: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": version,
        "channel": channel,
        "published_at": "",
        "notes": "test feed",
        "assets": [
            {
                "name": f"ANIXO-Setup-v{version}.exe",
                "url": "https://feed.test/ANIXO-Setup-v{version}.exe".format(
                    version=version
                ),
                "sha256": _sha(b"asset"),
                "platform": "windows",
                "arch": "x86_64",
                "kind": "installer",
            }
        ],
    }


def test_stable_rejects_dashless_beta_manifest(tmp_path: Path, monkeypatch: Any) -> None:
    service = _service(tmp_path, feed="https://feed.test/manifest.json")
    payload = _feed_payload(_higher_version(), "beta")

    async def fake_fetch(url: str) -> Any:
        return payload

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert check.update_available is False
    assert check.release is None


def test_beta_user_accepts_beta_manifest(tmp_path: Path, monkeypatch: Any) -> None:
    version = _higher_version()
    service = _service(
        tmp_path, feed="https://feed.test/manifest.json", channel="beta"
    )
    payload = _feed_payload(version, "beta")

    async def fake_fetch(url: str) -> Any:
        return payload

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert check.update_available is True
    assert check.release is not None and check.release.manifest is not None
    assert check.release.manifest.channel == "beta"


def test_redaction_covers_signed_query_bearer_and_userinfo() -> None:
    text = (
        "failed GET https://cdn.test/file?sig=test-value&se=123"
        "&X-Amz-Signature=test-value Authorization: Bearer test-value "
        "https://user:placeholder@cdn.test/file"
    )
    redacted = redact_update_text(text)
    assert "sig=<redacted>" in redacted
    assert "se=<redacted>" in redacted
    assert "x-amz-signature=<redacted>".lower() in redacted.lower()
    assert "Bearer <redacted>" in redacted
    assert "https://<redacted>@cdn.test/file" in redacted
    assert "test-value" not in redacted


def test_app_status_is_redacted(tmp_path: Path) -> None:
    app = AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            check_for_app_updates=False,
        ),
        data_dir=tmp_path / "data",
    )
    app._set_update_status(
        "failed: https://cdn.test/x?sig=test-value Bearer test-value"
    )
    assert "test-value" not in app.update_state.status
    assert "<redacted>" in app.update_state.status


def test_failed_result_marker_is_surfaced_after_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app = AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            check_for_app_updates=False,
        ),
        data_dir=tmp_path / "data",
    )
    messages: list[str] = []
    monkeypatch.setattr(
        app,
        "notify_user",
        lambda message, *, timeout=3: messages.append(message),  # type: ignore[arg-type]
    )
    marker = tmp_path / "data" / "updates" / "last-result.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "ok": False,
                "version": APP_VERSION,
                "message": "Update failed",
                "detail": "Bearer test-value during https://cdn.test/x?sig=test-value",
                "restored": True,
            }
        ),
        encoding="utf-8",
    )
    asyncio.run(app._notify_applied_update())
    assert app.update_state.status.startswith("UPDATE FAILED")
    assert "update.log" in app.update_state.status
    assert all("test-value" not in item for item in messages)
    assert marker.exists()


# ---------------------------------------------------------------------------
# Helper rollback, roots, and parent liveness


def test_rollback_removes_extra_managed_files_and_preserves_user_data(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updater_helper, "_wait_for_parent", lambda plan: None)
    monkeypatch.setattr(updater_helper, "_restart", lambda plan: None)

    def fail_health(plan: dict[str, Any]) -> str:
        raise updater_helper.UpdateHelperError("health check failed")

    monkeypatch.setattr(updater_helper, "_health_check", fail_health)
    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"old app")
    (install / "history.db").write_bytes(b"history")
    (install / "config.json").write_text("keep config", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    package = tmp_path / f"ANIXO-Portable-v{APP_VERSION}.zip"
    package.write_bytes(
        _zip_bytes(
            {
                "ANIXO/Anixo.exe": b"new app",
                "ANIXO/new-only.dll": b"new file",
                "ANIXO/config.json": b"must not overwrite",
            }
        )
    )
    result = updater_helper.apply_plan(_write_plan(tmp_path, install, package))
    assert result["ok"] is False
    assert result["restored"] is True
    assert (install / "Anixo.exe").read_bytes() == b"old app"
    assert (install / "history.db").read_bytes() == b"history"
    assert (install / "config.json").read_text(encoding="utf-8") == "keep config"
    assert not (install / "new-only.dll").exists()
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "outside"
    logged = _log_lines(tmp_path / "data")
    delete_event = next(
        item for item in logged if item["event"] == "rollback_delete_list"
    )
    assert "new-only.dll" in delete_event["files"]
    assert "history.db" not in delete_event["files"]
    assert "config.json" not in delete_event["files"]


def test_helper_refuses_install_root_overlapping_user_data(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updater_helper, "_wait_for_parent", lambda plan: None)
    monkeypatch.setattr(updater_helper, "_restart", lambda plan: None)
    monkeypatch.setattr(updater_helper, "_health_check", lambda plan: "ok")
    install = tmp_path
    package = tmp_path / f"ANIXO-Portable-v{APP_VERSION}.zip"
    package.write_bytes(_zip_bytes({"ANIXO/Anixo.exe": b"new app"}))
    result = updater_helper.apply_plan(_write_plan(tmp_path, install, package))
    assert result["ok"] is False
    assert "overlaps user data" in result["detail"]
    assert result["restored"] is False


def test_snapshot_skips_reparse_points(tmp_path: Path, monkeypatch: Any) -> None:
    root = tmp_path / "install"
    (root / "junction").mkdir(parents=True)
    (root / "junction" / "secret.txt").write_text("do not traverse", encoding="utf-8")
    (root / "normal.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(
        updater_helper,
        "_is_reparse",
        lambda path: Path(path).name == "junction",
    )
    files, directories = updater_helper._snapshot_tree(root, set())
    assert "normal.txt" in files
    assert not any("junction" in item for item in files)
    assert not any("junction" in item for item in directories)


def test_windows_simulation_does_not_change_path_flavor(monkeypatch: Any) -> None:
    """Simulating Windows for the helper must not switch pathlib's flavor."""
    import pathlib

    monkeypatch.setattr(os, "name", "posix")
    original = type(pathlib.Path("probe")).__name__
    monkeypatch.setattr(updater_helper, "os", _windows_os())
    assert type(pathlib.Path("probe")).__name__ == original
    assert updater_helper.os.name == "nt"


def test_parent_alive_fails_closed_on_simulated_windows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updater_helper, "os", _windows_os())
    monkeypatch.setattr(updater_helper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(updater_helper, "_process_start_epoch", lambda pid: None)
    with pytest.raises(updater_helper.UpdateHelperError, match="still running") as error:
        updater_helper._wait_for_parent({"parent_pid": 4242}, timeout=0)
    assert "Windows" in str(error.value)


def test_parent_pid_reuse_fails_closed(monkeypatch: Any) -> None:
    monkeypatch.setattr(updater_helper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        updater_helper, "_process_start_epoch", lambda pid: time.time() + 120
    )
    with pytest.raises(updater_helper.UpdateHelperError, match="PID was reused"):
        updater_helper._wait_for_parent(
            {"parent_pid": 4242, "parent_started_epoch": time.time()}, timeout=0
        )


def test_parent_alive_failure_writes_failed_marker(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updater_helper, "os", _windows_os())
    monkeypatch.setattr(updater_helper, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(updater_helper, "_process_start_epoch", lambda pid: None)
    original_wait = updater_helper._wait_for_parent
    monkeypatch.setattr(
        updater_helper,
        "_wait_for_parent",
        lambda plan: original_wait(plan, timeout=0),
    )
    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"old app")
    package = tmp_path / f"ANIXO-Portable-v{APP_VERSION}.zip"
    package.write_bytes(_zip_bytes({"ANIXO/Anixo.exe": b"new app"}))
    result = updater_helper.apply_plan(
        _write_plan(tmp_path, install, package, parent_pid=4242)
    )
    assert result["ok"] is False
    assert "still running" in result["detail"]
    assert (install / "Anixo.exe").read_bytes() == b"old app"
    marker = json.loads(
        (tmp_path / "data" / "updates" / "last-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["ok"] is False
    assert "still running" in marker["detail"]


# ---------------------------------------------------------------------------
# Cancel race


def test_cancel_keeps_busy_until_update_worker_exits(tmp_path: Path) -> None:
    app = AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            check_for_app_updates=False,
        ),
        data_dir=tmp_path / "data",
    )
    version = _higher_version()
    check = UpdateCheck(
        current=APP_VERSION,
        available=version,
        update_available=True,
        channel="stable",
        release=ReleaseInfo(version, f"v{version}", "", "", False, ()),
        error="",
    )
    started = asyncio.Event()
    release_gate = asyncio.Event()

    async def fake_check(channel: str | None = None, *, force: bool = False) -> UpdateCheck:
        return check

    async def fake_confirm(title: str, message: str = "") -> bool:
        return True

    async def fake_prepare(release: ReleaseInfo, **kwargs: Any) -> Any:
        started.set()
        await release_gate.wait()
        should_cancel = kwargs.get("should_cancel")
        if should_cancel and should_cancel():
            raise asyncio.CancelledError("Update download cancelled.")
        raise AssertionError("download should have been cancelled")

    app.updates.check = fake_check  # type: ignore[method-assign]
    app.updates.prepare_update = fake_prepare  # type: ignore[method-assign]
    app.confirm = fake_confirm  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            task = asyncio.create_task(app._update_anixo_flow())
            await asyncio.wait_for(started.wait(), timeout=5)
            app.action_cancel_update()
            assert app.update_state.busy is True
            assert "Cancelling" in app.update_state.status
            release_gate.set()
            await asyncio.wait_for(task, timeout=5)
            assert app.update_state.busy is False
            assert "cancelled" in app.update_state.status.lower()
            assert app.update_state.can_cancel is False
            assert app._update_cancel is None

    asyncio.run(run())


def test_userinfo_urls_are_not_trusted() -> None:
    hosts = set(Config().update_trusted_hosts)
    assert not is_trusted_asset_url(
        "https://user:placeholder@api.github.com/repos/test/anixo/releases", hosts
    )
