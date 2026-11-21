"""GitHub Releases based update checks and installer downloads for ANIXO.

The updater never writes into the application or user-data directories.  It
only downloads a release asset into a caller-supplied directory and, on
Windows, optionally asks the OS to launch it.  No tokens are read or sent.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from config import Config
from services.setup import APP_VERSION

DEFAULT_REPO = "fred8/anixo"
USER_AGENT = "ANIXO/0.1.0"
_RELEASES_URL = "https://api.github.com/repos/{repo}/releases"


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """One downloadable file attached to a GitHub release."""

    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """Normalized subset of a GitHub release used by the UI."""

    version: str
    tag: str
    notes: str
    published_at: str
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """Result of a release lookup, including a user-facing error string."""

    current: str
    available: str
    update_available: bool
    channel: str
    release: ReleaseInfo | None
    error: str


def parse_version(value: str) -> tuple[int, int, int, int, str]:
    """Parse ``0.1.0-beta.1`` into ``(major, minor, patch, pre_number, label)``.

    A leading ``v`` and build metadata after ``+`` are ignored.  Stable
    versions have an empty label; malformed input yields a low, stable tuple
    rather than raising.
    """
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    text = text.split("+", 1)[0]
    match = re.match(
        r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([a-z]+)(?:\.(\d+))?)?",
        text,
    )
    if not match:
        return (0, 0, 0, 0, text)
    return (
        int(match.group(1) or 0),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
        int(match.group(5) or 0),
        match.group(4) or "",
    )


def _version_key(
    parsed: tuple[int, int, int, int, str],
) -> tuple[int, int, int, int, int, str]:
    """Ordering key where a stable release outranks its prereleases."""
    major, minor, patch, number, label = parsed
    if not label:
        return (major, minor, patch, 1, 0, "")
    return (major, minor, patch, 0, max(1, number), label)


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:400]


class UpdateService:
    """Queries releases and verifies installer checksums."""

    def __init__(self, config: Config, repo: str | None = None) -> None:
        self.config = config
        self._repo_argument = repo

    @property
    def repository(self) -> str:
        """Resolve ``owner/repo`` from config, environment, or the argument."""
        configured = str(self.config.extra.get("update_repo") or "").strip()
        environmental = os.environ.get("ANIXO_UPDATE_REPO", "").strip()
        return configured or environmental or (self._repo_argument or "").strip() or DEFAULT_REPO

    async def check(self, channel: str = "stable") -> UpdateCheck:
        """Return the newest release for ``channel`` with update metadata.

        ``stable`` excludes prereleases; ``beta`` includes them so prerelease
        users can advance between betas.  Transport or payload problems are
        reported in ``UpdateCheck.error`` instead of raising.
        """
        name = (
            "beta"
            if str(channel or "stable").lower() in {"beta", "prerelease", "pre"}
            else "stable"
        )
        url = _RELEASES_URL.format(repo=self.repository)
        try:
            async with httpx.AsyncClient(
                timeout=12.0,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/vnd.github+json",
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as error:
            return UpdateCheck(APP_VERSION, "", False, name, None, _safe_error(error))

        if not isinstance(payload, list):
            return UpdateCheck(
                APP_VERSION,
                "",
                False,
                name,
                None,
                "GitHub returned an unexpected releases payload.",
            )

        releases: list[ReleaseInfo] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                releases.append(self._release_from_json(item))
            except (KeyError, TypeError, ValueError):
                continue
        if name == "stable":
            releases = [release for release in releases if not release.prerelease]
        if not releases:
            return UpdateCheck(
                APP_VERSION,
                "",
                False,
                name,
                None,
                f"No {name} release is available for {self.repository}.",
            )

        release = max(releases, key=lambda item: _version_key(parse_version(item.version)))
        available = release.version
        update_available = _version_key(parse_version(available)) > _version_key(
            parse_version(APP_VERSION)
        )
        return UpdateCheck(APP_VERSION, available, update_available, name, release, "")

    @staticmethod
    def _release_from_json(item: dict[str, Any]) -> ReleaseInfo:
        tag = str(item["tag_name"] or "")
        version = tag[1:] if tag.lower().startswith("v") else tag
        assets = tuple(
            ReleaseAsset(
                name=str(asset.get("name") or ""),
                url=str(asset.get("browser_download_url") or ""),
                size=int(asset.get("size") or 0),
            )
            for asset in item.get("assets", [])
            if isinstance(asset, dict) and asset.get("name")
        )
        return ReleaseInfo(
            version=version,
            tag=tag,
            notes=str(item.get("body") or ""),
            published_at=str(item.get("published_at") or ""),
            prerelease=bool(item.get("prerelease")),
            assets=assets,
        )

    @staticmethod
    def _expected_hash(contents: str, filename: str) -> str:
        """Find the SHA256 line for ``filename`` in a SHA256SUMS file."""
        for line in contents.splitlines():
            match = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
            if not match:
                continue
            name = match.group(2).replace("\\", "/").rsplit("/", 1)[-1]
            if name.lower() == filename.lower():
                return match.group(1)
        raise ValueError(f"SHA256SUMS.txt has no entry for {filename}.")

    async def download_installer(
        self, release: ReleaseInfo, destination_dir: Path
    ) -> Path:
        """Download and verify the release's first ``.exe`` asset.

        Raises ``ValueError`` with a clear message when the asset, checksum
        file, checksum line, or hash itself is missing or invalid.  Bytes are
        written only after verification succeeds.
        """
        installer = next(
            (
                asset
                for asset in release.assets
                if asset.name.lower().endswith(".exe") and asset.url
            ),
            None,
        )
        if installer is None:
            raise ValueError(f"Release {release.tag} has no downloadable .exe asset.")
        checksums = next(
            (
                asset
                for asset in release.assets
                if asset.name.lower() == "sha256sums.txt" and asset.url
            ),
            None,
        )
        if checksums is None:
            raise ValueError(
                f"Release {release.tag} is missing SHA256SUMS.txt; "
                "the installer cannot be verified."
            )

        try:
            async with httpx.AsyncClient(
                timeout=120.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                installer_response = await client.get(installer.url)
                installer_response.raise_for_status()
                checksum_response = await client.get(checksums.url)
                checksum_response.raise_for_status()
        except httpx.HTTPError as error:
            raise ValueError(f"Could not download release assets: {error}") from error

        data = installer_response.content
        expected = self._expected_hash(checksum_response.text, installer.name)
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != expected.lower():
            raise ValueError(
                f"SHA256 mismatch for {installer.name}: expected {expected}, "
                f"got {actual}. Nothing was written."
            )

        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / installer.name
        temporary = destination / f"{installer.name}.{os.getpid()}.{time.time_ns()}.part"
        temporary.write_bytes(data)
        temporary.replace(target)
        return target

    @staticmethod
    def launch_installer(path: Path) -> bool:
        """Hand a verified installer to Windows via ``os.startfile``."""
        starter = getattr(os, "startfile", None)
        if os.name != "nt" or starter is None:
            return False
        target = Path(path)
        if not target.exists():
            return False
        try:
            starter(str(target))
        except OSError:
            return False
        return True
