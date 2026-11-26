"""Conservative, redacted runtime health signals.

These helpers observe data that ANIXO already has.  They never probe an
installed dependency by executing it, never make a network request, and never
change provider episode rows.  Statuses are limited to ``PASSED``, ``FAILED``,
``UNKNOWN``, and ``NOT TESTED``; missing evidence is reported as unknown or not
tested rather than guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ALLOWED_STATUSES = frozenset({"PASSED", "FAILED", "UNKNOWN", "NOT TESTED"})

_LABEL_SECRET = re.compile(
    r"(?P<label>\b(?:api[ _-]?key|apikey|access[ _-]?token|refresh[ _-]?token|"
    r"token|password|passwd|pwd|secret|client[ _-]?secret|credential|"
    r"authorization|auth)\b)"
    r"(?P<sep>\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s\"',;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>https?://)(?P<credentials>[^/@\s]+:[^/@\s]+)@",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:sig|se|expires?|expiration|x-amz-[a-z0-9_-]+|token|"
    r"access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|key|password|"
    r"passwd|pwd|secret|signature)=)[^&\s]+"
)


def _redact(value: object, *, limit: int = 500) -> str:
    """Remove credentials, userinfo, and signed URL parameters from text."""
    text = str(value)
    text = _LABEL_SECRET.sub(
        lambda match: (
            f"{match.group('label')}{match.group('sep')}"
            f"{match.group('quote')}<redacted>"
        ),
        text,
    )
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _URL_CREDENTIALS.sub(lambda match: f"{match.group('scheme')}<redacted>@", text)
    text = _QUERY_SECRET.sub(r"\1<redacted>", text)
    return text[: max(1, limit)]


def redact_sensitive_text(value: object, *, limit: int = 500) -> str:
    """Public redaction helper shared by health and crash-report surfaces."""
    return _redact(value, limit=limit)


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """One redacted, evidence-based status line."""

    name: str
    status: str
    message: str = ""

    def __post_init__(self) -> None:
        status = str(self.status).upper()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported health status: {status!r}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "name", _redact(self.name))
        object.__setattr__(self, "message", _redact(self.message))

    def line(self) -> str:
        suffix = f" — {self.message}" if self.message else ""
        return f"{self.name}: {self.status}{suffix}"


def health_lines(statuses: Iterable[HealthStatus]) -> tuple[str, ...]:
    """Return display-ready, already-redacted status lines."""
    return tuple(item.line() for item in statuses)


def _diagnostic_value(lines: Iterable[str], key: str) -> str | None:
    wanted = key.strip().lower()
    for line in lines:
        name, separator, value = str(line).partition(":")
        if separator and name.strip().lower() == wanted:
            return value.strip()
    return None


def from_diagnostic_lines(lines: Iterable[str]) -> tuple[HealthStatus, ...]:
    """Classify the existing dependency report without new probes."""
    rows = tuple(str(line) for line in lines)
    results: list[HealthStatus] = []

    def add_observed(name: str, key: str, *, required: bool) -> None:
        value = _diagnostic_value(rows, key)
        if value is None:
            results.append(HealthStatus(name, "UNKNOWN", "not reported"))
        elif value and value.lower() != "not found":
            results.append(HealthStatus(name, "PASSED", "detected"))
        elif required:
            results.append(HealthStatus(name, "FAILED", "required dependency not detected"))
        else:
            results.append(HealthStatus(name, "UNKNOWN", "optional dependency not detected"))

    add_observed("ani-cli", "ani-cli script", required=True)
    add_observed("mpv", "mpv", required=True)
    add_observed("Git Bash", "Git Bash", required=False)
    add_observed("ffmpeg", "ffmpeg", required=False)
    add_observed("fzf", "fzf", required=False)
    return tuple(results)


def from_dependency_statuses(statuses: Iterable[Any]) -> tuple[HealthStatus, ...]:
    """Classify setup wizard dependency states without changing them."""
    results: list[HealthStatus] = []
    for item in statuses:
        name = str(getattr(item, "label", getattr(item, "key", "dependency")))
        patch_error = str(getattr(item, "patch_error", "") or "")
        if patch_error:
            results.append(HealthStatus(name, "FAILED", patch_error))
        elif bool(getattr(item, "installed", False)):
            results.append(HealthStatus(name, "PASSED", "detected"))
        elif bool(getattr(item, "required", False)):
            results.append(HealthStatus(name, "FAILED", "required dependency not detected"))
        else:
            results.append(HealthStatus(name, "UNKNOWN", "optional dependency not detected"))
    return tuple(results)


def network_status(reachable: bool | None) -> HealthStatus:
    """Report only the network probe result supplied by the caller."""
    if reachable is None:
        return HealthStatus("Network", "NOT TESTED", "no probe result supplied")
    if reachable:
        return HealthStatus("Network", "PASSED", "probe reached the configured endpoint")
    return HealthStatus("Network", "FAILED", "probe could not reach the configured endpoint")


def provider_status(
    *, rows: Sequence[Any] | None, error: object | None = None
) -> HealthStatus:
    """Report provider episode health without judging or changing its rows."""
    if error:
        return HealthStatus("Provider episodes", "FAILED", str(error))
    if rows is None:
        return HealthStatus("Provider episodes", "NOT TESTED", "lookup not attempted")
    if not rows:
        return HealthStatus("Provider episodes", "UNKNOWN", "no rows returned; rows unchanged")
    return HealthStatus(
        "Provider episodes", "PASSED", f"{len(rows)} provider row(s) loaded"
    )


def message_with_health(
    base: object | None, status: HealthStatus | None, *, label: str = "Health"
) -> str:
    """Append a redacted status note to a message; never touch episode data."""
    text = _redact(base or "").strip()
    if status is None or status.status == "PASSED":
        return text
    note = f"{_redact(label)}: {status.status} — {status.message}".strip(" —")
    if not note or note in text or f"{label}:" in text:
        return text
    return f"{text}\n{note}".strip()


def _identity(row: Any) -> tuple[str, str, str]:
    if isinstance(row, Mapping):
        return (
            str(row.get("number", "")),
            str(row.get("provider_episode_id", "")),
            str(row.get("title", "")),
        )
    return (
        str(getattr(row, "number", "")),
        str(getattr(row, "provider_episode_id", "")),
        str(getattr(row, "title", "")),
    )


def guard_rows_unchanged(before: Sequence[Any], after: Sequence[Any]) -> None:
    """Fail closed if health handling changed provider or episode rows."""
    first = tuple(before)
    second = tuple(after)
    if len(first) != len(second) or tuple(map(_identity, first)) != tuple(
        map(_identity, second)
    ):
        raise RuntimeError("Health data may not replace, filter, or synthesize episode rows.")
