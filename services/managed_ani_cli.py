"""Ownership metadata for ANIXO's dedicated ani-cli installation.

The receipt lives next to the managed script.  It is the authoritative proof
that ANIXO owns a copy; no ANIXO version, release name, timestamp, PATH guess,
or legacy manifest flag is used to decide whether a script may be replaced.
An independently managed installation elsewhere is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.update_log import redact_update_text

SCHEMA_VERSION = 1
METADATA_FILE = "anixo-ani-cli.json"
SCRIPT_NAME = "ani-cli"
WRAPPER_NAME = "ani-cli.cmd"
_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True, slots=True)
class ManagedAniCliState:
    """A validated receipt for one ANIXO-managed ani-cli copy."""

    directory: Path
    script: Path
    sha256: str
    version: str
    source: str
    installed_at: str
    install_method: str
    installed_by_anixo: bool = True
    managed: bool = True
    safe_to_remove: bool = True

    @property
    def wrapper(self) -> Path:
        return self.script.with_name(WRAPPER_NAME)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "installed_by_anixo": self.installed_by_anixo,
            "managed": self.managed,
            "safe_to_remove": self.safe_to_remove,
            "install_location": str(self.script),
            "sha256": self.sha256.upper(),
            "version": redact_update_text(str(self.version))[:120],
            "source": redact_update_text(str(self.source))[:500],
            "installed_at": redact_update_text(str(self.installed_at))[:100],
            "install_method": redact_update_text(str(self.install_method))[:120],
        }


class ManagedAniCli:
    """Read, write, back up, and restore a local ani-cli ownership receipt."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory).resolve()
        self.metadata_path = self.directory / METADATA_FILE
        self.script_path = self.directory / SCRIPT_NAME
        self.wrapper_path = self.directory / WRAPPER_NAME

    def read(self) -> ManagedAniCliState | None:
        """Return a receipt only when its metadata proves ANIXO ownership."""
        try:
            raw = self.metadata_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        if not all(
            data.get(key) is True
            for key in ("installed_by_anixo", "managed", "safe_to_remove")
        ):
            return None
        location = str(data.get("install_location") or "").strip()
        digest = str(data.get("sha256") or "").strip().upper()
        if not location or not _DIGEST.fullmatch(digest):
            return None
        try:
            script = Path(os.path.expandvars(os.path.expanduser(location))).resolve()
        except (OSError, RuntimeError):
            return None
        if script.parent != self.directory:
            return None
        return ManagedAniCliState(
            directory=self.directory,
            script=script,
            sha256=digest,
            version=str(data.get("version") or ""),
            source=str(data.get("source") or ""),
            installed_at=str(data.get("installed_at") or ""),
            install_method=str(data.get("install_method") or ""),
        )

    def is_owner(self, path: Path | str | None) -> bool:
        """True only when ``path`` is the script named by a valid receipt."""
        state = self.read()
        if state is None or path is None:
            return False
        try:
            return Path(path).resolve() == state.script
        except (OSError, RuntimeError):
            return False

    def record(
        self,
        *,
        script: Path | str,
        sha256: str,
        version: str,
        source: str,
        install_method: str,
    ) -> ManagedAniCliState:
        """Write the ownership receipt after a verified install."""
        target = Path(script).resolve()
        digest = str(sha256).strip().upper()
        if not _DIGEST.fullmatch(digest):
            raise ValueError("A full SHA-256 digest is required to record ownership.")
        if target.parent != self.directory:
            raise ValueError("An ani-cli receipt may only name its managed directory.")
        if not target.is_file() or digest_bytes(target.read_bytes()) != digest:
            raise ValueError("An ani-cli receipt requires a verified script on disk.")
        state = ManagedAniCliState(
            directory=self.directory,
            script=target,
            sha256=digest,
            version=str(version or "recorded revision"),
            source=str(source or ""),
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            install_method=str(install_method or "verified-install"),
        )
        self._write_mapping(state.to_mapping())
        return state

    def backup(self, backup_dir: Path | str) -> bool:
        """Copy the current receipt into a rollback directory."""
        try:
            payload = self.metadata_path.read_bytes()
        except OSError:
            return False
        try:
            target = Path(backup_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / METADATA_FILE).write_bytes(payload)
        except OSError:
            return False
        return True

    def restore(self, backup_dir: Path | str) -> bool:
        """Restore a backed-up receipt, or remove one created by a failed update."""
        candidate = Path(backup_dir) / METADATA_FILE
        try:
            if candidate.is_file():
                self.directory.mkdir(parents=True, exist_ok=True)
                self.metadata_path.write_bytes(candidate.read_bytes())
                return True
            self.metadata_path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def _write_mapping(self, payload: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".{METADATA_FILE}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.metadata_path)


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()
