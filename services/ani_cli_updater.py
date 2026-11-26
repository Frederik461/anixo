"""Safe ani-cli dependency update manager.

The upstream repository and download are derived from the already pinned
official URL in ``services/setup.py``.  ani-cli does not expose a reliable
version command here, so the installed revision is identified by SHA-256 and
mapped to known official tags.  Latest release metadata is fetched from the
public GitHub Releases API for the same upstream repository.

The manager never runs ``ani-cli -U`` and never executes a downloaded file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import Config, default_cache_dir
from platforms import PlatformAdapter, get_platform_adapter
from services import setup as setup_service
from services.ani_cli import (
    AniCliPatchError,
    AniCliService,
    patch_exact_provider_selection,
    sync_provider_url_from_script,
)
from services.dependency_manager import DependencyManager
from services.install_manifest import COMPONENT_ANI_CLI
from services.managed_ani_cli import ManagedAniCli
from services.update_log import UpdateLog, redact_update_text, redact_update_value
from services.updater import _version_key, parse_version

# Derived from ANI_CLI_URL = raw.githubusercontent.com/pystardust/ani-cli/v5.1/ani-cli
ANI_CLI_REPO = "pystardust/ani-cli"
ANI_CLI_RELEASES_URL = f"https://api.github.com/repos/{ANI_CLI_REPO}/releases/latest"
USER_AGENT = f"ANIXO/{setup_service.APP_VERSION}"
CACHE_TTL_SECONDS = 6 * 60 * 60

_KNOWN_REVISIONS: dict[str, str] = {
    setup_service.ANI_CLI_SHA256.upper(): "v5.1",
    # Official v5.1.8 master revision previously shipped on this machine.
    "A87F4B0EB178ED5B7856F93670015B2BA8832F1A9E634AD608967535364E77D9": "v5.1.8-master",
    # Official release that uses composite AniList:site search result keys.
    "60F10C12F2397811D137A76156555803D0913985FE73499DAFDA1A84771FE917": "v5.2",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


_SENSITIVE_QUERY_KEYS = {
    "sig",
    "se",
    "expires",
    "expiration",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "signature",
}


def _clean_https_url(url: str) -> bool:
    """Accept only HTTPS URLs without userinfo or credential-bearing query keys."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return False
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.strip().lower()
        if lowered in _SENSITIVE_QUERY_KEYS or lowered.startswith("x-amz-"):
            return False
    return True


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:400]


@dataclass(frozen=True, slots=True)
class AniCliInstalled:
    """Observed installed ani-cli revision and ownership."""

    path: str
    version: str
    revision: str
    digest: str
    owned_by_anixo: bool
    health: str
    detail: str
    patch_error: str = ""

    @property
    def installed(self) -> bool:
        return bool(self.path) and Path(self.path).exists()


@dataclass(frozen=True, slots=True)
class AniCliUpdateCheck:
    """Cached upstream check result."""

    installed_version: str
    latest_version: str
    update_available: bool
    last_checked: str
    asset_url: str
    expected_sha256: str
    error: str
    cached: bool = False
    message: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "checked_epoch": time.time(),
            "last_checked": self.last_checked,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "asset_url": self.asset_url,
            "expected_sha256": self.expected_sha256,
            "error": self.error,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, data: Any) -> AniCliUpdateCheck | None:
        if not isinstance(data, dict):
            return None
        try:
            epoch = float(data.get("checked_epoch", 0))
        except (TypeError, ValueError):
            return None
        if time.time() - epoch > CACHE_TTL_SECONDS:
            return None
        return cls(
            installed_version=str(data.get("installed_version") or ""),
            latest_version=str(data.get("latest_version") or ""),
            update_available=bool(data.get("update_available")),
            last_checked=str(data.get("last_checked") or ""),
            asset_url=str(data.get("asset_url") or ""),
            expected_sha256=str(data.get("expected_sha256") or ""),
            error=str(data.get("error") or ""),
            cached=True,
            message=str(data.get("message") or ""),
        )


@dataclass(frozen=True, slots=True)
class AniCliUpdateResult:
    """Outcome of an explicit, staged ani-cli update."""

    ok: bool
    message: str
    detail: str
    version: str = ""
    digest: str = ""
    backup: str = ""


class AniCliUpdateService:
    """Checks and safely installs ANIXO-managed ani-cli revisions."""

    def __init__(
        self,
        config: Config,
        data_dir: Path,
        ani_cli: AniCliService | None = None,
        manager: DependencyManager | None = None,
        adapter: PlatformAdapter | None = None,
    ) -> None:
        self.config = config
        self.data_dir = Path(data_dir).resolve()
        self.adapter = adapter or get_platform_adapter()
        self.ani_cli = ani_cli or AniCliService(config)
        self.manager = manager
        self.cache_dir = (
            Path(os.environ.get("ANIXO_CACHE_DIR") or "").resolve()
            if os.environ.get("ANIXO_CACHE_DIR")
            else default_cache_dir()
        )
        self.log = UpdateLog(self.data_dir)
        self._cache_path = self.data_dir / "updates" / "ani-cli-check.json"

    @property
    def managed(self) -> ManagedAniCli:
        """The local receipt beside ANIXO's dedicated ani-cli copy."""
        return self._manager().managed_ani_cli

    def _manager(self) -> DependencyManager:
        if self.manager is None:
            cache = (
                Path(os.environ.get("ANIXO_CACHE_DIR") or "").resolve()
                if os.environ.get("ANIXO_CACHE_DIR")
                else default_cache_dir()
            )
            from services.install_manifest import ManifestStore

            self.manager = DependencyManager(
                self.config,
                self.data_dir,
                cache,
                self.adapter,
                ManifestStore(self.data_dir, self.adapter),
            )
        return self.manager

    def _sync_provider_url(self, script: Path | None = None) -> bool:
        """Follow the provider URL declared by the installed ani-cli revision."""
        result = sync_provider_url_from_script(
            self.config, script or self.ani_cli.installed_script()
        )
        if not result:
            return False
        url, family, changed = result
        self.ani_cli._provider_url = url
        self.ani_cli._provider_family = family
        if changed:
            self.log.log("ani_cli_provider_url_synced", source=url)
        return changed

    def detect_installed(self) -> AniCliInstalled:
        """Report the managed copy first; never claim an independent one is owned.

        Ownership comes only from the receipt beside ANIXO's dedicated copy.  A
        script found through PATH or a user configuration is reported as
        independent and is never replaced.
        """
        state = self.managed.read()
        managed_path = state.script if state and state.script.exists() else None
        independent = self.ani_cli.installed_script()
        path = managed_path or independent
        if not path or not path.exists():
            return AniCliInstalled(
                "", "not installed", "", "", False, "missing",
                "No ani-cli script was detected.",
            )
        try:
            payload = path.read_bytes()
        except OSError as error:
            return AniCliInstalled(
                str(path), "unreadable", "", "", False, "unreadable", str(error)
            )
        digest = _digest(payload)
        owned = bool(managed_path and self.managed.is_owner(path))
        if owned and state is not None:
            version = state.version or _KNOWN_REVISIONS.get(digest, "")
        else:
            version = _KNOWN_REVISIONS.get(digest, "")
        if not version:
            version = f"unknown ({digest[:10]})"
        patch_error = ""
        try:
            patch_exact_provider_selection(payload.decode("utf-8"))
            patch_ok = True
        except (AniCliPatchError, UnicodeDecodeError, ValueError) as error:
            patch_ok = False
            patch_error = redact_update_text(str(error))
        if owned:
            health = "ANIXO-managed revision"
        elif digest in _KNOWN_REVISIONS:
            health = "verified official revision (independent)"
        else:
            health = "user-managed / unverified revision"
        detail = (
            f"Script: {path}\nSHA256: {digest}\n"
            f"Owner: {'ANIXO' if owned else 'user or another installer'}\n"
            f"Managed directory: {self.managed.directory}\n"
            f"Exact-provider patch: {'compatible' if patch_ok else 'out of date'}"
        )
        if not owned and independent and independent.exists():
            detail += "\nThe independent installation is left untouched."
        if patch_error:
            detail += f"\nPatch detail: {patch_error}"
        return AniCliInstalled(
            str(path), version, digest[:12], digest, owned, health, detail, patch_error
        )

    def _read_cache(self) -> AniCliUpdateCheck | None:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return AniCliUpdateCheck.from_mapping(data)

    def _write_cache(self, check: AniCliUpdateCheck) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_name(
                f"{self._cache_path.name}.{os.getpid()}.tmp"
            )
            payload = redact_update_value(check.to_mapping())
            if not _clean_https_url(str(payload.get("asset_url") or "")):
                payload["asset_url"] = ""
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self._cache_path)
        except OSError:
            self.log.log("ani_cli_cache_write_failed")

    async def check_async(self, *, force: bool = False) -> AniCliUpdateCheck:
        return await asyncio.to_thread(self.check, force=force)

    def check(self, *, force: bool = False) -> AniCliUpdateCheck:
        """Check the verified upstream release without changing anything."""
        installed = self.detect_installed()
        self._sync_provider_url()
        if not force:
            cached = self._read_cache()
            if cached and not cached.error:
                self.log.log("ani_cli_check_cache_hit", latest=cached.latest_version)
                return cached
        try:
            with httpx.Client(
                timeout=12.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            ) as client:
                response = client.get(ANI_CLI_RELEASES_URL)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            previous = self._read_cache()
            error_text = _safe_error(error)
            if previous:
                self.log.log("ani_cli_check_network_failed", error=error_text)
                return AniCliUpdateCheck(
                    previous.installed_version,
                    previous.latest_version,
                    previous.update_available,
                    previous.last_checked,
                    previous.asset_url,
                    previous.expected_sha256,
                    f"Upstream check failed; showing last successful check. {error_text}",
                    cached=True,
                    message="Upstream check failed; showing the last successful check.",
                )
            result = AniCliUpdateCheck(
                installed.version,
                "",
                False,
                _timestamp(),
                "",
                "",
                error_text,
                message="ani-cli update check is unavailable while offline.",
            )
            self.log.log("ani_cli_check_failed", error=error_text)
            return result
        try:
            parsed = self._parse_release(payload)
        except ValueError as error:
            result = AniCliUpdateCheck(
                installed.version, "", False, _timestamp(), "", "", _safe_error(error)
            )
            self.log.log("ani_cli_check_invalid_release", error=result.error)
            return result
        latest, url, expected = parsed
        newer = _version_key(parse_version(latest)) > _version_key(
            parse_version(installed.version)
        )
        changed = installed.digest.upper() != expected.upper()
        available = newer or changed
        result = AniCliUpdateCheck(
            installed.version,
            latest,
            available,
            _timestamp(),
            url,
            expected,
            "",
            message=(
                f"ani-cli {latest} is available."
                if available
                else f"ani-cli {installed.version} is current."
            ),
        )
        self._write_cache(result)
        self.log.log(
            "ani_cli_check_succeeded", latest=latest, update_available=available, source=url
        )
        return result

    @staticmethod
    def _parse_release(payload: Any) -> tuple[str, str, str]:
        if not isinstance(payload, dict):
            raise ValueError("GitHub returned an unexpected ani-cli release payload.")
        tag = str(payload.get("tag_name") or "").strip()
        version = tag[1:] if tag.lower().startswith("v") else tag
        if not version or not re.match(r"^\d+\.\d+", version):
            raise ValueError("The ani-cli release has no usable version tag.")
        asset = next(
            (
                item
                for item in payload.get("assets", [])
                if isinstance(item, dict) and str(item.get("name") or "") == "ani-cli"
            ),
            None,
        )
        if not asset:
            raise ValueError("The ani-cli release has no official `ani-cli` asset.")
        url = str(asset.get("browser_download_url") or "").strip()
        if not _clean_https_url(url):
            raise ValueError("The ani-cli asset URL is not a clean HTTPS URL.")
        digest = str(asset.get("digest") or "").strip()
        if digest.lower().startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("The ani-cli release has no trusted SHA-256 digest.")
        known = _KNOWN_REVISIONS.get(digest.upper())
        if known and known.lstrip("v").split("-")[0] == version:
            return version, url, digest.upper()
        return version, url, digest.upper()

    def update(
        self,
        check: AniCliUpdateCheck | None = None,
        *,
        progress: Any = None,
        should_cancel: Any = None,
    ) -> AniCliUpdateResult:
        """Update ANIXO's managed copy, creating it beside any independent one."""
        installed = self.detect_installed()
        adopted = False
        if not installed.installed or not installed.owned_by_anixo:
            ensured = self._manager().ensure_managed_ani_cli()
            if not ensured.ok:
                return AniCliUpdateResult(
                    False,
                    "Could not create an ANIXO-managed ani-cli installation.",
                    ensured.detail,
                )
            adopted = not installed.owned_by_anixo
            self.config.ani_cli_path = ensured.install_location
            installed = self.detect_installed()
            if not installed.installed or not installed.owned_by_anixo:
                return AniCliUpdateResult(
                    False,
                    "The ANIXO-managed ani-cli copy could not be verified.",
                    installed.detail,
                )

        check = check or self.check()
        if check.error:
            return AniCliUpdateResult(False, "Update check failed.", check.error)
        if not check.update_available:
            return AniCliUpdateResult(
                True,
                (
                    "ANIXO-managed ani-cli is current; the independent installation "
                    "was left untouched."
                    if adopted
                    else "ani-cli is already current."
                ),
                installed.detail,
                version=installed.version,
                digest=installed.digest,
            )
        if not check.asset_url or not check.expected_sha256:
            return AniCliUpdateResult(
                False, "No verified ani-cli candidate is available.", check.message
            )
        if should_cancel and should_cancel():
            return AniCliUpdateResult(False, "Update cancelled.", "No files were changed.")

        final_path = Path(installed.path)
        state = self.managed.read()
        if state is None or not self.managed.is_owner(final_path):
            return AniCliUpdateResult(
                False,
                "ani-cli ownership could not be verified.",
                "No script was replaced.",
            )

        base = self.managed.directory / "updates"
        staging = base / "staging"
        backups = base / "backups"
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup = backups / stamp
        wrapper = final_path.with_suffix(".cmd")
        created: list[Path] = []
        try:
            staging.mkdir(parents=True, exist_ok=True)
            if progress:
                progress("Downloading the verified ani-cli candidate…")
            payload = self._download(check.asset_url, should_cancel)
            actual = _digest(payload)
            if actual != str(check.expected_sha256).upper():
                raise ValueError(
                    f"SHA256 mismatch: expected {check.expected_sha256}, got {actual}."
                )
            if not setup_service._looks_like_ani_cli(payload):
                raise ValueError("Candidate does not look like the official ani-cli script.")
            staged = setup_service.install_verified_ani_cli_payload(
                staging,
                payload,
                expected_sha256=check.expected_sha256,
                overwrite=True,
                version=check.latest_version,
                source=check.asset_url,
                write_wrapper=False,
            )
            if not staged.ok:
                raise ValueError(staged.detail or staged.message)
            staged_script = Path(staged.install_location)
            created.append(staged_script)
            try:
                patch_exact_provider_selection(
                    staged_script.read_text(encoding="utf-8")
                )
            except (AniCliPatchError, OSError, UnicodeDecodeError, ValueError) as error:
                raise AniCliPatchError(
                    "ani-cli patch is out of date for this ani-cli version; "
                    "the candidate cannot be patched safely."
                ) from error

            if progress:
                progress("Backing up the ANIXO-managed ani-cli copy…")
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_path, backup / final_path.name)
            if wrapper.exists():
                shutil.copy2(wrapper, backup / wrapper.name)
            self.managed.backup(backup)
            if should_cancel and should_cancel():
                raise asyncio.CancelledError("Update cancelled before installation.")

            if progress:
                progress("Installing the staged candidate…")
            final = setup_service.install_verified_ani_cli_payload(
                self.managed.directory,
                payload,
                expected_sha256=check.expected_sha256,
                overwrite=True,
                version=check.latest_version,
                source=check.asset_url,
            )
            if not final.ok:
                raise ValueError(final.detail or final.message)
            installed_path = Path(final.install_location)
            if _digest(installed_path.read_bytes()) != str(check.expected_sha256).upper():
                raise ValueError("Post-install checksum verification failed.")
            try:
                patch_exact_provider_selection(
                    installed_path.read_text(encoding="utf-8")
                )
            except (AniCliPatchError, OSError, UnicodeDecodeError, ValueError) as error:
                raise AniCliPatchError(
                    "ani-cli patch is out of date for this ani-cli version; "
                    "the installed candidate could not be verified."
                ) from error
            self._sync_provider_url(installed_path)
            self._manager().record_ani_cli_update(
                version=check.latest_version,
                source=check.asset_url,
                install_location=str(installed_path),
                sha256=actual,
                config_path=str(installed_path),
            )
            final_installed = self.detect_installed()
            cache_version = (
                final_installed.version
                if final_installed.installed and final_installed.digest.upper() == actual
                else check.latest_version
            )
            self._write_cache(
                AniCliUpdateCheck(
                    cache_version,
                    check.latest_version,
                    False,
                    _timestamp(),
                    check.asset_url,
                    check.expected_sha256,
                    "",
                    message=f"ani-cli {check.latest_version} is installed.",
                )
            )
            self.log.log(
                "ani_cli_update_succeeded", version=check.latest_version, sha256=actual
            )
            return AniCliUpdateResult(
                True,
                f"ani-cli {check.latest_version} installed and verified.",
                final.detail,
                version=check.latest_version,
                digest=actual,
                backup=str(backup),
            )
        except (asyncio.CancelledError, ValueError, OSError, httpx.HTTPError) as error:
            detail = _safe_error(error)
            for path in created:
                if path.is_file() and staging in path.parents:
                    path.unlink(missing_ok=True)
            if backup.exists():
                try:
                    previous = backup / final_path.name
                    if previous.is_file():
                        shutil.copy2(previous, final_path)
                    previous_wrapper = backup / wrapper.name
                    if previous_wrapper.is_file():
                        shutil.copy2(previous_wrapper, wrapper)
                    self.managed.restore(backup)
                    detail += " The previous ANIXO-managed installation was restored."
                except OSError as restore_error:
                    detail += f" Rollback failed: {restore_error}"
            self.log.log("ani_cli_update_failed", error=detail)
            return AniCliUpdateResult(
                False,
                "ani-cli update failed; the previous installation remains available.",
                detail,
            )

    @staticmethod
    def _download(url: str, should_cancel: Any = None) -> bytes:
        if not _clean_https_url(url):
            raise ValueError("Refusing to download ani-cli from an unverified URL.")
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(64 * 1024):
                    if should_cancel and should_cancel():
                        raise asyncio.CancelledError("Update cancelled during download.")
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ValueError("ani-cli candidate is unexpectedly large.")
        return b"".join(chunks)
