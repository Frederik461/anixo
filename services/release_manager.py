"""Automated ANIXO release management from inside the CLI.

The release workflow in ``.github/workflows/release.yml`` remains the only
component that publishes GitHub Releases.  This service performs the safe
local work around it: version detection, version-file updates, preflight
checks, tests, commit/tag/push, workflow monitoring, and published-asset
verification.

No test in this module contacts the network or mutates a repository: all
subprocess, confirmation, sleeping, and HTTP operations are injectable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from config import Config
from packaging.version import Version
from services import updater as updater_module
from services.update_log import redact_update_text

PASSED = "PASSED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
NOT_TESTED = "NOT TESTED"

RELEASE_ASSET_TEMPLATES: tuple[str, ...] = (
    "ANIXO-Setup-v{version}.exe",
    "ANIXO-Uninstaller-v{version}.exe",
    "ANIXO-Portable-v{version}.zip",
    "ANIXO-Updater-windows-x64.exe",
    "ANIXO-v{version}-macos-arm64.dmg",
    "ANIXO-Uninstaller-v{version}-macos-arm64",
    "ANIXO-Updater-macos-arm64.zip",
    "ANIXO-v{version}-macos-x86_64.dmg",
    "ANIXO-Uninstaller-v{version}-macos-x86_64",
    "ANIXO-Updater-macos-x86_64.zip",
    "SHA256SUMS.txt",
    "release-manifest.json",
)

# These seven assets are the only ones pinned in release-manifest.json.  The
# uninstallers are real release assets but are intentionally not updater
# packages.
MANIFEST_ASSET_TEMPLATES: tuple[str, ...] = (
    "ANIXO-Setup-v{version}.exe",
    "ANIXO-Portable-v{version}.zip",
    "ANIXO-Updater-windows-x64.exe",
    "ANIXO-v{version}-macos-arm64.dmg",
    "ANIXO-Updater-macos-arm64.zip",
    "ANIXO-v{version}-macos-x86_64.dmg",
    "ANIXO-Updater-macos-x86_64.zip",
)

CHECKSUM_ASSET_TEMPLATES: tuple[str, ...] = RELEASE_ASSET_TEMPLATES[:-2]

_VERSION_RE = re.compile(
    r"^(?P<base>\d+\.\d+\.\d+)(?:-(?P<label>[A-Za-z][A-Za-z0-9.-]*)\.(?P<number>\d+))?$"
)
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")

_DECLARATION_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("version.py", re.compile(r"(?m)^\s*__version__\s*=\s*[\"']([^\"']+)[\"']"), True),
    ("pyproject.toml", re.compile(r"(?m)^version\s*=\s*\"([^\"]+)\""), True),
    (
        "installer/AnixoSetup.iss",
        re.compile(r"(?m)^\s*#define\s+MyAppVersion\s+\"([^\"]+)\""),
        True,
    ),
    (
        ".github/workflows/release.yml",
        re.compile(r"(?m)^\s*APP_VERSION:\s*([^\s#]+)"),
        False,
    ),
    (
        ".github/workflows/macos.yml",
        re.compile(r"(?m)^\s*APP_VERSION:\s*([^\s#]+)"),
        False,
    ),
    (
        "packaging/build_windows.ps1",
        re.compile(r"(?m)^\s*\[string\]\s*\$Version\s*=\s*'([^']+)'"),
        False,
    ),
    (
        "packaging/make_portable.ps1",
        re.compile(r"(?m)^\s*\[string\]\s*\$Version\s*=\s*'([^']+)'"),
        False,
    ),
    (
        "packaging/validate_build.ps1",
        re.compile(r"(?m)^\s*\[string\]\s*\$Version\s*=\s*'([^']+)'"),
        False,
    ),
    (
        "packaging/build_macos.sh",
        re.compile(r"(?m)^VERSION=\"\$\{1:-([^\}]+)\}\""),
        False,
    ),
)

_EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "build-onefile",
    "build-uninstaller",
    "dist",
}

_PACKAGING_FILES: tuple[str, ...] = (
    "anixo.spec",
    "anixo_macos.spec",
    "uninstaller.spec",
    "uninstaller_macos.spec",
    "installer/AnixoSetup.iss",
    "packaging/build_windows.ps1",
    "packaging/build_macos.sh",
    "packaging/make_portable.ps1",
    "packaging/validate_build.ps1",
    "packaging/make_release_manifest.py",
)


class ReleaseError(RuntimeError):
    """A release operation cannot safely continue."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    @property
    def mandatory_failure(self) -> bool:
        return self.status == FAILED


@dataclass(frozen=True)
class WorkflowRun:
    id: str
    status: str
    conclusion: str
    url: str
    head_branch: str
    title: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorkflowRun":
        return cls(
            id=str(payload.get("databaseId") or payload.get("id") or ""),
            status=str(payload.get("status") or ""),
            conclusion=str(payload.get("conclusion") or ""),
            url=str(payload.get("url") or ""),
            head_branch=str(payload.get("headBranch") or ""),
            title=str(payload.get("displayTitle") or ""),
        )


@dataclass(frozen=True)
class VersionParts:
    text: str
    base: str
    label: str
    number: int

    @property
    def is_prerelease(self) -> bool:
        return bool(self.label)


Runner = Callable[..., CommandResult]
Confirmer = Callable[..., bool]
Printer = Callable[[str], None]
Fetcher = Callable[[str], bytes]
HeadFetcher = Callable[[str], tuple[int, int]]


def parse_release_version(value: str) -> VersionParts:
    """Parse the project's ``major.minor.patch[-label.N]`` version format."""
    text = str(value or "").strip()
    match = _VERSION_RE.fullmatch(text)
    if not match:
        raise ReleaseError(f"Invalid ANIXO version: {text or '<empty>'}")
    label = str(match.group("label") or "").lower()
    number_text = match.group("number")
    return VersionParts(
        text=text,
        base=str(match.group("base")),
        label=label,
        number=int(number_text) if number_text else 0,
    )


def channel_for_version(version: str) -> str:
    """Return the release channel used everywhere in ANIXO."""
    return "beta" if "-" in str(version) else "stable"


def normalized_version(version: str) -> str:
    """Return the PEP 440 form used by uv.lock (``0.1.0-beta.1`` -> ``0.1.0b1``)."""
    return str(Version(str(version)))


def target_version(current: str, channel: str) -> str:
    """Compute the release target without mutating any file."""
    parts = parse_release_version(current)
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel == "beta":
        if not parts.is_prerelease:
            raise ReleaseError(
                f"Cannot increment a beta from stable version {parts.text}. "
                "Set the next base version first, then run `Anixo release beta`."
            )
        if parts.label != "beta":
            raise ReleaseError(
                f"Cannot increment a beta from {parts.label}.{parts.number}; expected a beta version."
            )
        return f"{parts.base}-beta.{parts.number + 1}"
    if normalized_channel == "stable":
        if not parts.is_prerelease:
            raise ReleaseError(f"{parts.text} is already stable; there is no prerelease to remove.")
        return parts.base
    raise ReleaseError(f"Unknown release channel: {channel or '<empty>'}")


def expected_asset_names(version: str) -> tuple[str, ...]:
    return tuple(template.format(version=version) for template in RELEASE_ASSET_TEMPLATES)


def expected_manifest_asset_names(version: str) -> tuple[str, ...]:
    return tuple(template.format(version=version) for template in MANIFEST_ASSET_TEMPLATES)


def expected_checksum_names(version: str) -> tuple[str, ...]:
    return tuple(template.format(version=version) for template in CHECKSUM_ASSET_TEMPLATES)


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``<sha256>  <file>`` lines into a case-insensitive name map."""
    result: dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        result[Path(match.group(2).strip()).name.lower()] = match.group(1).lower()
    return result


def repo_slug(value: str) -> str:
    """Extract ``owner/repo`` from an HTTPS or SSH GitHub remote URL."""
    text = str(value or "").strip().removesuffix(".git")
    pieces = [piece for piece in re.split(r"[/:]", text) if piece]
    if len(pieces) < 2:
        return text.lower()
    return f"{pieces[-2]}/{pieces[-1]}".lower()


def _default_runner(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        tuple(command),
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


def _default_confirm(prompt: str, *, phrase: str | None = None) -> bool:
    answer = input(prompt).strip().lower()
    if phrase:
        return answer == phrase.lower()
    return answer in {"y", "yes"}


def _safe_detail(value: str, *, limit: int = 500) -> str:
    return redact_update_text(str(value or "").strip())[:limit]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _windows_numeric_version(version: str) -> str:
    parts = parse_release_version(version)
    major, minor, patch = (int(piece) for piece in parts.base.split("."))
    return f"{major}.{minor}.{patch}.0"


def version_tokens(current: str, target: str) -> tuple[tuple[str, str], ...]:
    """Return exact and PEP 440-normalized replacement pairs."""
    pairs: list[tuple[str, str]] = [(str(current), str(target))]
    old_normalized = normalized_version(current)
    new_normalized = normalized_version(target)
    if old_normalized != str(current):
        pairs.append((old_normalized, new_normalized))
    return tuple(pairs)


def discover_version_references(
    root: Path,
    version: str,
    *,
    tracked_files: Iterable[Path] | None = None,
) -> list[Path]:
    """Find every tracked text file containing the current version token."""
    root = Path(root).resolve()
    tokens = [old for old, _ in version_tokens(version, version)] + [str(version)]
    needles = sorted({token.encode("utf-8") for token in tokens if token})
    found: list[Path] = []
    for relative in tracked_files or []:
        path = Path(relative)
        if not path.is_absolute():
            path = root / path
        try:
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if any(needle in raw for needle in needles):
            found.append(path)
    return sorted(set(found))


def declared_versions(root: Path) -> dict[str, list[str]]:
    """Collect version declarations from all known declaration formats."""
    root = Path(root).resolve()
    found: dict[str, list[str]] = {}
    for relative, pattern, required in _DECLARATION_PATTERNS:
        path = root / relative
        if not path.is_file():
            if required:
                found[relative] = []
            continue
        values = [match.group(1) for match in pattern.finditer(_read_text(path))]
        if required or values:
            found[relative] = values
    return found


def validate_versions(root: Path, current: str, tracked_files: Iterable[Path] | None = None) -> list[Path]:
    """Raise when any discovered version declaration disagrees with ``current``."""
    declarations = declared_versions(root)
    for relative, values in declarations.items():
        if not values:
            raise ReleaseError(f"Missing version declaration in {relative}.")
        unexpected = [value for value in values if value != current]
        if unexpected:
            raise ReleaseError(
                f"Version mismatch in {relative}: expected {current}, found {', '.join(unexpected)}."
            )

    lock = Path(root) / "uv.lock"
    if not lock.is_file():
        raise ReleaseError("uv.lock is missing; release cannot verify the frozen dependency lock.")
    lock_version = normalized_version(current)
    if lock_version not in _read_text(lock):
        raise ReleaseError(f"uv.lock does not contain normalized project version {lock_version}.")

    iss = Path(root) / "installer" / "AnixoSetup.iss"
    expected_numeric = _windows_numeric_version(current)
    iss_text = _read_text(iss) if iss.is_file() else ""
    for field in ("VersionInfoProductVersion", "VersionInfoVersion"):
        match = re.search(rf"(?m)^\s*{field}\s*=\s*(\S+)", iss_text)
        if not match or match.group(1) != expected_numeric:
            raise ReleaseError(
                f"{field} must be {expected_numeric} for {current}; refusing to publish."
            )

    references = discover_version_references(root, current, tracked_files=tracked_files)
    if not references:
        raise ReleaseError(f"No version references found for {current}; refusing to publish.")
    return references


def apply_version_references(
    root: Path,
    current: str,
    target: str,
    references: Iterable[Path],
) -> list[Path]:
    """Replace version tokens in discovered references without changing line endings."""
    pairs = [
        (old.encode("utf-8"), new.encode("utf-8"))
        for old, new in version_tokens(current, target)
        if old != new
    ]
    changed: list[Path] = []
    for path in references:
        raw = path.read_bytes()
        updated = raw
        for old, new in pairs:
            updated = updated.replace(old, new)
        if updated != raw:
            path.write_bytes(updated)
            changed.append(path)
    return changed


def load_manifest_generator(root: Path):
    """Load packaging/make_release_manifest.py by path, avoiding import shadowing."""
    script = Path(root) / "packaging" / "make_release_manifest.py"
    spec = importlib.util.spec_from_file_location("anixo_release_manifest_generator", script)
    if spec is None or spec.loader is None:
        raise ReleaseError(f"Cannot load release manifest generator: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseManager:
    """Coordinate a safe ANIXO release from a source checkout."""

    def __init__(
        self,
        repo_root: Path,
        config: Config,
        *,
        runner: Runner | None = None,
        confirmer: Confirmer | None = None,
        printer: Printer | None = None,
        fetcher: Fetcher | None = None,
        head_fetcher: HeadFetcher | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        timeout_minutes: int | None = None,
        uv_available: bool | None = None,
    ) -> None:
        self.root = Path(repo_root).resolve()
        self.config = config
        self.remote = str(config.release_remote or "origin").strip() or "origin"
        self.branch = str(config.release_branch or "main").strip() or "main"
        self.repo = str(config.update_repo or "").strip()
        self.workflow_rel = str(config.release_workflow or "").strip() or ".github/workflows/release.yml"
        self.timeout_seconds = max(
            5,
            int(timeout_minutes if timeout_minutes is not None else config.release_timeout_minutes),
        ) * 60
        self._runner = runner or _default_runner
        self._confirm = confirmer or _default_confirm
        self._print = printer or print
        self._fetcher = fetcher
        self._head_fetcher = head_fetcher
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._uv_available = shutil.which("uv") if uv_available is None else uv_available

    # ------------------------------------------------------------------
    # Generic helpers

    def _run(
        self,
        command: list[str],
        *,
        timeout: float | None = 60,
    ) -> CommandResult:
        try:
            return self._runner(command, cwd=self.root, timeout=timeout)
        except Exception as error:  # pragma: no cover - defensive for injected runners
            return CommandResult(tuple(command), 127, "", f"{type(error).__name__}: {error}")

    def _git(self, *arguments: str, timeout: float | None = 60) -> CommandResult:
        return self._run(["git", *arguments], timeout=timeout)

    def _gh(self, *arguments: str, timeout: float | None = 90) -> CommandResult:
        return self._run(["gh", *arguments], timeout=timeout)

    def _record(
        self,
        results: dict[str, CheckResult],
        name: str,
        status: str,
        detail: str = "",
    ) -> CheckResult:
        result = CheckResult(name=name, status=status, detail=_safe_detail(detail))
        results[name] = result
        self._print(f"[{result.status}] {result.name}" + (f" — {result.detail}" if result.detail else ""))
        return result

    def _print_results(self, title: str, results: Mapping[str, CheckResult]) -> None:
        self._print(f"\n{title}")
        for result in results.values():
            self._print(
                f"  [{result.status}] {result.name}"
                + (f" — {result.detail}" if result.detail else "")
            )

    def _failures(self, results: Mapping[str, CheckResult]) -> list[str]:
        return [result.name for result in results.values() if result.mandatory_failure]

    def _tracked_files(self) -> list[Path]:
        result = self._git("ls-files", "-z")
        if result.ok and result.stdout:
            return [Path(item) for item in result.stdout.split("\0") if item]
        files: list[Path] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            files.append(path.relative_to(self.root))
        return sorted(files)

    def _base_url(self, version: str) -> str:
        return f"https://github.com/{self.repo}/releases/download/v{version}"

    def _assert_trusted_url(self, url: str) -> None:
        if not updater_module.is_trusted_asset_url(url):
            raise ReleaseError(f"Refusing non-HTTPS or untrusted asset URL: {redact_update_text(url)}")

    def _fetch_bytes(self, url: str) -> bytes:
        self._assert_trusted_url(url)
        if self._fetcher is not None:
            return bytes(self._fetcher(url))
        import httpx

        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "ANIXO-Release-Manager"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    def _head(self, url: str) -> tuple[int, int]:
        self._assert_trusted_url(url)
        if self._head_fetcher is not None:
            status, size = self._head_fetcher(url)
            return int(status), int(size)
        import httpx

        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "ANIXO-Release-Manager"},
        ) as client:
            response = client.head(url)
            size = int(response.headers.get("content-length") or 0)
            return response.status_code, size

    # ------------------------------------------------------------------
    # Version and repository checks

    def current_version(self) -> str:
        path = self.root / "version.py"
        declarations = declared_versions(self.root)
        values = declarations.get("version.py") or []
        if not values:
            raise ReleaseError(f"Cannot read __version__ from {path}.")
        if not path.is_file():
            raise ReleaseError(f"Missing version source: {path}")
        return values[0]

    def _check_repository(self, results: dict[str, CheckResult]) -> None:
        inside = self._git("rev-parse", "--is-inside-work-tree")
        if not inside.ok or inside.stdout.strip() != "true":
            self._record(results, "Git repository", FAILED, "Not inside a Git work tree.")
            return
        remote = self._git("remote", "get-url", self.remote)
        if not remote.ok:
            self._record(
                results,
                "Git repository",
                FAILED,
                f"No '{self.remote}' remote configured.",
            )
            return
        actual = repo_slug(remote.stdout)
        expected = repo_slug(self.repo)
        if not expected or actual != expected:
            self._record(
                results,
                "Git repository",
                FAILED,
                f"Remote is {actual or '<unknown>'}; expected {expected or '<unknown>'}.",
            )
            return
        self._record(results, "Git repository", PASSED, f"{self.remote} → {actual}")

    def _check_branch(self, results: dict[str, CheckResult]) -> None:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip() if result.ok else ""
        if branch != self.branch:
            self._record(
                results,
                "Release branch",
                FAILED,
                f"On {branch or '<unknown>'}; configured release branch is {self.branch}.",
            )
            return
        self._record(results, "Release branch", PASSED, branch)

    def _check_clean_tree(self, results: dict[str, CheckResult]) -> None:
        result = self._git("status", "--porcelain", "--untracked-files=all")
        if not result.ok:
            self._record(results, "Working tree", FAILED, _safe_detail(result.stderr))
            return
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines:
            sample = ", ".join(lines[:8])
            more = "…" if len(lines) > 8 else ""
            self._record(
                results,
                "Working tree",
                FAILED,
                f"{len(lines)} uncommitted item(s): {sample}{more}. Commit or stash them manually.",
            )
            return
        self._record(results, "Working tree", PASSED, "No uncommitted changes.")

    def _check_pushed_commit(self, results: dict[str, CheckResult]) -> None:
        local = self._git("rev-parse", "HEAD")
        if not local.ok:
            self._record(results, "Pushed commit", FAILED, _safe_detail(local.stderr))
            return
        head = local.stdout.strip()
        remote = self._git("ls-remote", "--heads", self.remote, self.branch)
        if not remote.ok:
            self._record(
                results,
                "Pushed commit",
                FAILED,
                "Could not query the remote branch; check network and credentials.",
            )
            return
        remote_head = remote.stdout.split()[0] if remote.stdout.strip() else ""
        if remote_head != head:
            self._record(
                results,
                "Pushed commit",
                FAILED,
                f"HEAD {head[:10]} is not {self.remote}/{self.branch} "
                f"{remote_head[:10] or '<missing>'}. Push it first.",
            )
            return
        self._record(results, "Pushed commit", PASSED, head[:12])

    def _check_tag_available(self, results: dict[str, CheckResult], tag: str) -> None:
        local = self._git("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
        if local.ok:
            self._record(results, "Release tag", FAILED, f"{tag} already exists locally.")
            return
        remote = self._git("ls-remote", "--tags", self.remote, f"refs/tags/{tag}")
        if not remote.ok:
            self._record(results, "Release tag", FAILED, _safe_detail(remote.stderr))
            return
        if remote.stdout.strip():
            self._record(results, "Release tag", FAILED, f"{tag} already exists on {self.remote}.")
            return
        self._record(results, "Release tag", PASSED, f"{tag} is free locally and remotely.")

    def _check_versions(self, results: dict[str, CheckResult], current: str) -> list[Path]:
        try:
            references = validate_versions(self.root, current, tracked_files=self._tracked_files())
        except ReleaseError as error:
            self._record(results, "Version consistency", FAILED, str(error))
            return []
        files = {str(path.relative_to(self.root)) for path in references}
        self._record(
            results,
            "Version consistency",
            PASSED,
            f"{len(references)} tracked references across {len(files)} files match {current}.",
        )
        return references

    def _check_gh(self, results: dict[str, CheckResult]) -> None:
        auth = self._gh("auth", "status", "--hostname", "github.com")
        if not auth.ok:
            self._record(
                results,
                "GitHub CLI authentication",
                FAILED,
                "Run `gh auth login` with an account that can publish releases.",
            )
            return
        view = self._gh("repo", "view", self.repo, "--json", "nameWithOwner")
        if not view.ok:
            self._record(
                results,
                "GitHub CLI authentication",
                FAILED,
                f"Cannot access {self.repo} with the active GitHub CLI account.",
            )
            return
        name = ""
        try:
            name = str(json.loads(view.stdout).get("nameWithOwner") or "")
        except json.JSONDecodeError:
            name = ""
        self._record(
            results,
            "GitHub CLI authentication",
            PASSED,
            name or self.repo,
        )

    def _check_workflow(self, results: dict[str, CheckResult]) -> None:
        path = self.root / self.workflow_rel
        if not path.is_file():
            self._record(results, "Release workflow", FAILED, f"Missing {self.workflow_rel}.")
            return
        text = _read_text(path)
        required = (
            "push:",
            "tags:",
            "v*",
            "softprops/action-gh-release@v2",
            "SHA256SUMS.txt",
            "release-manifest.json",
        )
        missing = [item for item in required if item not in text]
        if missing:
            self._record(
                results,
                "Release workflow",
                FAILED,
                f"Missing expected workflow content: {', '.join(missing)}.",
            )
            return
        self._record(results, "Release workflow", PASSED, self.workflow_rel)

    def _check_tools(self, results: dict[str, CheckResult]) -> None:
        git = self._run(["git", "--version"])
        gh = self._run(["gh", "--version"])
        if not git.ok:
            self._record(results, "Required tools", FAILED, "git is not installed or not on PATH.")
            return
        if not gh.ok:
            self._record(results, "Required tools", FAILED, "gh is not installed or not on PATH.")
            return
        self._record(
            results,
            "Required tools",
            PASSED,
            f"git, gh, Python {sys.version.split()[0]}.",
        )

    def _check_tests(self, results: dict[str, CheckResult]) -> None:
        result = self._run([sys.executable, "-m", "pytest", "-q"], timeout=900)
        if not result.ok:
            tail = result.stdout.strip().splitlines()[-12:] or result.stderr.strip().splitlines()
            self._record(
                results,
                "Test suite",
                FAILED,
                "pytest failed. " + " | ".join(tail[-4:]),
            )
            return
        match = re.search(r"(\d+)\s+passed", result.stdout)
        detail = f"{match.group(1)} passed" if match else "pytest exited 0"
        self._record(results, "Test suite", PASSED, detail)

    def _check_compilation(self, results: dict[str, CheckResult]) -> None:
        python_files = sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*.py")
            if not any(part in _EXCLUDED_DIRS for part in path.parts)
        )
        if not python_files:
            self._record(results, "Python compilation", FAILED, "No Python files found.")
            return
        result = self._run([sys.executable, "-m", "compileall", "-q", *python_files], timeout=300)
        if not result.ok:
            self._record(results, "Python compilation", FAILED, _safe_detail(result.stderr))
            return
        self._record(results, "Python compilation", PASSED, f"{len(python_files)} files compiled.")

    def _check_manifest_generator(self, results: dict[str, CheckResult], version: str) -> None:
        with tempfile.TemporaryDirectory(prefix="anixo-manifest-") as temporary:
            release_dir = Path(temporary) / "release"
            release_dir.mkdir()
            notes = Path(temporary) / "NOTES.txt"
            notes.write_text(f"ANIXO {version} release-manager dry run.\n", encoding="utf-8")
            for name in expected_manifest_asset_names(version):
                (release_dir / name).write_bytes(f"manifest-validation:{name}".encode("utf-8"))
            base_url = self._base_url(version)
            output = release_dir / "release-manifest.json"
            command = [
                sys.executable,
                str(self.root / "packaging" / "make_release_manifest.py"),
                "--release-dir",
                str(release_dir),
                "--version",
                version,
                "--base-url",
                base_url,
                "--notes-file",
                str(notes),
                "--output",
                str(output),
            ]
            result = self._run(command, timeout=120)
            if not result.ok or not output.is_file():
                self._record(
                    results,
                    "Release manifest validation",
                    FAILED,
                    _safe_detail(result.stderr or result.stdout),
                )
                return
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
                manifest = updater_module.parse_manifest(payload, source_url=base_url)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._record(results, "Release manifest validation", FAILED, str(error))
                return
            expected_names = set(expected_manifest_asset_names(version))
            names = {asset.name for asset in manifest.assets}
            if (
                manifest.version != version
                or updater_module.normalize_channel(manifest.channel) != channel_for_version(version)
                or names != expected_names
                or not all(asset.sha256 and asset.size > 0 for asset in manifest.assets)
            ):
                self._record(
                    results,
                    "Release manifest validation",
                    FAILED,
                    "Generated manifest failed schema, version, channel, or asset checks.",
                )
                return
            self._record(
                results,
                "Release manifest validation",
                PASSED,
                f"schema {manifest.schema_version}, {len(names)} pinned assets.",
            )

    def _check_lock(self, results: dict[str, CheckResult], version: str) -> None:
        lock = self.root / "uv.lock"
        if not lock.is_file():
            self._record(results, "Dependency lock", FAILED, "uv.lock is missing.")
            return
        token = normalized_version(version)
        if token not in _read_text(lock):
            self._record(results, "Dependency lock", FAILED, f"uv.lock lacks version {token}.")
            return
        if not self._uv_available:
            self._record(
                results,
                "Dependency lock",
                NOT_TESTED,
                "uv is unavailable; normalized version was checked directly.",
            )
            return
        result = self._run(["uv", "lock", "--check"], timeout=180)
        if not result.ok:
            self._record(results, "Dependency lock", FAILED, _safe_detail(result.stderr))
            return
        self._record(results, "Dependency lock", PASSED, "uv lock --check passed.")

    def _check_packaging(self, results: dict[str, CheckResult], version: str) -> None:
        missing = [item for item in _PACKAGING_FILES if not (self.root / item).is_file()]
        if missing:
            self._record(
                results,
                "Packaging scripts",
                FAILED,
                "Missing: " + ", ".join(missing),
            )
            return
        release_dir = self.root / "dist" / "release"
        checksums = release_dir / "SHA256SUMS.txt"
        if not checksums.is_file():
            self._record(
                results,
                "Packaging artifacts",
                NOT_TESTED,
                "No local dist/release checksums; CI builds and verifies the artifacts.",
            )
            return
        parsed = parse_sha256sums(_read_text(checksums))
        needed = {name.lower() for name in expected_checksum_names(version)}
        absent = sorted(needed - set(parsed))
        if absent:
            self._record(
                results,
                "Packaging artifacts",
                FAILED,
                "SHA256SUMS.txt is missing: " + ", ".join(absent[:6]),
            )
            return
        self._record(results, "Packaging artifacts", PASSED, f"{len(needed)} local checksums.")

    def preflight(self, current: str, target: str) -> tuple[dict[str, CheckResult], list[Path]]:
        """Run every mandatory check before any file is modified."""
        self._print(f"\nPreflight for ANIXO {current} → {target}")
        results: dict[str, CheckResult] = {}
        self._check_repository(results)
        self._check_branch(results)
        self._check_clean_tree(results)
        self._check_pushed_commit(results)
        references = self._check_versions(results, current)
        self._check_tag_available(results, f"v{target}")
        self._check_gh(results)
        self._check_workflow(results)
        self._check_tools(results)
        self._check_tests(results)
        self._check_compilation(results)
        self._check_manifest_generator(results, target)
        self._check_lock(results, current)
        self._check_packaging(results, target)
        return results, references

    # ------------------------------------------------------------------
    # Publishing

    def _summary(self, current: str, target: str, channel: str) -> None:
        self._print("\nRelease summary")
        self._print(f"  Current version: {current}")
        self._print(f"  Target version:  {target}")
        self._print(f"  Channel:         {channel}")
        self._print(f"  Repository:      {self.repo}")
        self._print(f"  Git branch:      {self.branch} ({self.remote})")
        self._print(f"  Tag:             v{target}")
        self._print(f"  Workflow:        {self.workflow_rel}")
        self._print("  Publisher:       GitHub Actions release workflow only")

    def _required_ok(self, results: Mapping[str, CheckResult]) -> bool:
        return not self._failures(results)

    def run_release(self, channel: str) -> int:
        """Create a beta or stable release through the existing workflow."""
        channel = str(channel or "").strip().lower()
        try:
            current = self.current_version()
            target = target_version(current, channel)
        except ReleaseError as error:
            self._print(f"[FAILED] {error}")
            return 2

        self._summary(current, target, channel)
        results, references = self.preflight(current, target)
        self._print_results("Preflight results", results)
        if not self._required_ok(results):
            self._print("\nNot starting the release: mandatory preflight checks failed.")
            return 2

        current_parts = parse_release_version(current)
        if channel == "stable" and current_parts.label == "beta":
            prompt = (
                f"\nThis converts beta {current} to stable {target}. Type 'stable' to continue: "
            )
            if not self._confirm(prompt, phrase="stable"):
                self._print("Release cancelled; no files were changed.")
                return 130
        if not self._confirm(f"\nPublish {target} through GitHub Actions? [y/N]: ", phrase="yes"):
            self._print("Release cancelled; no files were changed.")
            return 130

        self._print("\nUpdating version references")
        try:
            changed = apply_version_references(self.root, current, target, references)
            validate_versions(self.root, target, tracked_files=self._tracked_files())
        except (OSError, ReleaseError) as error:
            self._print(f"[FAILED] Version update stopped: {error}")
            self._print("Version files may already be changed. Review them manually; nothing was discarded.")
            return 1
        if not changed:
            self._print("[FAILED] No version references were changed; refusing to commit an empty release.")
            return 1
        self._print(f"[PASSED] Updated {len(changed)} version reference file(s).")

        tag = f"v{target}"
        steps: tuple[tuple[list[str], str], ...] = (
            (["git", "add", "--", *[str(path.relative_to(self.root)) for path in changed]], "Stage version changes"),
            (["git", "commit", "-m", f"Release {tag}"], f"Commit Release {tag}"),
            (["git", "push", self.remote, self.branch], f"Push {self.branch}"),
            (["git", "tag", "-a", tag, "-m", f"ANIXO {target}"], f"Create tag {tag}"),
            (["git", "push", self.remote, tag], f"Push tag {tag}"),
        )
        for command, label in steps:
            result = self._run(command, timeout=300)
            if not result.ok:
                detail = _safe_detail(result.stderr or result.stdout)
                self._print(f"[FAILED] {label}: {detail}")
                self._print(f"Resume after fixing it with: Anixo release check {tag}")
                return 1
            self._print(f"[PASSED] {label}.")

        workflow = self._monitor_workflow(tag, required=True)
        if workflow is None or workflow.conclusion != "success":
            self._print(f"Do not re-tag or re-push. Resume with: Anixo release check {tag}")
            return 1

        verification = self.verify_release(target, tag)
        self._print_results("Published release verification", verification)
        ready = self._required_ok(verification)
        self._final_summary(target, verification, ready)
        return 0 if ready else 1

    def _step(self, label: str, result: CommandResult) -> bool:
        if not result.ok:
            self._print(f"[FAILED] {label}: {_safe_detail(result.stderr or result.stdout)}")
            return False
        self._print(f"[PASSED] {label}.")
        return True

    # ------------------------------------------------------------------
    # Workflow monitoring

    def _find_workflow_run(self, tag: str) -> WorkflowRun | None:
        workflow_name = Path(self.workflow_rel).name
        result = self._gh(
            "run",
            "list",
            "--repo",
            self.repo,
            "--workflow",
            workflow_name,
            "--event",
            "push",
            "--limit",
            "100",
            "--json",
            "databaseId,status,conclusion,headBranch,displayTitle,url,createdAt",
        )
        if not result.ok:
            self._print(f"[UNKNOWN] Could not list workflow runs: {_safe_detail(result.stderr)}")
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self._print("[UNKNOWN] Workflow run list was not valid JSON.")
            return None
        candidates = [WorkflowRun.from_payload(item) for item in payload if isinstance(item, dict)]
        exact = [item for item in candidates if item.head_branch == tag]
        if exact:
            return sorted(exact, key=lambda item: item.id, reverse=True)[0]
        titled = [item for item in candidates if tag in item.title]
        return sorted(titled, key=lambda item: item.id, reverse=True)[0] if titled else None

    def _refresh_workflow_run(self, run: WorkflowRun) -> WorkflowRun:
        result = self._gh(
            "run",
            "view",
            run.id,
            "--repo",
            self.repo,
            "--json",
            "databaseId,status,conclusion,headBranch,displayTitle,url",
        )
        if not result.ok:
            return run
        try:
            return WorkflowRun.from_payload(json.loads(result.stdout))
        except json.JSONDecodeError:
            return run

    def _report_failed_jobs(self, run: WorkflowRun) -> None:
        result = self._gh(
            "run",
            "view",
            run.id,
            "--repo",
            self.repo,
            "--json",
            "jobs",
        )
        if not result.ok:
            self._print("[UNKNOWN] Failed job details could not be loaded.")
            return
        try:
            jobs = json.loads(result.stdout).get("jobs") or []
        except json.JSONDecodeError:
            self._print("[UNKNOWN] Failed job details were not valid JSON.")
            return
        failed = [
            job
            for job in jobs
            if isinstance(job, dict) and str(job.get("conclusion") or "").lower() in {"failure", "cancelled", "skipped"}
        ]
        if not failed:
            self._print("[FAILED] Workflow failed; no individual failed job was reported.")
            return
        for job in failed:
            self._print(
                f"[FAILED] Job {job.get('name') or '<unnamed>'}: "
                f"{job.get('conclusion')} — {job.get('url') or run.url}"
            )

    def _monitor_workflow(self, tag: str, *, required: bool) -> WorkflowRun | None:
        self._print(f"\nWaiting for the release workflow triggered by {tag}")
        run = self._find_workflow_run(tag)
        if run is None:
            status = FAILED if required else UNKNOWN
            self._record({}, "Workflow run", status, f"No run found for {tag}.")
            self._print(f"Resume with: Anixo release check {tag}")
            return None
        self._print(f"[PASSED] Found workflow run {run.id}: {run.url or run.title}")

        deadline = self._clock() + self.timeout_seconds
        last_status = ""
        while True:
            if run.status == "completed":
                if run.conclusion == "success":
                    self._print("[PASSED] Release workflow completed successfully.")
                    return run
                self._print(f"[FAILED] Release workflow completed with {run.conclusion or 'unknown conclusion'}.")
                self._report_failed_jobs(run)
                return run
            if self._clock() >= deadline:
                self._record(
                    {},
                    "Workflow timeout",
                    UNKNOWN,
                    f"Still {run.status or 'running'} after {self.timeout_seconds // 60} minutes.",
                )
                self._print(f"Resume with: Anixo release check {tag}")
                return None
            if run.status != last_status:
                self._print(f"  Workflow status: {run.status or 'queued'}")
                last_status = run.status
            self._sleep(min(15.0, max(0.0, deadline - self._clock())))
            run = self._refresh_workflow_run(run)

    # ------------------------------------------------------------------
    # Release verification

    def verify_release(self, version: str, tag: str | None = None) -> dict[str, CheckResult]:
        """Verify the published release against the workflow's real formats."""
        tag = tag or f"v{version}"
        results: dict[str, CheckResult] = {}
        self._print(f"\nVerifying published release {tag}")
        view = self._gh(
            "release",
            "view",
            tag,
            "--repo",
            self.repo,
            "--json",
            "tagName,name,isDraft,isPrerelease,url,assets",
        )
        if not view.ok:
            self._record(results, "GitHub Release", FAILED, _safe_detail(view.stderr))
            return results
        try:
            release = json.loads(view.stdout)
        except json.JSONDecodeError:
            self._record(results, "GitHub Release", FAILED, "Release response was not valid JSON.")
            return results

        release_tag = str(release.get("tagName") or "")
        if release_tag != tag:
            self._record(results, "GitHub Release", FAILED, f"Tag is {release_tag}; expected {tag}.")
            return results
        expected_channel = channel_for_version(version)
        is_prerelease = bool(release.get("isPrerelease"))
        is_draft = bool(release.get("isDraft"))
        if is_draft or is_prerelease != (expected_channel == "beta"):
            self._record(
                results,
                "Release metadata",
                FAILED,
                f"draft={is_draft}, prerelease={is_prerelease}; expected channel {expected_channel}.",
            )
        else:
            self._record(results, "Release metadata", PASSED, f"channel {expected_channel}, not a draft.")

        raw_assets = release.get("assets") or []
        assets = {
            str(item.get("name")): item
            for item in raw_assets
            if isinstance(item, dict) and item.get("name")
        }
        needed = set(expected_asset_names(version))
        missing = sorted(needed - set(assets))
        if missing:
            self._record(
                results,
                "Expected assets",
                FAILED,
                "Missing: " + ", ".join(missing),
            )
        else:
            self._record(results, "Expected assets", PASSED, f"{len(needed)} expected assets present.")

        bad_hosts = [
            name
            for name, item in assets.items()
            if not updater_module.is_trusted_asset_url(str(item.get("url") or ""))
        ]
        if bad_hosts:
            self._record(
                results,
                "Asset URLs",
                FAILED,
                "Non-HTTPS or untrusted URLs: " + ", ".join(sorted(bad_hosts)[:6]),
            )
        else:
            self._record(results, "Asset URLs", PASSED, f"{len(assets)} HTTPS asset URLs.")

        manifest_asset = assets.get("release-manifest.json")
        manifest: Any = None
        if manifest_asset is None:
            self._record(results, "Release manifest", FAILED, "release-manifest.json is absent.")
        else:
            try:
                manifest_url = str(manifest_asset.get("url") or "")
                payload = json.loads(self._fetch_bytes(manifest_url).decode("utf-8"))
                manifest = updater_module.parse_manifest(payload, source_url=manifest_url)
            except (OSError, ValueError, json.JSONDecodeError, ReleaseError) as error:
                self._record(results, "Release manifest", FAILED, str(error))
            else:
                names = {asset.name for asset in manifest.assets}
                expected_names = set(expected_manifest_asset_names(version))
                channel = updater_module.normalize_channel(manifest.channel)
                if (
                    manifest.version != version
                    or channel != expected_channel
                    or names != expected_names
                    or not all(asset.sha256 and asset.size > 0 for asset in manifest.assets)
                ):
                    self._record(
                        results,
                        "Release manifest",
                        FAILED,
                        f"schema={manifest.schema_version}, version={manifest.version}, "
                        f"channel={channel}, assets={len(names)}.",
                    )
                else:
                    self._record(
                        results,
                        "Release manifest",
                        PASSED,
                        f"schema {manifest.schema_version}, {expected_channel}, {len(names)} pinned assets.",
                    )

        checksum_asset = assets.get("SHA256SUMS.txt")
        checksums: dict[str, str] = {}
        if checksum_asset is None:
            self._record(results, "SHA-256 checksums", FAILED, "SHA256SUMS.txt is absent.")
        else:
            try:
                checksums = parse_sha256sums(
                    self._fetch_bytes(str(checksum_asset.get("url") or "")).decode("utf-8")
                )
            except (OSError, ValueError, ReleaseError) as error:
                self._record(results, "SHA-256 checksums", FAILED, str(error))
            else:
                needed_checksums = {name.lower() for name in expected_checksum_names(version)}
                if set(checksums) != needed_checksums:
                    self._record(
                        results,
                        "SHA-256 checksums",
                        FAILED,
                        f"Expected {len(needed_checksums)} entries; found {len(checksums)}.",
                    )
                else:
                    self._record(
                        results,
                        "SHA-256 checksums",
                        PASSED,
                        f"{len(checksums)} checksum entries.",
                    )

        digest_mismatches: list[str] = []
        for name, item in assets.items():
            github_digest = str(item.get("digest") or "").lower().removeprefix("sha256:")
            if not github_digest:
                continue
            checksum_digest = checksums.get(name.lower() if name != "SHA256SUMS.txt" else "sha256sums.txt")
            if checksum_digest and github_digest != checksum_digest:
                digest_mismatches.append(name)
            if manifest is not None:
                manifest_asset_entry = next((entry for entry in manifest.assets if entry.name == name), None)
                if manifest_asset_entry and github_digest != manifest_asset_entry.sha256:
                    digest_mismatches.append(name)
        if digest_mismatches:
            self._record(
                results,
                "Checksum agreement",
                FAILED,
                "Mismatches: " + ", ".join(sorted(set(digest_mismatches))[:6]),
            )
        else:
            self._record(results, "Checksum agreement", PASSED, "GitHub, manifest, and SHA256SUMS agree.")

        url_failures: list[str] = []
        for name in sorted(needed):
            item = assets.get(name)
            if not item:
                continue
            try:
                status, _ = self._head(str(item.get("url") or ""))
            except (OSError, ValueError, ReleaseError) as error:
                url_failures.append(f"{name} ({type(error).__name__})")
                continue
            if status < 200 or status >= 400:
                url_failures.append(f"{name} (HTTP {status})")
        if url_failures:
            self._record(
                results,
                "Asset URL availability",
                FAILED,
                ", ".join(url_failures[:6]),
            )
        else:
            self._record(results, "Asset URL availability", PASSED, f"{len(needed)} URLs reachable over HTTPS.")

        return results

    # ------------------------------------------------------------------
    # Check and history commands

    def check_release(self, version_or_tag: str = "") -> int:
        """Resume monitoring and verification for an already-created release."""
        raw = str(version_or_tag or "").strip()
        version = raw[1:] if raw.startswith("v") else raw
        if not version:
            version = self.current_version()
        tag = raw if raw.startswith("v") else f"v{version}"
        channel = channel_for_version(version)
        self._summary(version, version, channel)
        self._print("Mode: verify an existing release; no files will be changed.")

        results: dict[str, CheckResult] = {}
        self._check_repository(results)
        self._check_gh(results)
        self._check_workflow(results)
        local_tag = self._git("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
        remote_tag = self._git("ls-remote", "--tags", self.remote, f"refs/tags/{tag}")
        if remote_tag.ok and remote_tag.stdout.strip():
            self._record(results, "Release tag", PASSED, f"{tag} exists on {self.remote}.")
        elif local_tag.ok:
            self._record(results, "Release tag", UNKNOWN, f"{tag} exists locally but not on {self.remote}.")
        else:
            self._record(results, "Release tag", FAILED, f"{tag} was not found locally or remotely.")

        workflow = self._monitor_workflow(tag, required=False)
        if workflow is not None and workflow.conclusion != "success":
            self._record(
                results,
                "Workflow conclusion",
                FAILED,
                workflow.conclusion or "unknown",
            )
        elif workflow is not None:
            self._record(results, "Workflow conclusion", PASSED, "success")

        verification = self.verify_release(version, tag)
        self._print_results("Existing release check", {**results, **verification})
        ready = self._required_ok({**results, **verification})
        self._final_summary(version, {**results, **verification}, ready)
        return 0 if ready else 1

    def history(self, limit: int = 10) -> int:
        """Show local release tags and GitHub Releases without changing anything."""
        limit = max(1, min(100, int(limit)))
        self._print(f"\nANIXO release history (last {limit})")
        tags = self._git("tag", "--sort=-v:refname")
        if tags.ok and tags.stdout.strip():
            self._print("Local tags:")
            for tag in tags.stdout.splitlines()[:limit]:
                if tag.strip():
                    self._print(f"  {tag.strip()} — {channel_for_version(tag.strip().lstrip('v'))}")
        else:
            self._print("Local tags: [NOT TESTED] no tags or git is unavailable.")

        auth = self._gh("auth", "status", "--hostname", "github.com")
        if not auth.ok:
            self._print("GitHub Releases: [UNKNOWN] gh is not authenticated.")
            return 0
        result = self._gh(
            "release",
            "list",
            "--repo",
            self.repo,
            "--limit",
            str(limit),
            "--json",
            "tagName,name,isPrerelease,publishedAt,url",
        )
        if not result.ok:
            self._print(f"GitHub Releases: [UNKNOWN] {_safe_detail(result.stderr)}")
            return 0
        try:
            releases = json.loads(result.stdout)
        except json.JSONDecodeError:
            self._print("GitHub Releases: [UNKNOWN] response was not valid JSON.")
            return 0
        self._print("GitHub Releases:")
        for release in releases if isinstance(releases, list) else []:
            if not isinstance(release, dict):
                continue
            tag = str(release.get("tagName") or "")
            channel = "beta" if release.get("isPrerelease") else "stable"
            self._print(
                f"  {tag or '<untagged>'} — {channel} — "
                f"{release.get('publishedAt') or '<unknown date>'} — {release.get('url') or ''}"
            )
        return 0

    def _final_summary(
        self,
        version: str,
        results: Mapping[str, CheckResult],
        ready: bool,
    ) -> None:
        self._print("\nFinal summary")
        self._print(f"  Version: {version}")
        self._print(f"  Channel: {channel_for_version(version)}")
        self._print(f"  Release URL: https://github.com/{self.repo}/releases/tag/v{version}")
        unknown = [result.name for result in results.values() if result.status == UNKNOWN]
        not_tested = [result.name for result in results.values() if result.status == NOT_TESTED]
        failed = self._failures(results)
        self._print(f"  Failed: {', '.join(failed) if failed else 'none'}")
        self._print(f"  Unknown: {', '.join(unknown) if unknown else 'none'}")
        self._print(f"  Not tested: {', '.join(not_tested) if not_tested else 'none'}")
        if ready:
            self._print(
                f"READY FOR ANIXO UPDATER TEST: v{version} passed release verification. "
                "Real-terminal install/update behaviour is still a manual test."
            )
        else:
            self._print(
                f"NOT READY FOR ANIXO UPDATER TEST: v{version} did not pass every mandatory check."
            )


__all__ = [
    "FAILED",
    "NOT_TESTED",
    "PASSED",
    "UNKNOWN",
    "CheckResult",
    "CommandResult",
    "ReleaseError",
    "ReleaseManager",
    "VersionParts",
    "WorkflowRun",
    "apply_version_references",
    "channel_for_version",
    "declared_versions",
    "discover_version_references",
    "expected_asset_names",
    "expected_checksum_names",
    "expected_manifest_asset_names",
    "normalized_version",
    "parse_release_version",
    "parse_sha256sums",
    "repo_slug",
    "target_version",
    "validate_versions",
    "version_tokens",
]
