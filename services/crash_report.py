"""Global, redacted crash reporting with conservative on-disk caps.

The reporter records Python and asyncio exceptions into one append-only
JSON-lines file in ANIXO's log directory.  It never deletes existing reports.
Planned exits are ignored, repeated identical failures are de-duplicated, and
all text is redacted before it is written, displayed, or echoed to stderr.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from services.health_checks import redact_sensitive_text
from services.update_log import redact_update_text
from version import APP_VERSION

MAX_REPORTS = 5
MAX_REPORT_CHARS = 8000
MAX_STDERR_CHARS = 500
DEDUPE_SECONDS = 30.0
_MAX_FINGERPRINTS = 64

_PLANNED_EXITS: tuple[type[BaseException], ...] = (
    SystemExit,
    KeyboardInterrupt,
    GeneratorExit,
    asyncio.CancelledError,
)

_ACTIVE: "CrashReporter | None" = None


def redact_crash_text(value: object, *, limit: int = MAX_REPORT_CHARS) -> str:
    """Apply both shared redaction layers before any crash text is surfaced."""
    return redact_sensitive_text(redact_update_text(str(value)), limit=limit)


def is_planned_exit(error: BaseException) -> bool:
    """Return True for exits that are part of normal shutdown or cancellation."""
    return isinstance(error, _PLANNED_EXITS)


@dataclass(frozen=True, slots=True)
class CrashReport:
    """One redacted crash record."""

    id: str
    at: str
    app_version: str
    source: str
    exception_type: str
    message: str
    traceback: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": redact_crash_text(self.id, limit=100),
            "at": redact_crash_text(self.at, limit=100),
            "app_version": redact_crash_text(self.app_version, limit=100),
            "source": redact_crash_text(self.source, limit=100),
            "exception_type": redact_crash_text(self.exception_type, limit=200),
            "message": redact_crash_text(self.message, limit=1000),
            "traceback": redact_crash_text(self.traceback, limit=4000),
        }

    @classmethod
    def from_mapping(cls, data: Any) -> "CrashReport | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                id=redact_crash_text(data.get("id", ""), limit=100),
                at=redact_crash_text(data.get("at", ""), limit=100),
                app_version=redact_crash_text(data.get("app_version", ""), limit=100),
                source=redact_crash_text(data.get("source", ""), limit=100),
                exception_type=redact_crash_text(
                    data.get("exception_type", ""), limit=200
                ),
                message=redact_crash_text(data.get("message", ""), limit=1000),
                traceback=redact_crash_text(data.get("traceback", ""), limit=4000),
            )
        except (TypeError, ValueError):
            return None

    def summary(self) -> str:
        return (
            f"{self.exception_type}: {self.message} "
            f"(source={self.source}, id={self.id})"
        )


class CrashReporter:
    """Installable, idempotent exception hooks for one ANIXO session."""

    def __init__(
        self,
        log_dir: Path | str,
        *,
        app_version: str = APP_VERSION,
        max_reports: int = MAX_REPORTS,
        max_report_chars: int = MAX_REPORT_CHARS,
        dedupe_seconds: float = DEDUPE_SECONDS,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(log_dir).resolve() / "crash-reports.jsonl"
        self.app_version = app_version
        self.max_reports = max(1, int(max_reports))
        self.max_report_chars = max(512, int(max_report_chars))
        self.dedupe_seconds = max(0.0, float(dedupe_seconds))
        self._now = now_fn or time.time
        self._seen: dict[str, float] = {}
        self._handling = False
        self._installed = False
        self._original_excepthook: Callable[..., Any] | None = None
        self._loops: dict[Any, Callable[..., Any] | None] = {}
        # Stable bound-method objects make repeated install/uninstall idempotent.
        self._sys_hook = self._sys_excepthook
        self._loop_hook = self._loop_exception_handler

    # ------------------------------------------------------------------
    # Hook installation

    def install(self) -> "CrashReporter":
        """Install sys/asyncio hooks once; repeated calls are harmless."""
        if self._installed:
            return self
        self._original_excepthook = sys.excepthook
        sys.excepthook = self._sys_hook
        self._installed = True
        return self

    def attach_loop(self, loop: Any | None = None) -> bool:
        """Attach to the running event loop once, if one is available."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
        if loop in self._loops:
            return True
        self._loops[loop] = loop.get_exception_handler()
        loop.set_exception_handler(self._loop_hook)
        return True

    def uninstall(self) -> None:
        """Restore previously installed hooks without touching report files."""
        if self._original_excepthook is not None and sys.excepthook is self._sys_hook:
            sys.excepthook = self._original_excepthook
        for loop, handler in self._loops.items():
            loop.set_exception_handler(handler)
        self._loops.clear()
        self._installed = False

    def _sys_excepthook(
        self,
        exc_type: type[BaseException],
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.handle_exception(error or exc_type(), source="sys", tb=tb)

    def _loop_exception_handler(self, loop: Any, context: dict[str, Any]) -> None:
        error = context.get("exception")
        if not isinstance(error, BaseException):
            message = str(context.get("message") or "asyncio exception")
            error = RuntimeError(message)
        self.handle_exception(error, source="asyncio", tb=error.__traceback__)

    # ------------------------------------------------------------------
    # Reporting

    def handle_exception(
        self,
        error: BaseException,
        *,
        source: str = "sys",
        tb: TracebackType | None = None,
    ) -> CrashReport | None:
        """Create, cap, persist, and summarize one non-planned exception."""
        if is_planned_exit(error):
            return None
        if self._handling:
            self._emit_fallback(
                "Crash reporter re-entered; nested exception was ignored.", saved=None
            )
            return None

        self._handling = True
        try:
            fingerprint = self._fingerprint(error, tb)
            if not self._remember(fingerprint):
                return None
            frames = traceback.format_exception(
                type(error), error, tb or error.__traceback__
            )
            report = CrashReport(
                id=uuid.uuid4().hex[:12],
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                app_version=self.app_version,
                source=source,
                exception_type=type(error).__name__,
                message=redact_crash_text(str(error), limit=self.max_report_chars // 4),
                traceback=redact_crash_text(
                    "".join(frames), limit=self.max_report_chars // 2
                ),
            )
            saved = self._write(report)
            self._emit_stderr(report.summary(), saved=saved)
            return report
        finally:
            self._handling = False

    def _fingerprint(
        self, error: BaseException, tb: TracebackType | None
    ) -> str:
        detail = ""
        frames = traceback.extract_tb(tb or error.__traceback__ or None)
        if frames:
            last = frames[-1]
            detail = f"{last.filename}:{last.lineno}:{last.name}"
        raw = f"{type(error).__name__}|{str(error)}|{detail}"
        digest = hashlib.sha256(
            redact_crash_text(raw, limit=1000).encode("utf-8")
        ).hexdigest()
        return digest

    def _remember(self, fingerprint: str) -> bool:
        now = float(self._now())
        cutoff = now - self.dedupe_seconds
        self._seen = {
            key: seen_at for key, seen_at in self._seen.items() if seen_at >= cutoff
        }
        if fingerprint in self._seen:
            return False
        self._seen[fingerprint] = now
        if len(self._seen) > _MAX_FINGERPRINTS:
            oldest = next(iter(self._seen))
            self._seen.pop(oldest, None)
        return True

    # ------------------------------------------------------------------
    # Storage and fallback

    def _read_records(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                data = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                records.append(data)
        return records

    def _write(self, report: CrashReport) -> bool:
        try:
            records = self._read_records()
            if len(records) >= self.max_reports:
                return False
            payload = report.to_mapping()
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(line.encode("utf-8")) > self.max_report_chars:
                payload["traceback"] = redact_crash_text(
                    payload["traceback"], limit=self.max_report_chars // 4
                )
                line = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            if len(line.encode("utf-8")) > self.max_report_chars:
                return False
            existing_bytes = self.path.stat().st_size if self.path.exists() else 0
            if existing_bytes + len(line.encode("utf-8")) + 1 > (
                self.max_reports * self.max_report_chars
            ):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            return True
        except OSError:
            return False

    def _emit_stderr(self, message: str, *, saved: bool | None) -> None:
        self._emit_fallback(message, saved=saved)

    def _emit_fallback(
        self,
        message: str,
        *,
        saved: bool | None,
        limit: int = MAX_STDERR_CHARS,
    ) -> None:
        """Print a fixed, redacted fallback without re-entering reporting."""
        status = "saved" if saved is True else (
            "not saved" if saved is False else "ignored"
        )
        text = redact_crash_text(
            f"ANIXO crash report ({status}): {message}",
            limit=limit,
        )
        try:
            print(text, file=sys.stderr)
        except OSError:
            return

    def latest(self) -> CrashReport | None:
        """Return the newest saved report, re-redacted when read."""
        for data in reversed(self._read_records()):
            report = CrashReport.from_mapping(data)
            if report is not None:
                return report
        return None


def install_global(
    log_dir: Path | str, **kwargs: Any
) -> CrashReporter:
    """Install one process-wide reporter; repeated calls return the same one."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = CrashReporter(log_dir, **kwargs).install()
    return _ACTIVE


def uninstall_global() -> None:
    """Remove the process-wide reporter, if one was installed."""
    global _ACTIVE
    if _ACTIVE is not None:
        _ACTIVE.uninstall()
        _ACTIVE = None
