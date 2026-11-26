"""Offline tests for redacted global crash reporting.

All files live in ``tmp_path``.  Hook replacements are scoped by
``monkeypatch`` to a single test, never skip a production check, and never
launch a dependency or an installed helper.  No real user log directory is
used by these tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button

import main as main_module
from app import AnixoApp
from screens.crash_report import CrashReportScreen
from screens.diagnostics import DiagnosticsScreen
from services.crash_report import CrashReporter


def _error(message: str) -> ValueError:
    try:
        raise ValueError(message)
    except ValueError as error:
        return error


def test_planned_exits_are_ignored(tmp_path: Path) -> None:
    reporter = CrashReporter(tmp_path / "logs")
    for error in (SystemExit(0), KeyboardInterrupt(), asyncio.CancelledError()):
        assert reporter.handle_exception(error) is None
    assert not reporter.path.exists()


def test_report_is_redacted_and_size_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reporter = CrashReporter(tmp_path / "logs", max_report_chars=1200, dedupe_seconds=0)
    token_value = "abc123XYZ"
    key_value = "topsecretXYZ"
    signature_value = "signedXYZ"
    error = _error(
        f"Bearer {token_value} api_key={key_value} "
        f"https://user:pass@example.test/?sig={signature_value} " + "p" * 5000
    )

    report = reporter.handle_exception(error)
    assert report is not None
    assert len(report.traceback) <= 600
    saved = reporter.path.read_text(encoding="utf-8")
    assert len(saved.splitlines()) == 1
    assert len(saved.splitlines()[0].encode("utf-8")) <= 1200
    stderr = capsys.readouterr().err
    for value in (token_value, key_value, signature_value, "user:pass"):
        assert value not in saved
        assert value not in stderr


def test_deduplication_suppresses_repeated_failures(tmp_path: Path) -> None:
    clock = [1000.0]
    reporter = CrashReporter(
        tmp_path / "logs", dedupe_seconds=30, now_fn=lambda: clock[0]
    )
    first = reporter.handle_exception(_error("repeat-value"))
    second = reporter.handle_exception(_error("repeat-value"))
    clock[0] += 31
    third = reporter.handle_exception(_error("repeat-value"))

    assert first is not None and second is None and third is not None
    assert first.id != third.id
    assert len(reporter.path.read_text(encoding="utf-8").splitlines()) == 2


def test_reentrancy_guard_falls_back_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class ReentrantReporter(CrashReporter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._probed = False

        def _emit_fallback(
            self,
            message: str,
            *,
            saved: bool | None,
            limit: int = 500,
        ) -> None:
            if not self._probed:
                self._probed = True
                nested = self.handle_exception(_error("nested-value"))
                assert nested is None
            super()._emit_fallback(message, saved=saved, limit=limit)

    reporter = ReentrantReporter(tmp_path / "logs")
    assert reporter.handle_exception(_error("original-value")) is not None
    assert "re-entered" in capsys.readouterr().err
    assert len(reporter.path.read_text(encoding="utf-8").splitlines()) == 1


def test_count_cap_stops_writing_without_deleting_reports(tmp_path: Path) -> None:
    reporter = CrashReporter(tmp_path / "logs", max_reports=2, dedupe_seconds=0)
    first = reporter.handle_exception(_error("first-value"))
    second = reporter.handle_exception(_error("second-value"))
    third = reporter.handle_exception(_error("third-value"))

    assert first is not None and second is not None and third is not None
    saved = reporter.path.read_text(encoding="utf-8")
    assert len(saved.splitlines()) == 2
    assert "first-value" in saved and "second-value" in saved
    assert "third-value" not in saved
    assert third.id not in saved
    assert json.loads(saved.splitlines()[0])["id"] == first.id


def test_handlers_install_idempotently_and_restore(tmp_path: Path) -> None:
    original = sys.excepthook
    reporter = CrashReporter(tmp_path / "logs")
    reporter.install()
    installed = sys.excepthook
    reporter.install()
    assert sys.excepthook is installed

    loop = asyncio.new_event_loop()
    try:
        assert reporter.attach_loop(loop) is True
        handler = loop.get_exception_handler()
        assert reporter.attach_loop(loop) is True
        assert loop.get_exception_handler() is handler
        reporter.uninstall()
    finally:
        reporter.uninstall()
        loop.close()
    assert sys.excepthook is original
    assert loop.get_exception_handler() is None


def test_report_screen_copies_only_redacted_text(tmp_path: Path) -> None:
    from tests.test_app import _test_app

    app = _test_app(tmp_path)
    text = "Redacted report — Bearer <redacted>"

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            screen = CrashReportScreen(text)
            await app.push_screen(screen)
            await pilot.pause()
            assert isinstance(app.screen, CrashReportScreen)
            button = screen.query_one("#crash-report-copy", Button)
            button.post_message(Button.Pressed(button))
            await pilot.pause()
            assert app.clipboard == text

    asyncio.run(run())


def test_diagnostics_screen_offers_crash_report_action(tmp_path: Path) -> None:
    from tests.test_app import _test_app

    app = _test_app(tmp_path)
    calls: list[bool] = []

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            screen = DiagnosticsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            app.action_crash_report = lambda: calls.append(True)  # type: ignore[method-assign]
            button = screen.query_one("#diagnostics-crash", Button)
            button.post_message(Button.Pressed(button))
            await pilot.pause()
            assert calls == [True]

    asyncio.run(run())


def test_main_installs_reporter_only_for_app_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class FakeReporter:
        pass

    monkeypatch.setattr(main_module, "default_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(
        main_module,
        "install_global",
        lambda log_dir: calls.append("install") or FakeReporter(),
    )
    monkeypatch.setattr(
        main_module, "uninstall_global", lambda: calls.append("uninstall")
    )
    monkeypatch.setattr(
        main_module.AnixoApp, "run", lambda self: calls.append("run")
    )
    code = main_module.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--config",
            str(tmp_path / "config.json"),
        ]
    )
    assert code == 0
    assert calls == ["install", "run", "uninstall"]

    calls.clear()
    monkeypatch.setattr(
        main_module,
        "install_global",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reporter installed for a non-app command")
        ),
    )
    assert main_module.main(["--version"]) == 0
    assert calls == []
