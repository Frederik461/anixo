"""Append-only, redacted logging for update operations.

Update logs live in ANIXO's data directory, never in the application bundle.
They record decisions and failures without credentials, URL userinfo, query
tokens, or local secrets.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)\S+"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)\b"
        ),
        "<redacted>",
    ),
    (
        re.compile(
            r"(?i)([?&](?:sig|se|expires?|expiration|x-amz-[a-z0-9_-]+|"
            r"token|access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|key|"
            r"password|passwd|pwd|secret|signature)=)[^&\s]+"
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)(https?://)[^/@\s]+@"), r"\1<redacted>@"),
)


def redact_update_text(value: str) -> str:
    """Remove common credential shapes from a log message."""
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:2000]


class UpdateLog:
    """Small JSON-lines logger used by the app and the updater helper."""

    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir).resolve() / "updates" / "update.log"

    def log(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": redact_update_text(event),
        }
        for key, value in fields.items():
            payload[key] = redact_update_text(value) if isinstance(value, str) else value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            # Logging must never make an update fail or reveal a path in a UI error.
            return

    def recent(self, limit: int = 80) -> list[str]:
        """Return the newest log lines, oldest first."""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-max(1, limit) :]
