"""GitHub Releases / public-feed update checks for ANIXO.

This module never installs anything.  It checks release metadata, selects a
compatible asset, verifies a downloaded asset, and writes a plan for the
separate ``updater_helper.py`` process.  No credentials are read or sent.

The ANIXO repository is private.  Testers can be served through a public,
read-only release manifest URL configured in Settings (or
``ANIXO_UPDATE_FEED_URL``).  Without that, the service uses the unauthenticated
GitHub Releases API and reports a clear error when the repository is not public.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from config import Config, default_data_dir, expand_path
from platforms import PlatformAdapter, get_platform_adapter
from services.install_manifest import (
    COMPONENT_APP,
    ManifestStore,
)
from services.update_log import UpdateLog
from version import APP_NAME, APP_VERSION, DEFAULT_UPDATE_CHANNEL

DEFAULT_REPO = "Frederik461/anixo"
USER_AGENT = f"ANIXO/{APP_VERSION}"
_RELEASES_URL = "https://api.github.com/repos/{repo}/releases"
_LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
_CACHE_TTL_SECONDS = 6 * 60 * 60
_TRUSTED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com"}
_GITHUB_HOST_SUFFIXES = (".githubusercontent.com",)


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """One downloadable file attached to a GitHub release or manifest."""

    name: str
    url: str
    size: int = 0
    sha256: str = ""
    platform: str = ""
    arch: str = ""
    kind: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """Machine-readable release metadata published with each release."""

    schema_version: int
    version: str
    channel: str
    published_at: str
    notes: str
    assets: tuple[ReleaseAsset, ...]
    signature: str = ""
    signature_key_id: str = ""

    def asset_named(self, name: str) -> ReleaseAsset | None:
        return next((item for item in self.assets if item.name == name), None)


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """Normalized subset of a GitHub release used by the UI."""

    version: str
    tag: str
    notes: str
    published_at: str
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]
    manifest: ReleaseManifest | None = None
    source: str = "github"


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """Result of a release lookup, including a user-facing error string."""

    current: str
    available: str
    update_available: bool
    channel: str
    release: ReleaseInfo | None
    error: str
    cached: bool = False
    checked_at: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class PlatformTarget:
    """The supported OS/architecture pair used for asset selection."""

    system: str
    arch: str
    label: str

    @property
    def key(self) -> str:
        return f"{self.system}-{self.arch}"


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Progress callback payload for a streamed asset download."""

    stage: str
    completed: int = 0
    total: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """A verified package plus the plan consumed by the helper process."""

    version: str
    asset: ReleaseAsset
    package: Path
    plan_path: Path
    sha256: str
    verification: str
    notes: str
    release: ReleaseInfo

    @property
    def checksum_verified(self) -> bool:
        return bool(self.sha256)


@dataclass(slots=True)
class UpdateState:
    """Mutable UI-facing state shared by the app and update screen."""

    current: str = APP_VERSION
    available: str = ""
    latest: str = ""
    channel: str = DEFAULT_UPDATE_CHANNEL
    status: str = "No update check yet."
    notes: str = ""
    progress: str = ""
    verification: str = ""
    error: str = ""
    release: ReleaseInfo | None = None
    prepared: PreparedUpdate | None = None
    busy: bool = False
    can_cancel: bool = False
    restart_required: bool = False


def normalize_channel(channel: str | None) -> str:
    value = str(channel or DEFAULT_UPDATE_CHANNEL).strip().lower()
    if value in {"beta", "prerelease", "pre"}:
        return "beta"
    return "stable"


def normalize_arch(value: str | None) -> str:
    machine = str(value or "").strip().lower().replace("-", "_")
    if machine in {"arm64", "aarch64", "arm64e"}:
        return "arm64"
    if machine in {"x86_64", "x64", "amd64", "intel64", "i686", "x86"}:
        return "x86_64"
    return machine


def detect_target(system: str | None = None, machine: str | None = None) -> PlatformTarget:
    """Return a supported target, raising a clear error for anything else."""
    name = system or platform.system() or ("Windows" if os.name == "nt" else "Darwin")
    arch = normalize_arch(machine or platform.machine())
    if name == "Windows" and arch == "x86_64":
        return PlatformTarget("windows", "x86_64", "Windows 10/11 x64")
    if name == "Darwin" and arch == "arm64":
        return PlatformTarget("macos", "arm64", "macOS Apple Silicon ARM64")
    if name == "Darwin" and arch == "x86_64":
        return PlatformTarget("macos", "x86_64", "macOS Intel x86_64")
    return PlatformTarget(
        "unknown" if name not in {"Windows", "Darwin"} else name.lower(),
        arch or "unknown",
        f"{name} {arch or 'unknown'}",
    )


def parse_version(value: str) -> tuple[int, int, int, int, str]:
    """Parse ``0.1.0-beta.3`` into ``(major, minor, patch, pre_number, label)``.

    A leading ``v`` and build metadata after ``+`` are ignored.  Stable
    versions have an empty label; malformed input yields a low, stable tuple
    rather than raising.  This shape is kept for backward compatibility.
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
    rank = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "c": 2}.get(label, 1)
    if not label:
        return (major, minor, patch, 1, 0, "")
    return (major, minor, patch, 0, rank, f"{label}{max(1, number)}")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:400]


def _is_https(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


def _host_of(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def _host_allowed(url: str, trusted_hosts: set[str]) -> bool:
    host = _host_of(url)
    if not _is_https(url) or not host:
        return False
    if host in trusted_hosts or host in _TRUSTED_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _GITHUB_HOST_SUFFIXES)


def is_trusted_asset_url(url: str, trusted_hosts: set[str] | None = None) -> bool:
    """Public wrapper for the updater's HTTPS host allowlist."""
    return _host_allowed(url, set(trusted_hosts or ()))


def _asset_tokens(asset: ReleaseAsset) -> tuple[str, str, str, str]:
    name = str(asset.name).strip().lower()
    tokens = f"{name} {asset.platform} {asset.arch} {asset.kind}".strip().lower()
    system = "windows" if any(x in tokens for x in ("windows", "win-x64", "win64", "setup", "portable")) else ""
    if not system and any(x in tokens for x in ("macos", "mac-", "darwin", "apple", ".dmg")):
        system = "macos"
    arch = ""
    if any(x in tokens for x in ("arm64", "aarch64", "apple-silicon", "apple_silicon")):
        arch = "arm64"
    elif any(x in tokens for x in ("x86_64", "x64", "amd64", "intel")):
        arch = "x86_64"
    kind = "installer" if "setup" in tokens or name.endswith(".exe") else ""
    if not kind and any(x in tokens for x in ("portable", "zip")):
        kind = "portable"
    if name.endswith(".dmg"):
        kind = "dmg"
    # Windows release assets predate explicit architecture suffixes.  ANIXO
    # only publishes Windows x64, so a Windows-named asset is x86_64 unless an
    # explicit conflicting token was found above.
    if system == "windows" and not arch:
        arch = "x86_64"
    return system, arch, kind, name


def asset_matches(
    asset: ReleaseAsset, target: PlatformTarget, preferred_kind: str = ""
) -> bool:
    """Return whether an asset is a safe match for ``target``."""
    system, arch, kind, name = _asset_tokens(asset)
    if asset.platform and asset.platform.lower() not in {target.system, system}:
        return False
    if asset.arch and normalize_arch(asset.arch) != target.arch:
        return False
    if system and system != target.system:
        return False
    if arch and arch != target.arch:
        return False
    if not system or not arch:
        return False
    if (
        "uninstaller" in name
        or "sha256sums" in name
        or "manifest" in name
        or "updater" in name
        or "helper" in name
    ):
        return False
    if target.system == "windows":
        if not (name.endswith(".exe") or name.endswith(".zip")):
            return False
    elif target.system == "macos":
        if not (name.endswith(".dmg") or name.endswith(".zip")):
            return False
    else:
        return False
    if preferred_kind == "installer":
        return kind in {"installer", "dmg"} or "setup" in name
    if preferred_kind == "portable":
        return kind == "portable" or name.endswith(".zip")
    return True


def select_asset(
    release: ReleaseInfo, target: PlatformTarget, preferred_kind: str = ""
) -> ReleaseAsset | None:
    """Pick the most specific compatible asset for the current machine."""
    assets = [asset for asset in release.assets if asset_matches(asset, target, preferred_kind)]
    if not assets:
        return None

    def rank(asset: ReleaseAsset) -> tuple[int, int, int]:
        _, _, kind, name = _asset_tokens(asset)
        if preferred_kind == "installer":
            kind_rank = 3 if "setup" in name else 2 if kind == "dmg" else 1
        elif preferred_kind == "portable":
            kind_rank = 3 if "portable" in name else 2 if name.endswith(".zip") else 1
        else:
            kind_rank = 3 if "setup" in name else 2 if name.endswith(".zip") else 1
        manifest_rank = 1 if asset.sha256 else 0
        explicit_rank = 1 if asset.platform and asset.arch else 0
        return (kind_rank, manifest_rank, explicit_rank)

    return max(assets, key=rank)


def select_helper_asset(release: ReleaseInfo, target: PlatformTarget) -> ReleaseAsset | None:
    """Pick a verified standalone updater helper for this exact platform/arch."""
    for asset in release.assets:
        name = str(asset.name).strip().lower()
        if not ("updater" in name or "helper" in name):
            continue
        system, arch, _, _ = _asset_tokens(asset)
        if system and system != target.system:
            continue
        detected = normalize_arch(asset.arch) or arch
        # Helpers are executables: never fall back to another architecture.
        if detected != target.arch:
            continue
        if target.system == "windows" and name.endswith(".exe"):
            return asset
        if target.system == "macos" and (name.endswith(".zip") or name.endswith(".dmg")):
            return asset
    return None


def parse_manifest(data: Any, source_url: str = "") -> ReleaseManifest:
    """Parse and validate a machine-readable release manifest."""
    if not isinstance(data, dict):
        raise ValueError("Release manifest must be a JSON object.")
    schema = int(data.get("schema_version", 0))
    if schema != 1:
        raise ValueError(f"Unsupported release manifest schema {schema}.")
    version = str(data.get("version") or "").strip()
    if not version or not re.match(r"^v?\d+\.\d+\.\d+", version):
        raise ValueError("Release manifest has an invalid version.")
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("Release manifest has no asset list.")
    assets: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            raise ValueError(f"Release asset {name or '<unnamed>'} has no name or URL.")
        if source_url and not _host_allowed(url, {_host_of(source_url)}):
            raise ValueError(f"Release asset {name} is hosted outside the trusted feed.")
        digest = str(item.get("sha256") or item.get("digest") or "").strip()
        if digest.lower().startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        if digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"Release asset {name} has an invalid SHA-256 value.")
        assets.append(
            ReleaseAsset(
                name=name,
                url=url,
                size=int(item.get("size") or 0),
                sha256=digest.lower(),
                platform=str(item.get("platform") or ""),
                arch=normalize_arch(str(item.get("arch") or "")),
                kind=str(item.get("kind") or ""),
            )
        )
    if not assets:
        raise ValueError("Release manifest has no usable assets.")
    return ReleaseManifest(
        schema_version=schema,
        version=version.lstrip("v"),
        channel=normalize_channel(str(data.get("channel") or "")),
        published_at=str(data.get("published_at") or ""),
        notes=str(data.get("notes") or data.get("body") or ""),
        assets=tuple(assets),
        signature=str(data.get("signature") or ""),
        signature_key_id=str(data.get("signature_key_id") or ""),
    )


def _pe_machine(path: Path) -> str:
    """Return the machine field of a PE executable without executing it."""
    with path.open("rb") as stream:
        header = stream.read(0x40)
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise ValueError("Windows asset is not a valid PE executable.")
        offset = struct.unpack_from("<I", header, 0x3C)[0]
        stream.seek(offset)
        signature = stream.read(6)
        if len(signature) < 6 or signature[:4] != b"PE\0\0":
            raise ValueError("Windows asset has no PE header.")
        machine = struct.unpack_from("<H", signature, 4)[0]
    return {0x8664: "x86_64", 0xAA64: "arm64", 0x14C: "x86"}.get(machine, hex(machine))


def _safe_zip_names(path: Path) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as archive:
        names = tuple(archive.namelist())
    for name in names:
        candidate = PurePosixPath(name.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or ":" in name[:3]:
            raise ValueError(f"Update package contains an unsafe path: {name}")
    return names


def validate_package(path: Path, asset: ReleaseAsset, target: PlatformTarget) -> str:
    """Validate package structure and architecture before any install."""
    package = Path(path)
    if not package.is_file() or package.stat().st_size <= 0:
        raise ValueError("Update package is missing or empty.")
    name = asset.name.lower()
    if name.endswith(".exe"):
        machine = _pe_machine(package)
        if machine != target.arch:
            raise ValueError(
                f"Windows asset architecture {machine} does not match {target.arch}."
            )
        return "PE executable architecture verified"
    if name.endswith(".zip"):
        names = _safe_zip_names(package)
        if target.system == "windows":
            if not any(item.lower().endswith("anixo.exe") for item in names):
                raise ValueError("Portable package does not contain Anixo.exe.")
        else:
            if not any("ANIXO.app/Contents/Info.plist" in item for item in names):
                raise ValueError("macOS package does not contain ANIXO.app.")
        return "ZIP package structure verified"
    if name.endswith(".dmg"):
        with package.open("rb") as stream:
            stream.seek(max(0, package.stat().st_size - 512))
            trailer = stream.read(512)
        if b"koly" not in trailer and b"CDSA" not in trailer:
            raise ValueError("macOS DMG has no recognised DMG trailer.")
        return "DMG container verified (architecture checked by helper)"
    raise ValueError(f"Unsupported update asset type: {asset.name}")


def expected_installation(
    data_dir: Path,
    adapter: PlatformAdapter | None = None,
    *,
    install_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve the existing app installation from the ownership manifest."""
    active = adapter or get_platform_adapter()
    store = ManifestStore(data_dir, active)
    record = store.load().manifest.get(COMPONENT_APP)
    if install_dir is not None:
        root = Path(expand_path(str(install_dir))).resolve()
        kind = record.install_method if record else "portable"
    elif record and record.install_location:
        root = Path(expand_path(record.install_location)).resolve()
        kind = record.install_method or "unknown"
    else:
        root = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent
        )
        kind = "portable" if (root / "PORTABLE.txt").exists() else "unknown"
    if active.is_windows:
        executable = root / "Anixo.exe"
    elif active.is_macos:
        bundle = root / "ANIXO.app"
        launcher = bundle / "Contents" / "MacOS" / "ANIXO"
        binary = bundle / "Contents" / "Resources" / "Anixo" / "Anixo"
        executable = binary if binary.exists() or not launcher.exists() else launcher
    else:
        executable = root / "Anixo"
    return {
        "install_dir": str(root),
        "install_kind": kind,
        "executable": str(executable),
        "version": record.version if record else APP_VERSION,
        "owned": bool(record and record.installed_by_anixo),
    }


class UpdateService:
    """Checks releases and prepares verified, platform-specific packages."""

    def __init__(
        self,
        config: Config,
        repo: str | None = None,
        *,
        data_dir: Path | None = None,
        adapter: PlatformAdapter | None = None,
        target: PlatformTarget | None = None,
    ) -> None:
        self.config = config
        self._repo_argument = repo
        self.data_dir = Path(data_dir).resolve() if data_dir else default_data_dir()
        self.adapter = adapter or get_platform_adapter()
        self.target = target or detect_target(self.adapter.system)
        self.log = UpdateLog(self.data_dir)
        self._lock = asyncio.Lock()

    @property
    def repository(self) -> str:
        """Resolve ``owner/repo`` from config, environment, or the argument."""
        configured = str(self.config.extra.get("update_repo") or "").strip()
        environmental = os.environ.get("ANIXO_UPDATE_REPO", "").strip()
        return (
            configured
            or environmental
            or (self._repo_argument or "").strip()
            or DEFAULT_REPO
        )

    @property
    def feed_url(self) -> str:
        return str(self.config.update_feed_url or "").strip()

    @property
    def trusted_hosts(self) -> set[str]:
        hosts = set(_TRUSTED_HOSTS)
        if self.feed_url:
            hosts.add(_host_of(self.feed_url))
        return {host for host in hosts if host}

    def _cache_path(self, channel: str) -> Path:
        return self.data_dir / "updates" / f"release-{normalize_channel(channel)}.json"

    def _read_cache(self, channel: str) -> UpdateCheck | None:
        try:
            raw = json.loads(self._cache_path(channel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or float(raw.get("checked_epoch", 0)) <= 0:
            return None
        if time.time() - float(raw["checked_epoch"]) > _CACHE_TTL_SECONDS:
            return None
        release = self._release_from_mapping(raw.get("release")) if raw.get("release") else None
        return UpdateCheck(
            current=APP_VERSION,
            available=str(raw.get("available") or ""),
            update_available=bool(raw.get("update_available")),
            channel=normalize_channel(raw.get("channel") or channel),
            release=release,
            error=str(raw.get("error") or ""),
            cached=True,
            checked_at=str(raw.get("checked_at") or ""),
            message=str(raw.get("message") or ""),
        )

    def _write_cache(self, check: UpdateCheck) -> None:
        path = self._cache_path(check.channel)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "checked_epoch": time.time(),
                "checked_at": _timestamp() or check.checked_at,
                "available": check.available,
                "update_available": check.update_available,
                "channel": check.channel,
                "error": check.error,
                "message": check.message,
                "release": self._release_mapping(check.release) if check.release else None,
            }
            temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            self.log.log("cache_write_failed", channel=check.channel)

    @staticmethod
    def _release_mapping(release: ReleaseInfo) -> dict[str, Any]:
        return {
            "version": release.version,
            "tag": release.tag,
            "notes": release.notes,
            "published_at": release.published_at,
            "prerelease": release.prerelease,
            "source": release.source,
            "assets": [
                {
                    "name": item.name,
                    "url": item.url,
                    "size": item.size,
                    "sha256": item.sha256,
                    "platform": item.platform,
                    "arch": item.arch,
                    "kind": item.kind,
                }
                for item in release.assets
            ],
            "manifest": (
                {
                    "schema_version": release.manifest.schema_version,
                    "version": release.manifest.version,
                    "channel": release.manifest.channel,
                    "published_at": release.manifest.published_at,
                    "notes": release.manifest.notes,
                    "signature": release.manifest.signature,
                    "signature_key_id": release.manifest.signature_key_id,
                    "assets": [
                        {
                            "name": item.name,
                            "url": item.url,
                            "size": item.size,
                            "sha256": item.sha256,
                            "platform": item.platform,
                            "arch": item.arch,
                            "kind": item.kind,
                        }
                        for item in release.manifest.assets
                    ],
                }
                if release.manifest
                else None
            ),
        }

    @staticmethod
    def _release_from_mapping(data: Any) -> ReleaseInfo | None:
        if not isinstance(data, dict):
            return None
        assets = tuple(
            ReleaseAsset(
                name=str(item.get("name") or ""),
                url=str(item.get("url") or ""),
                size=int(item.get("size") or 0),
                sha256=str(item.get("sha256") or ""),
                platform=str(item.get("platform") or ""),
                arch=str(item.get("arch") or ""),
                kind=str(item.get("kind") or ""),
            )
            for item in data.get("assets", [])
            if isinstance(item, dict) and item.get("name")
        )
        manifest = None
        raw_manifest = data.get("manifest")
        if isinstance(raw_manifest, dict):
            try:
                manifest = parse_manifest(raw_manifest)
            except ValueError:
                manifest = None
        return ReleaseInfo(
            version=str(data.get("version") or ""),
            tag=str(data.get("tag") or ""),
            notes=str(data.get("notes") or ""),
            published_at=str(data.get("published_at") or ""),
            prerelease=bool(data.get("prerelease")),
            assets=assets,
            manifest=manifest,
            source=str(data.get("source") or "github"),
        )

    async def _fetch_json(self, url: str) -> Any:
        if not _is_https(url):
            raise ValueError("Update feeds must use HTTPS.")
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json, application/json",
            },
        ) as client:
            response = await client.get(url)
            if response.status_code in {401, 403, 404}:
                raise ValueError(
                    "The update source is not publicly readable. Configure a "
                    "public update feed URL in Settings → Updates."
                )
            response.raise_for_status()
            return response.json()

    async def check(self, channel: str | None = None, *, force: bool = False) -> UpdateCheck:
        """Return the newest compatible release for ``channel``."""
        name = normalize_channel(channel or self.config.update_channel)
        async with self._lock:
            if not force:
                cached = self._read_cache(name)
                if cached:
                    self.log.log("check_cache_hit", channel=name, available=cached.available)
                    return cached
            try:
                if self.feed_url:
                    payload = await self._fetch_json(self.feed_url)
                    manifest = parse_manifest(payload, self.feed_url)
                    releases = [
                        ReleaseInfo(
                            version=manifest.version,
                            tag=f"v{manifest.version}",
                            notes=manifest.notes,
                            published_at=manifest.published_at,
                            prerelease="-" in manifest.version,
                            assets=manifest.assets,
                            manifest=manifest,
                            source="feed",
                        )
                    ]
                else:
                    payload = await self._fetch_json(
                        _RELEASES_URL.format(repo=self.repository)
                    )
                    if not isinstance(payload, list):
                        raise ValueError("GitHub returned an unexpected releases payload.")
                    releases = []
                    for item in payload:
                        if isinstance(item, dict):
                            try:
                                releases.append(self._release_from_json(item))
                            except (KeyError, TypeError, ValueError):
                                continue
            except (httpx.HTTPError, ValueError) as error:
                result = UpdateCheck(
                    APP_VERSION, "", False, name, None, _safe_error(error), checked_at=_timestamp()
                )
                self.log.log("check_failed", channel=name, error=result.error)
                return result

        if name == "stable":
            releases = [release for release in releases if not release.prerelease]
        compatible = [
            release for release in releases if select_asset(release, self.target) is not None
        ]
        if not compatible:
            message = (
                f"No {name} release has a compatible asset for {self.target.label}."
            )
            result = UpdateCheck(APP_VERSION, "", False, name, None, "", message=message)
            self.log.log("check_no_compatible_asset", channel=name, target=self.target.key)
            self._write_cache(result)
            return result
        release = max(compatible, key=lambda item: _version_key(parse_version(item.version)))
        available = release.version
        newer = _version_key(parse_version(available)) > _version_key(
            parse_version(APP_VERSION)
        )
        result = UpdateCheck(
            APP_VERSION,
            available,
            newer,
            name,
            release,
            "",
            checked_at=_timestamp(),
            message=(
                f"ANIXO {available} is available."
                if newer
                else f"ANIXO {APP_VERSION} is current on the {name} channel."
            ),
        )
        self.log.log(
            "check_succeeded",
            channel=name,
            available=available,
            update_available=newer,
            source=release.source,
        )
        self._write_cache(result)
        return result

    @staticmethod
    def _release_from_json(item: dict[str, Any]) -> ReleaseInfo:
        tag = str(item.get("tag_name") or item.get("tag") or "")
        version = tag[1:] if tag.lower().startswith("v") else tag
        if not version or not re.match(r"^\d+\.\d+\.\d+", version):
            raise ValueError("Release has no semantic version tag.")
        assets: list[ReleaseAsset] = []
        for asset in item.get("assets", []):
            if not isinstance(asset, dict) or not asset.get("name"):
                continue
            digest = str(asset.get("digest") or "").strip()
            if digest.lower().startswith("sha256:"):
                digest = digest.split(":", 1)[1]
            assets.append(
                ReleaseAsset(
                    name=str(asset.get("name")),
                    url=str(asset.get("browser_download_url") or asset.get("url") or ""),
                    size=int(asset.get("size") or 0),
                    sha256=digest.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", digest) else "",
                )
            )
        return ReleaseInfo(
            version=version,
            tag=tag,
            notes=str(item.get("body") or ""),
            published_at=str(item.get("published_at") or ""),
            prerelease=bool(item.get("prerelease")),
            assets=tuple(assets),
            source="github",
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
                return match.group(1).lower()
        raise ValueError(f"SHA256SUMS.txt has no entry for {filename}.")

    async def _fetch_text(self, url: str) -> str:
        if not _host_allowed(url, self.trusted_hosts):
            raise ValueError("Release asset URL is outside the trusted update source.")
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _resolve_checksum(self, release: ReleaseInfo, asset: ReleaseAsset) -> str:
        if asset.sha256:
            return asset.sha256.lower()
        if release.manifest:
            candidate = release.manifest.asset_named(asset.name)
            if candidate and candidate.sha256:
                return candidate.sha256.lower()
        sums = next(
            (
                item
                for item in release.assets
                if item.name.lower() == "sha256sums.txt" and item.url
            ),
            None,
        )
        if not sums:
            raise ValueError(
                f"Release {release.tag} has no trusted checksum for {asset.name}."
            )
        contents = await self._fetch_text(sums.url)
        return self._expected_hash(contents, asset.name)

    async def download_asset(
        self,
        asset: ReleaseAsset,
        destination_dir: Path,
        expected_sha256: str,
        *,
        progress: Callable[[DownloadProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Path:
        """Stream, hash, and verify an asset before writing its final path."""
        if not _host_allowed(asset.url, self.trusted_hosts):
            raise ValueError("Update asset is hosted outside the trusted update source.")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("Update asset has no valid SHA-256 checksum.")
        destination = Path(destination_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / Path(asset.name).name
        temporary = destination / f"{target.name}.{os.getpid()}.{time.time_ns()}.part"
        digest = hashlib.sha256()
        completed = 0
        try:
            async with httpx.AsyncClient(
                timeout=120.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                async with client.stream("GET", asset.url) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length") or asset.size or 0)
                    if progress:
                        progress(DownloadProgress("downloading", 0, total, asset.name))
                    with temporary.open("wb") as stream:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if should_cancel and should_cancel():
                                raise asyncio.CancelledError("Update download cancelled.")
                            stream.write(chunk)
                            digest.update(chunk)
                            completed += len(chunk)
                            if progress:
                                progress(
                                    DownloadProgress("downloading", completed, total, asset.name)
                                )
        except (httpx.HTTPError, OSError):
            temporary.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            temporary.unlink(missing_ok=True)
            raise
        actual = digest.hexdigest().lower()
        if actual != expected_sha256.lower():
            temporary.unlink(missing_ok=True)
            raise ValueError(
                f"SHA256 mismatch for {asset.name}: expected {expected_sha256}, got {actual}."
            )
        if asset.size and completed != asset.size:
            temporary.unlink(missing_ok=True)
            raise ValueError(
                f"Size mismatch for {asset.name}: expected {asset.size}, got {completed}."
            )
        temporary.replace(target)
        self.log.log("download_verified", asset=asset.name, sha256=actual, size=completed)
        if progress:
            progress(DownloadProgress("verified", completed, completed, asset.name))
        return target

    @staticmethod
    def _local_helper_path() -> Path | None:
        """Find a helper that already exists on this machine, if any."""
        candidates: list[Path] = []
        configured = os.environ.get("ANIXO_UPDATER_HELPER", "").strip()
        if configured:
            candidates.append(Path(expand_path(configured)))
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            candidates.extend(
                [
                    root / ("ANIXO-Updater.exe" if os.name == "nt" else "ANIXO-Updater"),
                    root / "ANIXO-Updater",
                ]
            )
        else:
            candidates.append(Path(__file__).resolve().parent.parent / "updater_helper.py")
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return None

    async def _prepare_helper(
        self,
        release: ReleaseInfo,
        *,
        progress: Callable[[DownloadProgress], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> Path | None:
        """Bootstrap a verified helper for installations that lack one."""
        local = self._local_helper_path()
        if local is not None:
            return local
        asset = select_helper_asset(release, self.target)
        if asset is None:
            return None
        checksum = await self._resolve_checksum(release, asset)
        directory = self.data_dir / "updates" / "helper"
        helper = await self.download_asset(
            asset,
            directory,
            checksum,
            progress=progress,
            should_cancel=should_cancel,
        )
        if helper.name.lower().endswith(".zip"):
            names = _safe_zip_names(helper)
            with zipfile.ZipFile(helper) as archive:
                archive.extractall(directory)
            binaries = [
                directory / name
                for name in names
                if not name.endswith("/")
                and ("updater" in name.lower() or "helper" in name.lower())
            ]
            if not binaries:
                raise ValueError("The updater helper archive has no helper executable.")
            helper = binaries[0]
        if os.name != "nt":
            try:
                helper.chmod(helper.stat().st_mode | 0o111)
            except OSError:
                pass
        return helper.resolve()

    def _write_plan(self, prepared: dict[str, Any]) -> Path:
        staging = self.data_dir / "updates" / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        version = re.sub(r"[^A-Za-z0-9._-]", "_", str(prepared["version"]))
        path = staging / f"update-{version}-{int(time.time())}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(prepared, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    async def prepare_update(
        self,
        release: ReleaseInfo,
        *,
        preferred_kind: str = "",
        install_dir: Path | None = None,
        progress: Callable[[DownloadProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PreparedUpdate:
        """Download, verify, and stage a compatible update package."""
        if should_cancel and should_cancel():
            raise asyncio.CancelledError("Update cancelled before download.")
        installation = expected_installation(
            self.data_dir, self.adapter, install_dir=install_dir
        )
        if not preferred_kind:
            preferred_kind = (
                "installer"
                if "installer" in str(installation["install_kind"]).lower()
                else "portable"
            )
        asset = select_asset(release, self.target, preferred_kind)
        if not asset:
            raise ValueError(
                f"No compatible {preferred_kind} update exists for {self.target.label}."
            )
        if progress:
            progress(DownloadProgress("resolving", 0, 0, asset.name))
        if should_cancel and should_cancel():
            raise asyncio.CancelledError("Update cancelled before download.")
        checksum = await self._resolve_checksum(release, asset)
        if should_cancel and should_cancel():
            raise asyncio.CancelledError("Update cancelled before download.")
        staging = self.data_dir / "updates" / "staging" / release.version
        package = await self.download_asset(
            asset,
            staging,
            checksum,
            progress=progress,
            should_cancel=should_cancel,
        )
        if progress:
            progress(DownloadProgress("validating", package.stat().st_size, package.stat().st_size, asset.name))
        verification = validate_package(package, asset, self.target)
        signature = self._verify_signature(release.manifest)
        if signature[0] == "signature FAILED":
            raise ValueError(
                f"Release manifest signature verification failed: {signature[1]}"
            )
        helper = await self._prepare_helper(
            release, progress=progress, should_cancel=should_cancel
        )
        plan: dict[str, Any] = {
            "schema_version": 1,
            "app_name": APP_NAME,
            "version": release.version,
            "channel": normalize_channel(self.config.update_channel),
            "notes": release.notes,
            "published_at": release.published_at,
            "install_kind": installation["install_kind"],
            "install_dir": installation["install_dir"],
            "executable": installation["executable"],
            "package_path": str(package.resolve()),
            "asset_name": asset.name,
            "asset_kind": asset.kind,
            "platform": self.target.system,
            "arch": self.target.arch,
            "sha256": checksum.lower(),
            "signature_status": signature[0],
            "signature_detail": signature[1],
            "data_dir": str(self.data_dir),
            "cache_dir": str(self.adapter.cache_dir()),
            "log_dir": str(self.adapter.log_dir()),
            "config_path": str(self.adapter.config_path()),
            "backup_dir": str(self.data_dir / "updates" / "backups"),
            "source": release.source,
            "parent_pid": os.getpid(),
            "helper_path": str(helper) if helper else "",
        }
        plan_path = self._write_plan(plan)
        self.log.log(
            "update_prepared",
            version=release.version,
            asset=asset.name,
            plan=str(plan_path),
            signature=signature[0],
        )
        return PreparedUpdate(
            version=release.version,
            asset=asset,
            package=package,
            plan_path=plan_path,
            sha256=checksum.lower(),
            verification=f"{verification}; {signature[0]}",
            notes=release.notes,
            release=release,
        )

    def _verify_signature(self, manifest: ReleaseManifest | None) -> tuple[str, str]:
        public_key = self.config.update_public_key
        if not public_key:
            if not manifest or not manifest.signature:
                return (
                    "signature not provided",
                    "Checksum verified against the release manifest.",
                )
            return (
                "signature not checked",
                "No update public key configured; checksum verified from the release source.",
            )
        if not manifest or not manifest.signature:
            return (
                "signature FAILED",
                "A public key is configured but the manifest has no signature.",
            )
        key = Path(expand_path(public_key))
        if not key.is_file():
            return ("signature FAILED", "Configured update public key does not exist.")
        openssl = shutil.which("openssl")
        if not openssl:
            return ("signature FAILED", "openssl is unavailable; verification cannot be completed.")
        try:
            signature = base64.b64decode(manifest.signature, validate=True)
        except (ValueError, TypeError):
            return ("signature FAILED", "Manifest signature is not valid base64.")
        raw = json.dumps(self._release_signature_payload(manifest), sort_keys=True).encode()
        temporary = self.data_dir / "updates" / "staging"
        temporary.mkdir(parents=True, exist_ok=True)
        manifest_file = temporary / f"manifest-{time.time_ns()}.json"
        signature_file = temporary / f"manifest-{time.time_ns()}.sig"
        try:
            manifest_file.write_bytes(raw)
            signature_file.write_bytes(signature)
            result = subprocess.run(
                [
                    openssl,
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(key),
                    "-signature",
                    str(signature_file),
                    str(manifest_file),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            manifest_file.unlink(missing_ok=True)
            signature_file.unlink(missing_ok=True)
            return ("signature FAILED", "Could not run signature verification.")
        finally:
            manifest_file.unlink(missing_ok=True)
            signature_file.unlink(missing_ok=True)
        if result.returncode != 0:
            return ("signature FAILED", "Manifest signature did not verify.")
        return ("signature VERIFIED", "Release manifest signature verified.")

    @staticmethod
    def _release_signature_payload(manifest: ReleaseManifest) -> dict[str, Any]:
        return {
            "schema_version": manifest.schema_version,
            "version": manifest.version,
            "channel": manifest.channel,
            "published_at": manifest.published_at,
            "notes": manifest.notes,
            "assets": [
                {
                    "name": asset.name,
                    "sha256": asset.sha256,
                    "platform": asset.platform,
                    "arch": asset.arch,
                    "kind": asset.kind,
                }
                for asset in manifest.assets
            ],
        }

    def helper_command(self, plan_path: Path) -> list[str]:
        """Return a command that starts the separate updater helper."""
        try:
            payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            staged = str(payload.get("helper_path") or "").strip()
            if staged and Path(expand_path(staged)).exists():
                helper = Path(expand_path(staged))
                if helper.suffix.lower() == ".py":
                    return [sys.executable, str(helper), "--plan", str(plan_path)]
                return [str(helper), "--plan", str(plan_path)]
        except (OSError, ValueError):
            pass
        configured = os.environ.get("ANIXO_UPDATER_HELPER", "").strip()
        if configured:
            helper = Path(expand_path(configured))
            if helper.suffix.lower() == ".py":
                return [sys.executable, str(helper), "--plan", str(plan_path)]
            return [str(helper), "--plan", str(plan_path)]
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            name = "ANIXO-Updater.exe" if os.name == "nt" else "ANIXO-Updater"
            return [str(root / name), "--plan", str(plan_path)]
        script = Path(__file__).resolve().parent.parent / "updater_helper.py"
        return [sys.executable, str(script), "--plan", str(plan_path)]

    def launch_helper(self, plan_path: Path) -> bool:
        """Start the helper process after the app has closed cleanly."""
        command = self.helper_command(plan_path)
        if not Path(command[0]).exists() and not shutil.which(command[0]):
            self.log.log("helper_missing", command=command[0])
            return False
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            self.log.log("helper_launch_failed", error=str(error))
            return False
        self.log.log("helper_launched", plan=str(plan_path))
        return True

    # ------------------------------------------------------------------
    # Backward-compatible installer helper used by older callers/tests.

    async def download_installer(self, release: ReleaseInfo, destination_dir: Path) -> Path:
        """Download and verify a release's Windows setup executable."""
        asset = next(
            (
                item
                for item in release.assets
                if item.name.lower().startswith("anixo-setup-")
                and item.name.lower().endswith(".exe")
                and item.url
            ),
            None,
        )
        if asset is None:
            raise ValueError(f"Release {release.tag} has no downloadable setup executable.")
        sums = next(
            (
                item
                for item in release.assets
                if item.name.lower() == "sha256sums.txt" and item.url
            ),
            None,
        )
        if sums is None:
            raise ValueError(
                f"Release {release.tag} is missing SHA256SUMS.txt; "
                "the installer cannot be verified."
            )
        checksum = self._expected_hash(await self._fetch_text(sums.url), asset.name)
        return await self.download_asset(asset, destination_dir, checksum)

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
