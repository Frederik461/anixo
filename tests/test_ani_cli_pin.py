"""Offline tests for the developer ani-cli pin checker and patch failures.

Every fixture is built in ``tmp_path``.  No test downloads ani-cli, executes a
candidate, calls ``-U``, or touches an installed/user-managed script.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from config import Config
from platforms import WINDOWS, PlatformAdapter
from services import ani_cli as ani_cli_module
from services import setup as setup_service
from services.ani_cli import AniCliInvocation, AniCliPatchError, AniCliService
from services.ani_cli_updater import (
    AniCliUpdateCheck,
    AniCliUpdateService,
)
from services.dependency_manager import DependencyManager
from services.install_manifest import COMPONENT_ANI_CLI, InstallRecord, ManifestStore
from services.managed_ani_cli import ManagedAniCli
from version import APP_VERSION


def _load_pin_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_ani_cli_pin.py"
    spec = importlib.util.spec_from_file_location("anixo_check_ani_cli_pin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pin = _load_pin_module()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _higher_version() -> str:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", APP_VERSION)
    assert match is not None
    return f"{int(match.group(1)) + 100}.{match.group(2)}.{match.group(3)}"


def _source(*, drift: bool = False) -> bytes:
    search = ani_cli_module._SEARCH_ANCHOR
    if drift:
        search = search.replace("anime_list", "drifted_list")
    head = (
        f"#!/usr/bin/env bash\n# ani-cli {APP_VERSION}\n"
        "base_api=https://example.invalid\n"
    ).encode("utf-8")
    return head + (search + ani_cli_module._MENU_ANCHOR).encode("utf-8") + (
        b"\n" + b"# padding line\n" * 400
    )


def _source_composite(*, drift: bool = False) -> bytes:
    """Synthetic official layout whose result keys are AniListID:siteID:."""
    search = ani_cli_module._SEARCH_ANCHOR_V2
    if drift:
        search = search.replace("anime_list", "drifted_list")
    head = (
        f"#!/usr/bin/env bash\n# ani-cli {APP_VERSION}\n"
        "version_number=synthetic\n"
        "miruro_search() { :; }\n"
        "miruro_episodes() { :; }\n"
        "ANI_CLI_EXACT=0\n"
    ).encode("utf-8")
    return head + (search + ani_cli_module._MENU_ANCHOR).encode("utf-8") + (
        b"\n" + b"# padding line\n" * 400
    )


def _statuses(report: Any) -> dict[str, str]:
    return {item.name: item.status for item in report.checks}


def _details(report: Any) -> str:
    return "\n".join(item.detail for item in report.checks)


class _StubAniCli:
    def __init__(self, path: Path) -> None:
        self.path = path

    def installed_script(self) -> Path | None:
        return self.path


def _update_service(
    tmp_path: Path, script: Path, *, owned: bool, source: bytes
) -> AniCliUpdateService:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    digest = _sha(source).upper()
    store = ManifestStore(data, PlatformAdapter(WINDOWS))
    manifest = store.new_manifest("windows-installer")
    manifest.upsert(
        InstallRecord(
            key=COMPONENT_ANI_CLI,
            name="ani-cli",
            platform="windows",
            installed_by_anixo=owned,
            managed=owned,
            safe_to_remove=owned,
            install_location=str(script),
            version=APP_VERSION,
            installed_at="2026-11-25T00:00:00+00:00",
            source="https://example.invalid/ani-cli",
            install_method="pinned-script",
            expected_sha256=digest,
            observed_sha256=digest,
            paths=(str(script),),
            config_keys=("ani_cli_path",),
        )
    )
    store.save(manifest)
    config = Config()
    config.ani_cli_path = str(script)
    manager = DependencyManager(
        config,
        data,
        cache,
        PlatformAdapter(WINDOWS),
        store,
        managed_ani_cli_dir=script.parent if owned else None,
    )
    if owned:
        ManagedAniCli(script.parent).record(
            script=script,
            sha256=digest,
            version=APP_VERSION,
            source="https://example.invalid/ani-cli",
            install_method="pinned-script",
        )
    return AniCliUpdateService(
        config,
        data,
        ani_cli=_StubAniCli(script),
        manager=manager,
        adapter=PlatformAdapter(WINDOWS),
    )


# ---------------------------------------------------------------------------
# Digest and patch gates


def test_candidate_digest_mismatch_is_rejected() -> None:
    source = _source()
    report = pin.run_check(source, expected_sha256="0" * 64)
    assert report.ok is False
    assert "mismatch" in report.reason.lower()
    assert _statuses(report)["digest"] == "FAILED"
    assert "patch.apply" not in _statuses(report)


def test_anchor_drift_is_rejected_with_clear_message() -> None:
    source = _source(drift=True)
    report = pin.run_check(source, expected_sha256=_sha(source))
    statuses = _statuses(report)
    assert report.ok is False
    assert statuses["patch.search_anchor"] == "FAILED"
    assert statuses["patch.menu_anchor"] == "PASSED"
    assert "out of date" in report.reason.lower()
    with pytest.raises(AniCliPatchError, match="out of date"):
        ani_cli_module.patch_exact_provider_selection(source.decode("utf-8"))


def test_patch_applies_to_copy_only(tmp_path: Path) -> None:
    source = _source()
    original = tmp_path / "candidate"
    original.write_bytes(source)
    stage = tmp_path / "stage"
    stage.mkdir()
    report = pin.run_check(
        path=original, expected_sha256=_sha(source), work_dir=stage
    )
    assert report.ok is True, "\n".join(report.lines())
    assert original.read_bytes() == source
    assert (stage / "candidate").read_bytes() == source
    assert ani_cli_module._EXACT_MARKER in (
        stage / "ani-cli-patched"
    ).read_text(encoding="utf-8")


def test_composite_layout_patch_is_exact_and_layout_specific() -> None:
    source = _source_composite()
    patched = ani_cli_module.patch_exact_provider_selection(
        source.decode("utf-8")
    )
    assert ani_cli_module._EXACT_MARKER in patched
    assert (
        ani_cli_module._SEARCH_ANCHOR_V2 + ani_cli_module._EXACT_FILTER_V2
        in patched
    )
    assert "$1 == id" in patched
    assert "split($1, parts," not in patched
    assert "hianime_search" not in patched
    assert ani_cli_module._SEARCH_ANCHOR not in patched


def test_composite_layout_drift_is_rejected() -> None:
    source = _source_composite(drift=True)
    with pytest.raises(AniCliPatchError, match="out of date"):
        ani_cli_module.patch_exact_provider_selection(source.decode("utf-8"))


def test_structural_validation_accepts_only_known_layouts() -> None:
    assert setup_service._looks_like_ani_cli(_source()) is True
    assert setup_service._looks_like_ani_cli(_source_composite()) is True
    assert setup_service._looks_like_ani_cli(b"#!/bin/sh\n" + b"x" * 5000) is False


def test_composite_environment_uses_exact_provider_id(tmp_path: Path) -> None:
    script = tmp_path / "managed" / "ani-cli"
    script.parent.mkdir()
    script.write_bytes(_source_composite())
    service = AniCliService(
        Config(ani_cli_path=str(script), ani_cli_base_url="https://example.invalid")
    )
    service.supports_exit_after_play = lambda: False  # type: ignore[method-assign]
    invocation = AniCliInvocation("direct", "ani-cli", (), str(script))
    numeric = re.match(r"^v?\d+\.\d+\.(\d+)", APP_VERSION)
    assert numeric is not None
    exact_id = f"{numeric.group(1)}:site-{numeric.group(1)}:"

    environment = service._environment(invocation, provider_id=exact_id)
    assert environment["ANI_CLI_PROVIDER_ID"] == exact_id
    assert environment["ANI_CLI_EXACT"] == "0"
    assert environment["ANI_CLI_MENU"] == "false"

    unmatched = service._environment(invocation, provider_id="no-composite-id")
    assert unmatched["ANI_CLI_PROVIDER_ID"] == "no-composite-id"


def test_pin_checker_accepts_composite_layout() -> None:
    source = _source_composite()
    report = pin.run_check(source, expected_sha256=_sha(source))
    assert report.ok is True, "\n".join(report.lines())
    statuses = _statuses(report)
    assert statuses["patch.search_anchor"] == "PASSED"
    assert statuses["patch.menu_anchor"] == "PASSED"


def test_url_candidate_uses_injected_fetcher_only() -> None:
    source = _source()
    seen: list[str] = []

    def fake_fetch(url: str) -> bytes:
        seen.append(url)
        return source

    url = "https://example.invalid/ani-cli?placeholder=not-a-real-secret"
    report = pin.run_check(
        url=url, expected_sha256=_sha(source), fetcher=fake_fetch
    )
    assert report.ok is True, "\n".join(report.lines())
    assert seen == [url]
    assert "not-a-real-secret" not in "\n".join(report.lines())


# ---------------------------------------------------------------------------
# Real command-construction assertions


def test_exact_command_assertions_all_pass() -> None:
    source = _source()
    report = pin.run_check(source, expected_sha256=_sha(source))
    statuses = _statuses(report)
    assert report.ok is True, "\n".join(report.lines())
    assert set(statuses) == {
        "digest",
        "patch.search_anchor",
        "patch.menu_anchor",
        "patch.apply",
        "patch.marker",
        "command.build",
        "command.exact_provider_id",
        "command.exact_selection",
        "command.exact_episode",
        "command.environment",
        "command.no_continue_with_exact",
        "command.dub_off_default",
        "command.dub_only_requested",
        "command.no_title_only_exact",
        "command.no_self_update",
    }
    assert set(statuses.values()) == {"PASSED"}
    details = _details(report)
    assert "argv=" in details
    assert "commands=" in details


def test_dub_only_when_requested() -> None:
    source = _source()
    report = pin.run_check(source, expected_sha256=_sha(source))
    statuses = _statuses(report)
    assert statuses["command.dub_off_default"] == "PASSED"
    assert statuses["command.dub_only_requested"] == "PASSED"

    service = AniCliService(Config(ani_cli_base_url="https://example.invalid"))
    service.supports_exit_after_play = lambda: False  # type: ignore[method-assign]
    sub = service.exact_arguments(
        exact=True, provider_id="pin-check-result", episode="7", dub=False
    )
    dub = service.exact_arguments(
        exact=True, provider_id="pin-check-result", episode="7", dub=True
    )
    assert sub is not None and dub is not None
    assert "--dub" not in sub
    assert dub.count("--dub") == 1


def test_no_continue_with_exact_episode_and_no_self_update() -> None:
    source = _source()
    report = pin.run_check(source, expected_sha256=_sha(source))
    statuses = _statuses(report)
    assert statuses["command.no_continue_with_exact"] == "PASSED"
    assert statuses["command.no_title_only_exact"] == "PASSED"
    assert statuses["command.no_self_update"] == "PASSED"

    service = AniCliService(Config(ani_cli_base_url="https://example.invalid"))
    service.supports_exit_after_play = lambda: False  # type: ignore[method-assign]
    sub = service.exact_arguments(
        exact=True, provider_id="pin-check-result", episode="7"
    )
    title_only = service.exact_arguments(exact=True, title="a title", episode="7")
    assert sub is not None and "-c" not in sub and "-e" in sub
    assert title_only is None
    assert all("-U" not in item for item in _details(report).splitlines())


# ---------------------------------------------------------------------------
# Runtime fail-closed behavior


def test_unowned_user_ani_cli_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir()
    script.write_bytes(source)
    service = _update_service(tmp_path, script, owned=False, source=source)

    def fake_install(destination: Path, *, overwrite: bool = False):
        assert overwrite is False
        destination.mkdir(parents=True, exist_ok=True)
        managed = destination / "ani-cli"
        managed.write_bytes(source)
        return setup_service.InstallResult(
            True,
            "managed copy created",
            "verified",
            source="https://example.invalid/managed-ani-cli",
            version=APP_VERSION,
            install_location=str(managed),
            sha256=_sha(source).upper(),
        )

    monkeypatch.setattr(setup_service, "install_ani_cli", fake_install)
    check = AniCliUpdateCheck(
        APP_VERSION,
        APP_VERSION,
        False,
        "2026-11-25T00:00:00+00:00",
        "https://example.invalid/ani-cli",
        _sha(source).upper(),
        "",
    )
    result = service.update(check)
    assert result.ok is True
    assert "left untouched" in result.message
    assert script.read_bytes() == source
    managed = service._manager().managed_ani_cli.script_path
    assert managed.parent != script.parent
    assert managed.read_bytes() == source


def test_runtime_patch_failure_is_typed_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(drift=True)
    script = tmp_path / "tools" / "ani-cli"
    script.parent.mkdir()
    script.write_bytes(source)
    service = _update_service(tmp_path, script, owned=True, source=source)

    detected = service.detect_installed()
    assert detected.patch_error
    assert "out of date" in detected.patch_error.lower()
    assert "out of date" in detected.detail.lower()

    monkeypatch.setattr(setup_service, "_find_git_bash", lambda *a, **k: None)
    monkeypatch.setattr(service, "_download", lambda url, cancel=None: source)
    check = AniCliUpdateCheck(
        APP_VERSION,
        _higher_version(),
        True,
        "2026-11-25T00:00:00+00:00",
        "https://example.invalid/ani-cli",
        _sha(source).upper(),
        "",
    )
    result = service.update(check)
    assert result.ok is False
    assert "out of date" in result.detail.lower()
    assert script.read_bytes() == source


def test_exact_playback_refuses_unpatched_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(drift=True)
    script = tmp_path / "ani-cli"
    script.write_bytes(source)
    digest = _sha(source).upper()
    known = set(ani_cli_module._KNOWN_PROVIDER_SHA256) | {digest}
    monkeypatch.setattr(ani_cli_module, "_KNOWN_PROVIDER_SHA256", known)

    service = AniCliService(Config(ani_cli_base_url="https://example.invalid"))
    monkeypatch.setattr(service, "installed_script", lambda: script)
    monkeypatch.setattr(
        service,
        "resolve_invocation",
        lambda: AniCliInvocation("direct", "ani-cli", (), str(script)),
    )

    def no_process(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an unpatched script must never be launched")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    process = asyncio.run(
        service.spawn(exact=True, provider_id="pin-check-result", episode="7")
    )
    assert process is None
