"""Offline tests for conservative, message-only runtime health signals.

All fixtures are built in ``tmp_path`` or as in-memory dataclasses.  The test
hooks below replace diagnostic data providers only; they are scoped by
``monkeypatch`` to one test, never skip a production check, and never launch a
dependency or an installed helper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from config import Config
from screens.episode_selector import EpisodeSelectorScreen
from screens.setup import SetupScreen
from services.diagnostics import build_report
from services.terminal import detect_capabilities
from services.health_checks import (
    HealthStatus,
    from_dependency_statuses,
    from_diagnostic_lines,
    guard_rows_unchanged,
    health_lines,
    message_with_health,
    network_status,
    provider_status,
)
from services.provider import ProviderEpisode, ProviderError
from services.setup import DependencyStatus, SetupSnapshot
from version import APP_VERSION

from tests.test_episodes import make_app, sample_anime


def test_health_status_uses_allowed_statuses_and_redacts_secrets() -> None:
    status = HealthStatus(
        "ani-cli",
        "failed",
        "Bearer abc123 api_key=topsecret https://user:pass@example.test/?sig=signed",
    )
    assert status.status == "FAILED"
    line = status.line()
    assert "abc123" not in line
    assert "topsecret" not in line
    assert "user:pass" not in line
    assert "signed" not in line
    assert "<redacted>" in line
    assert "FAILED" in line
    with pytest.raises(ValueError, match="Unsupported health status"):
        HealthStatus("bad", "PASS", "")


def test_missing_evidence_is_never_reported_as_success() -> None:
    unknown = from_diagnostic_lines(["Platform: Test OS", "mpv: not found"])
    statuses = {item.name: item.status for item in unknown}
    assert statuses == {
        "ani-cli": "UNKNOWN",
        "mpv": "FAILED",
        "Git Bash": "UNKNOWN",
        "ffmpeg": "UNKNOWN",
        "fzf": "UNKNOWN",
    }
    assert from_diagnostic_lines([])[0].status == "UNKNOWN"
    assert network_status(None).status == "NOT TESTED"
    assert provider_status(rows=None).status == "NOT TESTED"


def test_dependency_statuses_report_requirements_without_mutating_them() -> None:
    before = (
        SimpleNamespace(key="ani-cli", label="ani-cli", required=True, installed=False),
        SimpleNamespace(key="mpv", label="mpv", required=True, installed=True),
        SimpleNamespace(key="ffmpeg", label="ffmpeg", required=False, installed=False),
        SimpleNamespace(
            key="ani-cli",
            label="ani-cli",
            required=True,
            installed=True,
            patch_error="Bearer secret-value",
        ),
    )
    statuses = from_dependency_statuses(before)
    assert [(item.name, item.status) for item in statuses] == [
        ("ani-cli", "FAILED"),
        ("mpv", "PASSED"),
        ("ffmpeg", "UNKNOWN"),
        ("ani-cli", "FAILED"),
    ]
    assert "secret-value" not in health_lines(statuses)[-1]
    assert before[0].installed is False
    assert before[1].installed is True


def test_diagnose_report_has_redacted_health_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This hook only replaces diagnostic strings for this one unit test.
    monkeypatch.setattr(
        "services.diagnostics.dependency_diagnostics",
        lambda config, ani_cli: [
            "ani-cli script: not found",
            "mpv: not found",
            "Git Bash: api_key=discard-me",
        ],
    )
    monkeypatch.setattr("services.diagnostics.check_network", lambda *a, **k: False)
    report = build_report(
        app_version=APP_VERSION,
        install_dir=tmp_path / "install",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        config=Config(),
        ani_cli=None,
        capabilities=detect_capabilities(),
        cache_counts={},
    )
    assert "Health checks\n-------------" in report
    assert "ani-cli: FAILED" in report
    assert "mpv: FAILED" in report
    assert "Network: FAILED" in report
    assert "discard-me" not in report


def test_setup_screen_shows_health_without_changing_dependency_rows() -> None:
    dependencies = (
        DependencyStatus(
            key="ani-cli",
            label="ani-cli",
            description="Required",
            required=True,
            installed=False,
            version="not found",
            detail="not detected",
            official_url="https://example.test/ani-cli",
            winget_id=None,
            installable=True,
        ),
        DependencyStatus(
            key="mpv",
            label="mpv",
            description="Required",
            required=True,
            installed=True,
            version="detected",
            detail="detected",
            official_url="https://example.test/mpv",
            winget_id=None,
            installable=False,
        ),
    )
    snapshot = SetupSnapshot(
        os_name="TestOS",
        os_version="1",
        architecture="test",
        app_version=APP_VERSION,
        terminal_summary="test",
        renderer="off",
        network_ok=False,
        dependencies=dependencies,
        legacy_data_path=None,
        legacy_data_counts={},
    )
    screen = SetupScreen(snapshot)
    text = screen._items_text(1)
    assert "Health checks:" in text
    assert "ani-cli: FAILED" in text
    assert "mpv: PASSED" in text
    assert screen.snapshot.dependencies == dependencies
    assert dependencies[0].installed is False


def test_health_message_only_appends_a_redacted_note() -> None:
    failed = HealthStatus("Provider episodes", "FAILED", "Bearer token-value")
    message = message_with_health("lookup https://example.test/?sig=query-value", failed)
    assert "Bearer token-value" not in message
    assert "query-value" not in message
    assert "lookup" in message
    assert "Health: FAILED" in message
    assert message.count("Health:") == 1
    assert message_with_health(message, failed).count("Health:") == 1

    passed = HealthStatus("Provider episodes", "PASSED", "2 rows")
    assert message_with_health("original", passed) == "original"


def test_guard_rows_unchanged_fails_closed() -> None:
    before = [
        SimpleNamespace(number="1", provider_episode_id="1001", title="One"),
        SimpleNamespace(number="2", provider_episode_id="1002", title="Two"),
    ]
    unchanged = list(before)
    guard_rows_unchanged(before, unchanged)

    filtered = [unchanged[0]]
    with pytest.raises(RuntimeError, match="may not replace"):
        guard_rows_unchanged(before, filtered)

    invented = unchanged + [
        SimpleNamespace(number="3", provider_episode_id="1003", title="Unaired")
    ]
    with pytest.raises(RuntimeError, match="may not replace"):
        guard_rows_unchanged(before, invented)


def test_selector_health_note_never_changes_provider_rows(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime(status="RELEASING")
    app.database.upsert_anime(anime)
    calls = 0
    secret = "signed-secret-value"

    def episodes(source_id: str) -> list[ProviderEpisode]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(f"lookup failed https://example.test/?sig={secret}")
        return [ProviderEpisode("1", "1001", "Arrival")]

    app.provider.episodes = episodes  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            for _ in range(5):
                await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert selector.episodes == []
            assert selector._source_error and "lookup failed" in selector._source_error
            assert "Health: FAILED" in selector._source_error
            assert secret not in selector._source_error
            visible = str(selector.query_one(".state-message", Static).content)
            assert secret not in visible

            await pilot.press("r")
            for _ in range(5):
                await pilot.pause()
            assert [(item.number, item.title) for item in selector.episodes] == [
                ("1", "Arrival")
            ]
            assert selector._source_error is None

    asyncio.run(run())
