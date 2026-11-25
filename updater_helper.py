"""Separate ANIXO updater helper.

The running application never replaces its own executable.  It verifies and
stages an update, exits, then starts this helper with a JSON plan.  The helper
rechecks the checksum, validates the package, backs up the existing app, applies
the package with platform-appropriate operations, runs a final health check,
and restores the backup if anything fails.

This helper only installs already-downloaded, already-verified packages.  It
never contacts GitHub and never reads credentials.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from services.update_log import UpdateLog
from services.updater import validate_package

PRESERVE_NAMES = {
    "PORTABLE.txt",
    "PORTABLE.md",
    "START ANIXO.cmd",
    "START ANIXO.sh",
    "uninstall-paths.ini",
    "anixo-uninstall.cmd",
    "config.json",
}
SCHEMA_VERSION = 1


class UpdateHelperError(RuntimeError):
    """A failure that must trigger rollback."""


def _load_plan(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or int(data.get("schema_version", 0)) != SCHEMA_VERSION:
        raise UpdateHelperError("Update plan is missing or uses an unsupported schema.")
    for key in (
        "version",
        "package_path",
        "asset_name",
        "sha256",
        "install_dir",
        "executable",
        "data_dir",
        "platform",
        "arch",
    ):
        if not data.get(key):
            raise UpdateHelperError(f"Update plan has no {key}.")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _safe_zip_names(archive: zipfile.ZipFile) -> tuple[str, ...]:
    names = tuple(archive.namelist())
    for name in names:
        candidate = PurePosixPath(name.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or ":" in name[:3]:
            raise UpdateHelperError(f"Update package contains an unsafe path: {name}")
    return names


def _extract_zip(package: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        names = _safe_zip_names(archive)
        archive.extractall(destination)
    for name in names:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if len(parts) >= 2 and parts[-1] == "Anixo.exe":
            return destination.joinpath(*parts[:-1])
        if "ANIXO.app" in parts:
            index = parts.index("ANIXO.app")
            return destination.joinpath(*parts[: index + 1])
    return destination


def _mount_dmg(package: Path) -> tuple[Path, Any]:
    if sys.platform != "darwin":
        raise UpdateHelperError("A macOS DMG can only be mounted on macOS.")
    process = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-noautoopen", str(package)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise UpdateHelperError("Could not mount the verified DMG.")
    mounts = [line.split("\t")[-1] for line in process.stdout.splitlines() if "\t/" in line]
    if not mounts:
        subprocess.run(["hdiutil", "detach", str(package)], capture_output=True, check=False)
        raise UpdateHelperError("Could not locate the mounted DMG volume.")
    return Path(mounts[-1]), package


def _macos_app_from_package(package: Path, asset_name: str, staging: Path) -> tuple[Path, Any]:
    if asset_name.lower().endswith(".zip"):
        extracted = _extract_zip(package, staging)
        app = extracted if extracted.name == "ANIXO.app" else extracted / "ANIXO.app"
    elif asset_name.lower().endswith(".dmg"):
        mount, handle = _mount_dmg(package)
        app = mount / "ANIXO.app"
        if not app.exists():
            subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True, check=False)
            raise UpdateHelperError("DMG does not contain ANIXO.app.")
        return app, mount
    else:
        raise UpdateHelperError(f"Unsupported macOS update asset: {asset_name}")
    if not app.exists():
        raise UpdateHelperError("Update package does not contain ANIXO.app.")
    return app, None


def _macos_arch(app: Path) -> str:
    info_path = app / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
        executable = str(info.get("CFBundleExecutable") or "Anixo")
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise UpdateHelperError(f"Could not read ANIXO.app Info.plist: {error}") from error
    candidates = [
        app / "Contents" / "MacOS" / executable,
        app / "Contents" / "Resources" / "Anixo" / "Anixo",
    ]
    binary = next((item for item in candidates if item.is_file()), None)
    if binary is None:
        raise UpdateHelperError("ANIXO.app has no executable.")
    lipo = shutil.which("lipo")
    if not lipo:
        raise UpdateHelperError("lipo is required to verify macOS architecture.")
    result = subprocess.run(
        [lipo, "-archs", str(binary)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateHelperError("Could not inspect ANIXO.app architecture.")
    return result.stdout.strip().lower()


def _codesign_status(app: Path) -> str:
    codesign = shutil.which("codesign")
    if not codesign:
        return "codesign unavailable"
    result = subprocess.run(
        [codesign, "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return "signature VERIFIED" if result.returncode == 0 else "signature FAILED or absent"


def _notarization_status(app: Path) -> str:
    spctl = shutil.which("spctl")
    if not spctl:
        return "notarization check unavailable"
    result = subprocess.run(
        [spctl, "-a", "-vv", str(app)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (
        "Gatekeeper assessment passed"
        if result.returncode == 0
        else "Gatekeeper assessment failed or unsigned"
    )


def _ignored(source: Path, plan: dict[str, Any]) -> set[Path]:
    ignored: set[Path] = set()
    for key in ("data_dir", "cache_dir", "log_dir"):
        value = plan.get(key)
        if value:
            ignored.add(Path(value).resolve())
    config = plan.get("config_path")
    if config:
        ignored.add(Path(config).resolve())
    return ignored


def _copy_preserving(
    source: Path,
    destination: Path,
    *,
    ignored: set[Path] | None = None,
    skip_names: set[str] | None = None,
) -> None:
    ignored = ignored or set()
    skip_names = skip_names or set()
    source = source.resolve()
    if source in ignored:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            if child.resolve() in ignored:
                continue
            _copy_preserving(
                child,
                destination / child.name,
                ignored=ignored,
                skip_names=skip_names,
            )
        return
    if source.name in skip_names:
        return
    shutil.copy2(source, destination, follow_symlinks=False)


def _backup(plan: dict[str, Any], stamp: str) -> Path:
    root = Path(plan["backup_dir"])
    backup = root / f"{stamp}-{plan['version']}"
    install = Path(plan["install_dir"])
    if install.exists():
        _copy_preserving(
            install,
            backup / "installation",
            ignored=_ignored(install, plan),
        )
    (backup / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return backup


def _restore(plan: dict[str, Any], backup: Path) -> None:
    """Overlay the pre-update installation without deleting user data."""
    install = Path(plan["install_dir"])
    source = backup / "installation"
    if not source.exists():
        return
    _copy_preserving(source, install)


def _health_check(plan: dict[str, Any]) -> str:
    executable = Path(plan["executable"])
    if not executable.exists():
        raise UpdateHelperError(f"Updated executable is missing: {executable}")
    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0 or plan["version"] not in output:
        raise UpdateHelperError(f"Updated app failed its health check: {output[:300]}")
    return output.splitlines()[0] if output else "version check passed"


def _restart(plan: dict[str, Any]) -> None:
    executable = Path(plan["executable"])
    environment = os.environ.copy()
    for key, env_name in (
        ("data_dir", "ANIXO_DATA_DIR"),
        ("cache_dir", "ANIXO_CACHE_DIR"),
        ("log_dir", "ANIXO_LOG_DIR"),
        ("config_path", "ANIXO_CONFIG"),
    ):
        value = plan.get(key)
        if value:
            environment[env_name] = str(value)
    environment["ANIXO_UPDATE_APPLIED"] = str(plan["version"])
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=environment,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=environment,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _write_result(plan: dict[str, Any], payload: dict[str, Any]) -> Path:
    root = Path(plan["data_dir"]) / "updates"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "last-result.json"
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_parent(plan: dict[str, Any], timeout: float = 30.0) -> None:
    """Wait briefly for the app that launched this helper to exit cleanly."""
    try:
        pid = int(plan.get("parent_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.25)


def apply_plan(plan_path: Path) -> dict[str, Any]:
    """Apply a verified plan, restoring the previous app on any failure."""
    plan = _load_plan(Path(plan_path))
    log = UpdateLog(plan["data_dir"])
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup: Path | None = None
    staging = Path(plan["data_dir"]) / "updates" / "helper-staging" / stamp
    result: dict[str, Any] = {
        "ok": False,
        "version": plan["version"],
        "restarted": False,
        "backup": "",
        "message": "",
        "detail": "",
    }
    try:
        _wait_for_parent(plan)
        log.log("helper_started", version=plan["version"], platform=plan["platform"])
        package = Path(plan["package_path"]).resolve()
        if not package.is_file():
            raise UpdateHelperError("Staged update package is missing.")
        actual = _sha256(package)
        if actual != str(plan["sha256"]).lower():
            raise UpdateHelperError("Staged package failed its checksum recheck.")
        validate_package(
            package,
            type("Asset", (), {"name": plan["asset_name"]})(),  # type: ignore[arg-type]
            type("Target", (), {"system": plan["platform"], "arch": plan["arch"]})(),  # type: ignore[arg-type]
        )
        backup = _backup(plan, stamp)
        result["backup"] = str(backup)
        staging.mkdir(parents=True, exist_ok=True)
        install = Path(plan["install_dir"])
        if plan["platform"] == "windows" and str(plan["asset_name"]).lower().endswith(".exe"):
            args = [
                str(package),
                "/VERYSILENT",
                "/NORESTART",
                "/CLOSEAPPLICATIONS",
                f"/DIR={install}",
            ]
            process = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
            if process.returncode != 0:
                raise UpdateHelperError(
                    f"Installer exited with {process.returncode}: "
                    f"{(process.stdout + process.stderr)[-300:]}"
                )
        else:
            # Portable and app-bundle updates replace only app-managed files.
            # The whole install directory is never deleted: user data, local
            # config, shortcuts and user-added files must survive an update.
            install.parent.mkdir(parents=True, exist_ok=True)
            if plan["platform"] == "windows":
                extracted = _extract_zip(package, staging)
                _copy_preserving(
                    extracted,
                    install,
                    ignored=_ignored(install, plan),
                    skip_names=PRESERVE_NAMES,
                )
            else:
                app, mount = _macos_app_from_package(package, str(plan["asset_name"]), staging)
                arch = _macos_arch(app)
                if arch != str(plan["arch"]):
                    raise UpdateHelperError(
                        f"macOS package is {arch or 'unknown'}, expected {plan['arch']}."
                    )
                result["signature"] = _codesign_status(app)
                result["notarization"] = _notarization_status(app)
                existing = install / "ANIXO.app"
                if existing.exists():
                    shutil.rmtree(existing)
                _copy_preserving(app, existing, ignored=_ignored(install, plan))
                if mount is not None:
                    subprocess.run(
                        ["hdiutil", "detach", str(mount)],
                        capture_output=True,
                        timeout=60,
                        check=False,
                    )
        result["health"] = _health_check(plan)
        _restart(plan)
        result["restarted"] = True
        result["ok"] = True
        result["message"] = f"ANIXO {plan['version']} installed and restarted."
        log.log(
            "helper_succeeded",
            version=plan["version"],
            backup=str(backup),
            restarted=True,
        )
        shutil.rmtree(staging, ignore_errors=True)
    except (UpdateHelperError, OSError, ValueError, subprocess.SubprocessError) as error:
        result["detail"] = f"{type(error).__name__}: {error}"[:500]
        result["message"] = "Update failed; the previous installation was restored."
        if backup is not None:
            try:
                _restore(plan, backup)
            except OSError as restore_error:
                result["message"] = "Update failed and rollback needs attention."
                result["detail"] += f" Rollback: {restore_error}"[:500]
        log.log("helper_failed", version=plan["version"], error=result["detail"])
    _write_result(plan, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ANIXO-Updater",
        description="Apply a verified ANIXO update plan after ANIXO has exited.",
    )
    parser.add_argument("--plan", type=Path, required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = apply_plan(args.plan)
    except (OSError, ValueError) as error:
        print(f"ANIXO updater could not read the plan: {error}", file=sys.stderr)
        return 2
    print(result["message"])
    if result["detail"]:
        print(result["detail"], file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
