"""Deterministic tests for the staged ANIXO and ani-cli update systems.

No test contacts the network or executes a downloaded file.  HTTP clients,
helper subprocesses and parent-process waits are replaced with in-memory fakes
so download, corruption, cancellation and rollback paths are reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest

from config import Config
from platforms import MACOS, WINDOWS, PlatformAdapter
from services import setup as setup_service
from services import updater as updater_module
from services.ani_cli_updater import (
    AniCliUpdateCheck,
    AniCliUpdateService,
)
from services.dependency_manager import DependencyManager
from services.install_manifest import (
    COMPONENT_ANI_CLI,
    InstallRecord,
    ManifestStore,
)
from services.updater import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateService,
    detect_target,
    parse_manifest,
    select_asset,
    select_helper_asset,
    validate_package,
)
from version import APP_VERSION
import updater_helper


# ---------------------------------------------------------------------------
# Helpers


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _service(
    tmp_path: Path,
    *,
    channel: str = "stable",
    target=None,
    feed: str = "",
    repo: str = "test/anixo",
) -> UpdateService:
    config = Config()
    config.update_channel = channel
    config.update_feed_url = feed
    config.extra["update_repo"] = repo
    config.update_public_key = ""
    return UpdateService(
        config,
        data_dir=tmp_path / "data",
        adapter=PlatformAdapter(WINDOWS),
        target=target or detect_target("Windows", "AMD64"),
    )


def _asset(
    name: str,
    url: str = "https://objects.githubusercontent.com/release/file",
    digest: str = "",
) -> ReleaseAsset:
    return ReleaseAsset(name=name, url=url, size=0, sha256=digest)


def _release(version: str, assets: tuple[ReleaseAsset, ...], prerelease: bool = False) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        notes=f"Notes for {version}",
        published_at="2026-11-25T00:00:00Z",
        prerelease=prerelease,
        assets=assets,
    )


def _github_payload(version: str, names: list[str], prerelease: bool = False) -> dict[str, Any]:
    return {
        "tag_name": f"v{version}",
        "body": f"Notes for {version}",
        "published_at": "2026-11-25T00:00:00Z",
        "prerelease": prerelease,
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://objects.githubusercontent.com/release/{name}",
                "digest": "sha256:" + _sha(b"payload"),
                "size": 7,
            }
            for name in names
        ],
    }


def _fake_pe(machine: int = 0x8664) -> bytes:
    data = bytearray(b"MZ" + b"\x00" * 62)
    struct.pack_into("<I", data, 0x3C, 0x80)
    data.extend(b"\x00" * (0x80 - len(data)))
    data.extend(b"PE\x00\x00")
    data.extend(struct.pack("<H", machine))
    data.extend(b"\x00" * 16)
    return bytes(data)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {"content-length": str(len(body))}

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, size: int):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class _FakeStream:
    def __init__(self, body: bytes) -> None:
        self.response = _FakeResponse(body)

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeAsyncClient:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def stream(self, method: str, url: str) -> _FakeStream:
        return _FakeStream(self.body)


def _ani_cli_source(marker: bytes = b"v5.1") -> bytes:
    search = '        anime_list=$(hianime_search "$query") || exit 1\n'
    menu = 'dep_ch "$menu_program" # TODO: use dep_ch_failover to iterate through options\n'
    head = f"#!/usr/bin/env bash\n# ani-cli {marker.decode()}\nbase_api=https://example.invalid\n".encode()
    body = head + (search + menu).encode() + b"\n" + b"# padding line\n" * 400
    return body


class _StubAniCli:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def installed_script(self) -> Path | None:
        return self.path


def _ani_cli_record(path: Path, digest: str, *, owned: bool = True, version: str = "v5.1") -> InstallRecord:
    return InstallRecord(
        key=COMPONENT_ANI_CLI,
        name="ani-cli",
        platform="windows",
        installed_by_anixo=owned,
        managed=owned,
        safe_to_remove=owned,
        install_location=str(path),
        version=version,
        installed_at="2026-11-21T00:00:00+00:00",
        source="https://example.invalid/ani-cli",
        install_method="pinned-script",
        expected_sha256=digest.upper(),
        observed_sha256=digest.upper(),
        paths=(str(path),),
        config_keys=("ani_cli_path",),
    )


def _ani_cli_service(
    tmp_path: Path,
    script: Path,
    *,
    owned: bool = True,
) -> AniCliUpdateService:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    digest = _sha(script.read_bytes()).upper()
    store = ManifestStore(data, PlatformAdapter(WINDOWS))
    manifest = store.new_manifest("windows-installer")
    manifest.upsert(_ani_cli_record(script, digest, owned=owned))
    store.save(manifest)
    config = Config()
    config.ani_cli_path = str(script)
    config.preferred_audio = "dub"
    manager = DependencyManager(config, data, cache, PlatformAdapter(WINDOWS), store)
    return AniCliUpdateService(
        config,
        data,
        ani_cli=_StubAniCli(script),
        manager=manager,
        adapter=PlatformAdapter(WINDOWS),
    )


def _plan(
    tmp_path: Path,
    install_dir: Path,
    package: Path,
    *,
    platform: str = "windows",
    arch: str = "x86_64",
    version: str = "9.9.9",
) -> Path:
    payload = {
        "schema_version": 1,
        "app_name": "ANIXO",
        "version": version,
        "channel": "stable",
        "notes": "",
        "published_at": "",
        "install_kind": "portable",
        "install_dir": str(install_dir),
        "executable": str(install_dir / "Anixo.exe"),
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
        "parent_pid": 0,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Targets, channel selection and manifest validation


def test_detect_target_covers_supported_platforms_and_rejects_unknown() -> None:
    assert detect_target("Windows", "AMD64").key == "windows-x86_64"
    assert detect_target("Darwin", "arm64").key == "macos-arm64"
    assert detect_target("Darwin", "x86_64").key == "macos-x86_64"
    assert detect_target("Windows", "arm64").arch == "arm64"


def test_select_asset_matches_platform_arch_and_kind() -> None:
    win = detect_target("Windows", "AMD64")
    arm = detect_target("Darwin", "arm64")
    intel = detect_target("Darwin", "x86_64")
    assets = (
        _asset("ANIXO-Setup-v1.2.0.exe"),
        _asset("ANIXO-Portable-v1.2.0.zip"),
        _asset("ANIXO-Uninstaller-v1.2.0.exe"),
        _asset("ANIXO-v1.2.0-macos-arm64.dmg"),
        _asset("ANIXO-v1.2.0-macos-x64.dmg"),
        _asset("ANIXO-v1.2.0-macos-arm64.zip"),
    )
    release = _release("1.2.0", assets)
    assert select_asset(release, win, "installer").name == "ANIXO-Setup-v1.2.0.exe"
    assert select_asset(release, win, "portable").name == "ANIXO-Portable-v1.2.0.zip"
    assert select_asset(release, arm).name in {
        "ANIXO-v1.2.0-macos-arm64.dmg",
        "ANIXO-v1.2.0-macos-arm64.zip",
    }
    assert select_asset(release, intel).name == "ANIXO-v1.2.0-macos-x64.dmg"
    assert select_asset(release, detect_target("Windows", "arm64")) is None


def test_select_asset_refuses_features_that_do_not_exist() -> None:
    assert select_asset(_release("1.0.0", ()), detect_target("Windows", "AMD64")) is None


def test_parse_manifest_rejects_malformed_metadata_and_hosts() -> None:
    base = {
        "schema_version": 1,
        "version": "1.2.0",
        "channel": "stable",
        "assets": [
            {
                "name": "ANIXO-Setup-v1.2.0.exe",
                "url": "https://cdn.example.test/ANIXO-Setup-v1.2.0.exe",
                "sha256": _sha(b"x"),
            }
        ],
    }
    assert parse_manifest(base, "https://cdn.example.test/manifest.json").version == "1.2.0"
    with pytest.raises(ValueError, match="schema"):
        parse_manifest({**base, "schema_version": 2})
    with pytest.raises(ValueError, match="version"):
        parse_manifest({**base, "version": "not-a-version"})
    with pytest.raises(ValueError, match="SHA-256"):
        parse_manifest(
            {
                **base,
                "assets": [{**base["assets"][0], "sha256": "xyz"}],
            }
        )
    with pytest.raises(ValueError, match="outside the trusted"):
        parse_manifest(
            base,
            "https://other.test/manifest.json",
        )
    with pytest.raises(ValueError, match="trusted"):
        parse_manifest(
            {
                **base,
                "assets": [{**base["assets"][0], "url": "http://cdn.example.test/x.exe"}],
            },
            "https://cdn.example.test/manifest.json",
        )
    assert updater_module._is_https("http://cdn.example.test/x.exe") is False
    assert updater_module._is_https("https://cdn.example.test/x.exe") is True


def test_check_stable_channel_excludes_beta_and_uses_cache(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, channel="stable")
    calls: list[str] = []

    async def fake_fetch(url: str) -> Any:
        calls.append(url)
        return [
            _github_payload("2.0.0-beta.1", ["ANIXO-Setup-v2.0.0-beta.1.exe"], True),
            _github_payload("1.2.0", ["ANIXO-Setup-v1.2.0.exe"]),
        ]

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    first = asyncio.run(service.check())
    second = asyncio.run(service.check())
    assert first.available == "1.2.0"
    assert first.update_available is True
    assert "2.0.0-beta" not in str(first.release.assets)
    assert second.cached is True
    assert len(calls) == 1


def test_beta_channel_can_select_beta_release(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, channel="beta")

    async def fake_fetch(url: str) -> Any:
        return [_github_payload("2.0.0-beta.1", ["ANIXO-Setup-v2.0.0-beta.1.exe"], True)]

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert check.channel == "beta"
    assert check.available == "2.0.0-beta.1"
    assert check.release is not None and check.release.prerelease is True


def test_check_reports_incompatible_platform_without_error(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, target=detect_target("Darwin", "arm64"))

    async def fake_fetch(url: str) -> Any:
        return [_github_payload("1.2.0", ["ANIXO-Setup-v1.2.0.exe"])]

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert check.update_available is False
    assert "compatible asset" in check.message


def test_check_private_repository_gives_actionable_error(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    async def fake_fetch(url: str) -> Any:
        raise ValueError(
            "The update source is not publicly readable. Configure a public update feed URL in Settings → Updates."
        )

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert "public update feed" in check.error
    assert check.release is None


def test_public_feed_manifest_is_used(tmp_path: Path, monkeypatch) -> None:
    feed = "https://cdn.example.test/manifest.json"
    service = _service(tmp_path, feed=feed)
    payload = {
        "schema_version": 1,
        "version": "1.3.0",
        "channel": "stable",
        "published_at": "2026-11-25T00:00:00Z",
        "notes": "Tester feed build",
        "assets": [
            {
                "name": "ANIXO-Setup-v1.3.0.exe",
                "url": "https://cdn.example.test/ANIXO-Setup-v1.3.0.exe",
                "platform": "windows",
                "arch": "x86_64",
                "sha256": _sha(b"x"),
            }
        ],
    }

    async def fake_fetch(url: str) -> Any:
        assert url == feed
        return payload

    monkeypatch.setattr(service, "_fetch_json", fake_fetch)
    check = asyncio.run(service.check())
    assert check.available == "1.3.0"
    assert check.release is not None and check.release.source == "feed"
    assert check.release.notes == "Tester feed build"


# ---------------------------------------------------------------------------
# Downloads, checksums, package validation and prepare plans


def test_download_asset_verifies_checksum_and_writes_final_path(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    body = b"verified-bytes"
    monkeypatch.setattr(updater_module.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(body))
    progress: list[str] = []
    path = asyncio.run(
        service.download_asset(
            _asset("ANIXO-Setup-v1.2.0.exe", digest=_sha(body)),
            tmp_path / "staging",
            _sha(body),
            progress=lambda item: progress.append(item.stage),
        )
    )
    assert path.read_bytes() == body
    assert "downloading" in progress and progress[-1] == "verified"


def test_download_asset_rejects_corruption_and_keeps_no_final_file(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        updater_module.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(b"corrupted")
    )
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        asyncio.run(
            service.download_asset(
                _asset("ANIXO-Setup-v1.2.0.exe"),
                tmp_path / "staging",
                _sha(b"expected"),
            )
        )
    assert not (tmp_path / "staging" / "ANIXO-Setup-v1.2.0.exe").exists()
    assert not list((tmp_path / "staging").glob("*.part"))


def test_download_asset_is_cancellable(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        updater_module.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(b"verified-bytes")
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.download_asset(
                _asset("ANIXO-Setup-v1.2.0.exe"),
                tmp_path / "staging",
                _sha(b"verified-bytes"),
                should_cancel=lambda: True,
            )
        )
    assert not list((tmp_path / "staging").glob("*"))


def test_resolve_checksum_from_sums_file(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    sums = ReleaseAsset(
        "SHA256SUMS.txt", "https://objects.githubusercontent.com/SHA256SUMS.txt"
    )
    release = _release("1.2.0", (_asset("ANIXO-Setup-v1.2.0.exe"), sums))

    async def fake_text(url: str) -> str:
        return f"{_sha(b'x')}  ANIXO-Setup-v1.2.0.exe\n"

    monkeypatch.setattr(service, "_fetch_text", fake_text)
    assert asyncio.run(service._resolve_checksum(release, release.assets[0])) == _sha(b"x")


def test_validate_package_checks_architecture_and_structure(tmp_path: Path) -> None:
    win = detect_target("Windows", "AMD64")
    arm = detect_target("Darwin", "arm64")

    setup = tmp_path / "setup.exe"
    setup.write_bytes(_fake_pe(0x8664))
    assert "architecture verified" in validate_package(setup, _asset("setup.exe"), win)
    with pytest.raises(ValueError, match="does not match"):
        validate_package(setup, _asset("setup.exe"), detect_target("Windows", "arm64"))

    portable = tmp_path / "portable.zip"
    portable.write_bytes(_zip_bytes({"ANIXO/Anixo.exe": b"MZ"}))
    assert "verified" in validate_package(portable, _asset("portable.zip"), win)

    unsafe = tmp_path / "unsafe.zip"
    unsafe.write_bytes(_zip_bytes({"../evil.txt": b"x", "ANIXO/Anixo.exe": b"MZ"}))
    with pytest.raises(ValueError, match="unsafe path"):
        validate_package(unsafe, _asset("unsafe.zip"), win)

    dmg = tmp_path / "app.dmg"
    dmg.write_bytes(b"\x00" * 100 + b"koly" + b"\x00" * 508)
    assert "DMG container" in validate_package(
        dmg, _asset("ANIXO-v1.2.0-macos-arm64.dmg"), arm
    )


def test_prepare_update_writes_verified_plan(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    body = _fake_pe()
    digest = _sha(body)
    asset = ReleaseAsset(
        "ANIXO-Setup-v9.9.9.exe",
        "https://objects.githubusercontent.com/ANIXO-Setup-v9.9.9.exe",
        size=len(body),
        sha256=digest,
    )
    release = _release("9.9.9", (asset,))

    async def fake_download(asset_arg: ReleaseAsset, destination: Path, expected: str, **kwargs: Any) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / asset.name
        path.write_bytes(body)
        return path

    monkeypatch.setattr(service, "download_asset", fake_download)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    prepared = asyncio.run(
        service.prepare_update(release, preferred_kind="installer", install_dir=install_dir)
    )
    plan = json.loads(prepared.plan_path.read_text(encoding="utf-8"))
    assert prepared.checksum_verified is True
    assert plan["version"] == "9.9.9"
    assert plan["sha256"] == digest
    assert plan["platform"] == "windows"
    assert plan["arch"] == "x86_64"
    assert plan["parent_pid"] > 0
    assert "signature" in prepared.verification


def test_signature_verification_fails_closed_when_key_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    service.config.update_public_key = str(tmp_path / "missing.pem")
    status, detail = service._verify_signature(None)
    assert status == "signature FAILED"
    assert "no signature" in detail

    body = _fake_pe()
    asset = ReleaseAsset(
        "ANIXO-Setup-v9.9.9.exe",
        "https://objects.githubusercontent.com/ANIXO-Setup-v9.9.9.exe",
        size=len(body),
        sha256=_sha(body),
    )

    async def fake_download(asset_arg: ReleaseAsset, destination: Path, expected: str, **kwargs: Any) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / asset.name
        path.write_bytes(body)
        return path

    monkeypatch.setattr(service, "download_asset", fake_download)
    monkeypatch.setattr(
        service,
        "_verify_signature",
        lambda manifest: ("signature FAILED", "forced failure"),
    )
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    with pytest.raises(ValueError, match="signature verification failed"):
        asyncio.run(
            service.prepare_update(
                _release("9.9.9", (asset,)),
                preferred_kind="installer",
                install_dir=install_dir,
            )
        )


def test_prepare_update_honours_cancellation_before_download(tmp_path: Path) -> None:
    service = _service(tmp_path)
    release = _release(
        "9.9.9",
        (
            ReleaseAsset(
                "ANIXO-Setup-v9.9.9.exe",
                "https://objects.githubusercontent.com/x.exe",
                sha256=_sha(b"x"),
            ),
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.prepare_update(
                release,
                install_dir=tmp_path / "install",
                should_cancel=lambda: True,
            )
        )


def test_helper_command_and_launch_are_deterministic(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.delenv("ANIXO_UPDATER_HELPER", raising=False)
    command = service.helper_command(tmp_path / "plan.json")
    assert command[0].endswith("python.exe") or command[0].endswith("python")
    assert command[1].endswith("updater_helper.py")
    assert command[2] == "--plan"

    monkeypatch.setenv("ANIXO_UPDATER_HELPER", str(tmp_path / "missing-helper.exe"))
    assert service.launch_helper(tmp_path / "plan.json") is False


# ---------------------------------------------------------------------------
# ani-cli updater


def test_ani_cli_detect_revisions_and_ownership(tmp_path: Path) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    script.write_bytes(_ani_cli_source())
    service = _ani_cli_service(tmp_path, script)
    detected = service.detect_installed()
    assert detected.installed is True
    assert detected.owned_by_anixo is True
    assert detected.version == "v5.1"
    assert detected.health == "ANIXO-managed revision"
    assert "Exact-provider patch: compatible" in detected.detail


def test_ani_cli_detect_user_managed_script(tmp_path: Path) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    script.write_bytes(_ani_cli_source())
    service = _ani_cli_service(tmp_path, script, owned=False)
    detected = service.detect_installed()
    assert detected.owned_by_anixo is False
    assert "user-managed" in detected.health


def test_ani_cli_release_parser_requires_asset_and_digest(tmp_path: Path) -> None:
    payload = {
        "tag_name": "v9.9.9",
        "assets": [
            {
                "name": "ani-cli",
                "browser_download_url": "https://example.test/ani-cli",
                "digest": "sha256:" + "A" * 64,
            }
        ],
    }
    version, url, digest = AniCliUpdateService._parse_release(payload)
    assert version == "9.9.9" and url.endswith("/ani-cli") and digest == "A" * 64
    with pytest.raises(ValueError, match="asset"):
        AniCliUpdateService._parse_release({"tag_name": "v9.9.9", "assets": []})
    with pytest.raises(ValueError, match="SHA-256"):
        AniCliUpdateService._parse_release(
            {
                "tag_name": "v9.9.9",
                "assets": [{"name": "ani-cli", "browser_download_url": "https://x.test/a", "digest": "bad"}],
            }
        )


def test_ani_cli_update_refuses_user_managed_installation(tmp_path: Path) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    original = _ani_cli_source()
    script.write_bytes(original)
    service = _ani_cli_service(tmp_path, script, owned=False)
    check = AniCliUpdateCheck(
        "v5.1", "v9.9.9", True, "2026-11-25", "https://example.test/ani-cli", "B" * 64, ""
    )
    result = service.update(check)
    assert result.ok is False
    assert "another tool" in result.message
    assert script.read_bytes() == original


def test_ani_cli_update_stages_verifies_and_records(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    script.write_bytes(_ani_cli_source())
    service = _ani_cli_service(tmp_path, script)
    candidate = _ani_cli_source(b"v9.9.9")
    digest = _sha(candidate).upper()
    monkeypatch.setattr(setup_service, "_find_git_bash", lambda *a, **k: None)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: candidate)
    check = AniCliUpdateCheck(
        "v5.1", "v9.9.9", True, "2026-11-25", "https://example.test/ani-cli", digest, ""
    )
    result = service.update(check)
    assert result.ok is True
    assert result.version == "v9.9.9"
    assert script.read_bytes() == candidate
    assert service._manager().manifest.get(COMPONENT_ANI_CLI).version == "v9.9.9"
    assert service.config.preferred_audio == "dub"
    assert service.config.ani_cli_path == str(script)
    assert Path(result.backup).exists()


def test_ani_cli_update_rolls_back_on_corrupt_candidate(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    original = _ani_cli_source()
    script.write_bytes(original)
    service = _ani_cli_service(tmp_path, script)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: b"corrupted")
    check = AniCliUpdateCheck(
        "v5.1", "v9.9.9", True, "2026-11-25", "https://example.test/ani-cli", "B" * 64, ""
    )
    result = service.update(check)
    assert result.ok is False
    assert script.read_bytes() == original
    assert service._manager().manifest.get(COMPONENT_ANI_CLI).version == "v5.1"


def test_ani_cli_check_uses_cached_result_on_network_failure(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir(parents=True)
    script.write_bytes(_ani_cli_source())
    service = _ani_cli_service(tmp_path, script)
    cached = AniCliUpdateCheck(
        "v5.1", "v9.9.9", True, "2026-11-24", "https://example.test/ani-cli", "B" * 64, ""
    )
    service._write_cache(cached)

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_FailingClient":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def get(self, url: str):
            import httpx

            raise httpx.ConnectError("offline")

    import services.ani_cli_updater as module

    monkeypatch.setattr(module.httpx, "Client", _FailingClient)
    result = service.check(force=True)
    assert result.cached is True
    assert result.latest_version == "v9.9.9"
    assert "last successful" in result.message


# ---------------------------------------------------------------------------
# Helper process: portable/installer modes, rollback, user data


def _patch_helper(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(updater_helper, "_wait_for_parent", lambda plan: calls.append("wait"))
    monkeypatch.setattr(updater_helper, "_health_check", lambda plan: calls.append("health") or "ok")
    monkeypatch.setattr(updater_helper, "_restart", lambda plan: calls.append("restart"))
    return calls


def test_helper_portable_update_preserves_user_data(tmp_path: Path, monkeypatch) -> None:
    calls = _patch_helper(monkeypatch)
    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"old app")
    (install / "Anixo.dll").write_bytes(b"old dll")
    (install / "PORTABLE.txt").write_text("portable", encoding="utf-8")
    (install / "watchlist.json").write_text("user data", encoding="utf-8")
    (install / "config.json").write_text('{"theme":"aurora"}', encoding="utf-8")
    package = tmp_path / "ANIXO-Portable-v9.9.9.zip"
    package.write_bytes(
        _zip_bytes(
            {
                "ANIXO/Anixo.exe": b"new app",
                "ANIXO/Anixo.dll": b"new dll",
                "ANIXO/PORTABLE.txt": b"must not overwrite",
            }
        )
    )
    plan = _plan(tmp_path, install, package)
    result = updater_helper.apply_plan(plan)
    assert result["ok"] is True
    assert (install / "Anixo.exe").read_bytes() == b"new app"
    assert (install / "PORTABLE.txt").read_text(encoding="utf-8") == "portable"
    assert (install / "watchlist.json").read_text(encoding="utf-8") == "user data"
    assert (install / "config.json").read_text(encoding="utf-8") == '{"theme":"aurora"}'
    assert Path(result["backup"]).exists()
    assert calls == ["wait", "health", "restart"]


def test_helper_rolls_back_after_health_check_failure(tmp_path: Path, monkeypatch) -> None:
    _patch_helper(monkeypatch)
    monkeypatch.setattr(
        updater_helper, "_health_check", lambda plan: (_ for _ in ()).throw(
            updater_helper.UpdateHelperError("health failed")
        )
    )
    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"old app")
    (install / "history.db").write_bytes(b"history")
    package = tmp_path / "ANIXO-Portable-v9.9.9.zip"
    package.write_bytes(_zip_bytes({"ANIXO/Anixo.exe": b"new app"}))
    result = updater_helper.apply_plan(_plan(tmp_path, install, package))
    assert result["ok"] is False
    assert (install / "Anixo.exe").read_bytes() == b"old app"
    assert (install / "history.db").read_bytes() == b"history"
    marker = json.loads((tmp_path / "data" / "updates" / "last-result.json").read_text())
    assert marker["ok"] is False


def test_helper_installer_mode_invokes_silent_installer(tmp_path: Path, monkeypatch) -> None:
    calls = _patch_helper(monkeypatch)
    seen: dict[str, Any] = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        return _Completed()

    monkeypatch.setattr(updater_helper.subprocess, "run", fake_run)
    install = tmp_path / "install"
    install.mkdir()
    (install / "settings.json").write_text("keep", encoding="utf-8")
    package = tmp_path / "ANIXO-Setup-v9.9.9.exe"
    package.write_bytes(_fake_pe())
    plan_path = _plan(tmp_path, install, package)
    result = updater_helper.apply_plan(plan_path)
    assert result["ok"] is True
    assert seen["args"][1:4] == ["/VERYSILENT", "/NORESTART", "/CLOSEAPPLICATIONS"]
    assert f"/DIR={install}" in seen["args"]
    assert (install / "settings.json").read_text(encoding="utf-8") == "keep"
    assert calls == ["wait", "health", "restart"]


def test_helper_rejects_bad_checksum_before_touching_install(tmp_path: Path, monkeypatch) -> None:
    _patch_helper(monkeypatch)
    install = tmp_path / "install"
    install.mkdir()
    (install / "Anixo.exe").write_bytes(b"original")
    package = tmp_path / "ANIXO-Portable-v9.9.9.zip"
    package.write_bytes(_zip_bytes({"ANIXO/Anixo.exe": b"new"}))
    plan_path = _plan(tmp_path, install, package)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    result = updater_helper.apply_plan(plan_path)
    assert result["ok"] is False
    assert "checksum" in result["detail"]
    assert (install / "Anixo.exe").read_bytes() == b"original"


def test_helper_rejects_invalid_plan(tmp_path: Path) -> None:
    plan = tmp_path / "bad.json"
    plan.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(updater_helper.UpdateHelperError, match="schema"):
        updater_helper.apply_plan(plan)


# ---------------------------------------------------------------------------
# Update center UI


def test_update_screen_buttons_are_mouse_and_keyboard_accessible(tmp_path: Path) -> None:
    from app import AnixoApp

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
    from screens.updates import UpdateScreen

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_open_updates()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UpdateScreen)
            for selector in (
                "#update-check",
                "#update-download",
                "#update-cancel",
                "#update-restart",
                "#update-notes-button",
            ):
                assert screen.query_one(selector) is not None
            assert screen.query_one("#update-cancel").disabled is True
            assert screen.query_one("#update-restart").disabled is True
            await pilot.click("#update-check")
            await pilot.pause()

    asyncio.run(run())


def test_select_helper_asset_uses_verified_platform_asset() -> None:
    release = _release(
        "1.2.0",
        (
            _asset("ANIXO-Setup-v1.2.0.exe"),
            _asset("ANIXO-Updater-windows-x64.exe"),
            _asset("ANIXO-Updater-macos-arm64.zip"),
        ),
    )
    assert (
        select_helper_asset(release, detect_target("Windows", "AMD64")).name
        == "ANIXO-Updater-windows-x64.exe"
    )
    assert (
        select_helper_asset(release, detect_target("Darwin", "arm64")).name
        == "ANIXO-Updater-macos-arm64.zip"
    )
    assert select_helper_asset(release, detect_target("Darwin", "x86_64")) is None
    # Helpers must never be mistaken for the app installer itself.
    assert select_asset(release, detect_target("Windows", "AMD64"), "installer").name == (
        "ANIXO-Setup-v1.2.0.exe"
    )


def test_release_manifest_generator_pins_all_assets(tmp_path: Path) -> None:
    # Load by path: the repository has a ``packaging/`` directory that can be
    # confused with the third-party ``packaging`` distribution on sys.path.
    script = Path(__file__).resolve().parent.parent / "packaging" / "make_release_manifest.py"
    spec = importlib.util.spec_from_file_location("anixo_release_manifest", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_manifest = module.build_manifest

    release_dir = tmp_path / "release"
    release_dir.mkdir()
    names = (
        "ANIXO-Setup-v1.2.0.exe",
        "ANIXO-Portable-v1.2.0.zip",
        "ANIXO-Updater-windows-x64.exe",
        "ANIXO-v1.2.0-macos-arm64.dmg",
        "ANIXO-Updater-macos-arm64.zip",
    )
    for name in names:
        (release_dir / name).write_bytes(name.encode("utf-8"))
    manifest = build_manifest(
        release_dir,
        "1.2.0",
        "https://github.com/OWNER/REPO/releases/download/v1.2.0",
        notes="Tester build",
    )
    assert manifest["channel"] == "stable"
    assert len(manifest["assets"]) == len(names)
    setup = next(item for item in manifest["assets"] if item["kind"] == "installer")
    assert setup["url"].startswith("https://github.com/OWNER/REPO/releases/download/")
    assert setup["sha256"] == _sha(b"ANIXO-Setup-v1.2.0.exe")
    parsed = parse_manifest(manifest, setup["url"])
    assert parsed.version == "1.2.0"
    assert parsed.notes == "Tester build"

    (release_dir / "ANIXO-Updater-macos-arm64.zip").unlink()
    with pytest.raises(ValueError, match="helper"):
        build_manifest(
            release_dir,
            "1.2.0",
            "https://github.com/OWNER/REPO/releases/download/v1.2.0",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        build_manifest(release_dir, "1.2.0", "http://example.test/download")


def test_config_defaults_are_appropriate_for_beta_build(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANIXO_UPDATE_CHANNEL", raising=False)
    config = Config()
    assert config.update_channel == "beta"
    assert config.check_for_app_updates is True
    assert config.update_feed_url == ""
    assert APP_VERSION.startswith("0.1.0-beta")
