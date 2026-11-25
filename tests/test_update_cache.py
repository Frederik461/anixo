"""Deterministic tests for release-check caching and rate-limit fallback.

No test in this module contacts the network.  HTTP responses, streaming
downloads, and app notifications are fakes; all state lives in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
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
from services.updater import UpdateCheck, UpdateService, detect_target
from version import APP_VERSION


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _versions() -> tuple[str, str]:
    """Return synthetic stable/beta versions derived from the installed version."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", APP_VERSION)
    assert match is not None
    major = int(match.group(1)) + 100
    stable = f"{major}.{match.group(2)}.{match.group(3)}"
    return stable, f"{stable}-beta.1"


def _service(
    tmp_path: Path,
    *,
    channel: str = "stable",
    feed: str = "",
    repo: str = "test/anixo",
    interval_minutes: int = 30,
    max_age_days: int = 7,
) -> UpdateService:
    config = Config()
    config.update_channel = channel
    config.update_feed_url = feed
    config.update_check_interval_minutes = interval_minutes
    config.update_cache_max_age_days = max_age_days
    config.update_public_key = ""
    config.check_for_app_updates = False
    config.extra["update_repo"] = repo
    return UpdateService(
        config,
        data_dir=tmp_path / "data",
        adapter=PlatformAdapter(WINDOWS),
        target=detect_target("Windows", "AMD64"),
    )


def _payload(version: str, *, prerelease: bool = False, digest: str = "") -> dict[str, Any]:
    name = f"ANIXO-Setup-v{version}.exe"
    return {
        "tag_name": f"v{version}",
        "body": "synthetic release",
        "published_at": "",
        "prerelease": prerelease,
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"https://github.com/test/anixo/releases/download/v{version}/{name}"
                ),
                "size": 64,
                "digest": f"sha256:{digest}" if digest else "",
            }
        ],
    }


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: Any = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        if payload is None:
            self._payload: Any = []
        elif isinstance(payload, dict) and "tag_name" in payload:
            self._payload = [payload]
        else:
            self._payload = payload
        self._body = body

    def json(self) -> Any:
        if self.status_code == 304:
            raise AssertionError("a 304 response must not be parsed")
        return self._payload

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _FetchRecorder:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(
        self, url: str, *, headers: dict[str, str], trusted_hosts: set[str], timeout: float
    ) -> _FakeResponse:
        self.calls.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError("unexpected network request")
        return self.responses.pop(0)


def _patch_fetch(monkeypatch: Any, recorder: _FetchRecorder) -> None:
    monkeypatch.setattr(updater_module, "_fetch_response", recorder)


def _cache_payload(
    service: UpdateService,
    check: UpdateCheck,
    *,
    checked_epoch: float | None = None,
    etag: str = "",
    last_modified: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": updater_module._CACHE_SCHEMA_VERSION,
        "repo": service.repository,
        "channel": check.channel,
        "feed_url": service.feed_url,
        "checked_epoch": checked_epoch or time.time(),
        "checked_at": check.checked_at or "cached",
        "etag": etag,
        "last_modified": last_modified,
        "available": check.available,
        "update_available": check.update_available,
        "error": "",
        "message": check.message,
        "release": service._release_mapping(check.release) if check.release else None,
    }


def _write_raw_cache(
    service: UpdateService,
    check: UpdateCheck,
    *,
    checked_epoch: float | None = None,
    etag: str = "",
    last_modified: str = "",
    repo: str | None = None,
    feed_url: str | None = None,
    channel: str | None = None,
) -> Path:
    path = service._cache_path(check.channel)
    payload = _cache_payload(
        service, check, checked_epoch=checked_epoch, etag=etag, last_modified=last_modified
    )
    if repo is not None:
        payload["repo"] = repo
    if feed_url is not None:
        payload["feed_url"] = feed_url
    if channel is not None:
        payload["channel"] = channel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _first_check(tmp_path: Path, monkeypatch: Any, channel: str = "stable") -> UpdateService:
    stable, _ = _versions()
    version = stable if channel == "stable" else _versions()[1]
    service = _service(tmp_path, channel=channel)
    recorder = _FetchRecorder([_FakeResponse(200, payload=_payload(version))])
    _patch_fetch(monkeypatch, recorder)
    check = asyncio.run(service.check())
    assert check.update_available is True
    return service


# ---------------------------------------------------------------------------
# Conditional requests and fallback


def test_conditional_request_uses_cache_on_304(tmp_path: Path, monkeypatch: Any) -> None:
    source = _first_check(tmp_path, monkeypatch)
    check = asyncio.run(source.check())
    token = f"etag-{_versions()[0]}"
    modified = "Mon, 25 Nov 2026 00:00:00 GMT"
    _write_raw_cache(
        source,
        check,
        checked_epoch=time.time() - 120,
        etag=token,
        last_modified=modified,
    )

    service = _service(tmp_path, interval_minutes=1)
    recorder = _FetchRecorder(
        [_FakeResponse(304, headers={"etag": token, "last-modified": modified})]
    )
    _patch_fetch(monkeypatch, recorder)
    result = asyncio.run(service.check())

    assert result.cached is True
    assert result.release is not None
    assert result.available == check.available
    assert "no change" in result.message.lower()
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1]["If-None-Match"] == token
    assert recorder.calls[0][1]["If-Modified-Since"] == modified
    stored = json.loads(service._cache_path("stable").read_text(encoding="utf-8"))
    assert stored["checked_epoch"] > time.time() - 5


def test_403_with_valid_cache_returns_last_good_data(tmp_path: Path, monkeypatch: Any) -> None:
    source = _first_check(tmp_path, monkeypatch)
    check = asyncio.run(source.check())
    _write_raw_cache(source, check, checked_epoch=time.time() - 120)

    service = _service(tmp_path, interval_minutes=1)
    recorder = _FetchRecorder([_FakeResponse(403)])
    _patch_fetch(monkeypatch, recorder)
    result = asyncio.run(service.check())

    assert result.cached is True
    assert result.error == ""
    assert result.release is not None
    assert "rate-limited" in result.message.lower()
    assert "ago" in result.message.lower()


def test_403_without_cache_returns_actionable_error(tmp_path: Path, monkeypatch: Any) -> None:
    service = _service(tmp_path)
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(403)]))
    result = asyncio.run(service.check())
    assert result.update_available is False
    assert result.release is None
    assert "rate-limited" in result.error.lower()
    assert "public update feed" in result.error.lower()


def test_403_with_too_old_cache_refuses(tmp_path: Path, monkeypatch: Any) -> None:
    source = _first_check(tmp_path, monkeypatch)
    check = asyncio.run(source.check())
    _write_raw_cache(source, check, checked_epoch=time.time() - 2 * 86400)

    service = _service(tmp_path, max_age_days=1)
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(403)]))
    result = asyncio.run(service.check())
    assert result.release is None
    assert "too old to trust" in result.error.lower()


def test_cache_for_other_repo_channel_or_feed_is_ignored(
    tmp_path: Path, monkeypatch: Any
) -> None:
    stable, _ = _versions()
    cases = [
        {"repo": "someone/other"},
        {"feed_url": "https://feed.example.test/manifest.json"},
        {"channel": "beta"},
    ]
    for index, mismatch in enumerate(cases):
        root = tmp_path / f"case-{index}"
        source = _service(root)
        check = asyncio.run(
            _fetch_once(root, monkeypatch, source, stable)
        )
        _write_raw_cache(source, check, checked_epoch=time.time() - 120, **mismatch)

        service = _service(root)
        recorder = _FetchRecorder([_FakeResponse(200, payload=_payload(stable))])
        _patch_fetch(monkeypatch, recorder)
        result = asyncio.run(service.check())
        assert result.cached is False
        assert len(recorder.calls) == 1


async def _fetch_once(
    root: Path, monkeypatch: Any, service: UpdateService, version: str
) -> UpdateCheck:
    recorder = _FetchRecorder([_FakeResponse(200, payload=_payload(version))])
    _patch_fetch(monkeypatch, recorder)
    return await service.check()


def test_corrupt_cache_is_ignored(tmp_path: Path, monkeypatch: Any) -> None:
    stable, _ = _versions()
    service = _service(tmp_path)
    path = service._cache_path("stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    recorder = _FetchRecorder([_FakeResponse(200, payload=_payload(stable))])
    _patch_fetch(monkeypatch, recorder)
    result = asyncio.run(service.check())
    assert result.cached is False
    assert result.available == stable
    assert len(recorder.calls) == 1


def test_min_interval_prevents_network_call(tmp_path: Path, monkeypatch: Any) -> None:
    service = _first_check(tmp_path, monkeypatch)
    recorder = _FetchRecorder([])
    _patch_fetch(monkeypatch, recorder)
    result = asyncio.run(service.check())
    assert result.cached is True
    assert "ago" in result.message.lower()
    assert recorder.calls == []


def test_manual_cooldown_prevents_repeated_requests(tmp_path: Path, monkeypatch: Any) -> None:
    service = _first_check(tmp_path, monkeypatch)
    recorder = _FetchRecorder([])
    _patch_fetch(monkeypatch, recorder)
    result = asyncio.run(service.check(force=True))
    assert result.cached is True
    assert "retry shortly" in result.message.lower()
    assert recorder.calls == []


def test_error_does_not_overwrite_good_cache(tmp_path: Path, monkeypatch: Any) -> None:
    source = _first_check(tmp_path, monkeypatch)
    check = asyncio.run(source.check())
    path = _write_raw_cache(source, check, checked_epoch=time.time() - 120)

    service = _service(tmp_path, interval_minutes=1)
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(403)]))
    result = asyncio.run(service.check())
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert result.cached is True
    assert stored["release"]["version"] == check.available
    assert stored["error"] == ""


def test_cache_file_contains_no_credentials_or_signed_urls(
    tmp_path: Path, monkeypatch: Any
) -> None:
    stable, _ = _versions()
    secret = f"secret-{stable}"
    signature = f"signed-{stable}"
    service = _service(tmp_path)
    payload = _payload(stable)
    payload["assets"][0]["browser_download_url"] += (
        f"?sig={secret}&X-Amz-Signature={signature}"
    )
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(200, payload=payload)]))
    asyncio.run(service.check())
    stored = service._cache_path("stable").read_text(encoding="utf-8")
    assert secret not in stored
    assert signature not in stored
    assert "<redacted>" in stored


# ---------------------------------------------------------------------------
# Channel safety from cached data


def test_stable_never_gets_beta_from_cache(tmp_path: Path, monkeypatch: Any) -> None:
    _, beta = _versions()
    beta_source = _service(tmp_path, channel="beta")
    beta_recorder = _FetchRecorder(
        [_FakeResponse(200, payload=_payload(beta, prerelease=True))]
    )
    _patch_fetch(monkeypatch, beta_recorder)
    beta_check = asyncio.run(beta_source.check())
    assert beta_check.update_available is True
    _write_raw_cache(beta_source, beta_check, checked_epoch=time.time() - 120)

    stable_service = _service(tmp_path)
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(403)]))
    stable_result = asyncio.run(stable_service.check())
    assert stable_result.release is None
    assert stable_result.update_available is False

    beta_service = _service(tmp_path, channel="beta")
    beta_result = asyncio.run(beta_service.check())
    assert beta_result.cached is True
    assert beta_result.available == beta
    assert beta_result.release is not None
    assert beta_result.release.prerelease is True


def test_stable_cache_rejects_prerelease_flag_even_without_manifest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    stable, _ = _versions()
    service = _service(tmp_path)
    good = UpdateCheck(
        current=APP_VERSION,
        available=stable,
        update_available=True,
        channel="stable",
        release=None,
        error="",
    )
    payload = _cache_payload(service, good, checked_epoch=time.time() - 120)
    payload["release"] = {
        "version": stable,
        "tag": f"v{stable}",
        "notes": "",
        "published_at": "",
        "prerelease": True,
        "source": "github",
        "assets": [],
        "manifest": None,
    }
    path = service._cache_path("stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(403)]))
    result = asyncio.run(service.check())
    assert result.release is None
    assert "rate-limited" in result.error.lower()


# ---------------------------------------------------------------------------
# Cached offers still get full production validation


def test_cached_offer_still_runs_checksum_and_package_validation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    stable, _ = _versions()
    name = f"ANIXO-Portable-v{stable}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ANIXO/Anixo.exe", b"app")
    good = buffer.getvalue()
    service = _service(tmp_path)
    payload = _payload(stable)
    payload["assets"] = [
        {
            "name": name,
            "browser_download_url": (
                f"https://github.com/test/anixo/releases/download/v{stable}/{name}"
            ),
            "size": len(good),
            "digest": f"sha256:{_sha(good)}",
        }
    ]
    _patch_fetch(monkeypatch, _FetchRecorder([_FakeResponse(200, payload=payload)]))
    first = asyncio.run(service.check())
    assert first.release is not None

    cached = asyncio.run(service.check())
    assert cached.cached is True
    assert cached.release is not None

    bodies: list[bytes] = [b"corrupted", good]

    @contextlib.asynccontextmanager
    async def fake_stream(url: str, **kwargs: Any):
        response = _FakeResponse(200, body=bodies.pop(0))
        response.headers["content-length"] = str(len(response._body))
        yield response

    monkeypatch.setattr(updater_module, "_stream_response", fake_stream)
    validations: list[str] = []
    real_validate = updater_module.validate_package

    def counting_validate(path: Path, asset: Any, target: Any) -> str:
        validations.append(asset.name)
        return real_validate(path, asset, target)

    monkeypatch.setattr(updater_module, "validate_package", counting_validate)

    with pytest.raises(ValueError, match="SHA256 mismatch"):
        asyncio.run(
            service.prepare_update(
                cached.release, install_dir=tmp_path / "install"
            )
        )
    assert validations == []

    prepared = asyncio.run(
        service.prepare_update(cached.release, install_dir=tmp_path / "install")
    )
    assert prepared.checksum_verified is True
    assert "ZIP package structure verified" in prepared.verification
    assert validations == [name]


# ---------------------------------------------------------------------------
# Auto-check visibility and release packaging


def _app(tmp_path: Path) -> AnixoApp:
    return AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            check_for_app_updates=False,
        ),
        data_dir=tmp_path / "data",
    )


def test_auto_check_reports_cached_status_once(tmp_path: Path, monkeypatch: Any) -> None:
    app = _app(tmp_path)
    cached = UpdateCheck(
        current=APP_VERSION,
        available="",
        update_available=False,
        channel="stable",
        release=None,
        error="",
        cached=True,
        message="Cached result from 2m ago — ANIXO is current.",
    )

    async def fake_check(channel: str | None = None, *, force: bool = False) -> UpdateCheck:
        return cached

    notifications: list[str] = []
    monkeypatch.setattr(app.updates, "check", fake_check)
    monkeypatch.setattr(
        app,
        "notify_user",
        lambda message, *, timeout=3: notifications.append(message),  # type: ignore[arg-type]
    )
    asyncio.run(app._auto_check_updates())
    asyncio.run(app._auto_check_updates())
    assert "Cached result" in app.update_state.status
    assert len(notifications) == 1


def test_auto_check_reports_error_status_once(tmp_path: Path, monkeypatch: Any) -> None:
    app = _app(tmp_path)
    failed = UpdateCheck(
        current=APP_VERSION,
        available="",
        update_available=False,
        channel="stable",
        release=None,
        error="GitHub rate-limited the update check.",
    )

    async def fake_check(channel: str | None = None, *, force: bool = False) -> UpdateCheck:
        return failed

    notifications: list[str] = []
    monkeypatch.setattr(app.updates, "check", fake_check)
    monkeypatch.setattr(
        app,
        "notify_user",
        lambda message, *, timeout=3: notifications.append(message),  # type: ignore[arg-type]
    )
    asyncio.run(app._auto_check_updates())
    asyncio.run(app._auto_check_updates())
    assert "Update check failed" in app.update_state.status
    assert "rate-limited" in app.update_state.status
    assert len(notifications) == 1


def test_packaging_writes_explicit_channel_for_stable_and_beta(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "packaging" / "make_release_manifest.py"
    spec = importlib.util.spec_from_file_location("anixo_release_manifest", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stable, beta = _versions()

    for version in (stable, beta):
        release_dir = tmp_path / version
        release_dir.mkdir()
        for template, *_ in module._ASSET_SPECS:
            (release_dir / template.format(version=version)).write_bytes(b"asset")
        manifest = module.build_manifest(
            release_dir, version, "https://example.test/releases"
        )
        assert manifest["channel"] == ("beta" if version == beta else "stable")
        assert "channel" in manifest
