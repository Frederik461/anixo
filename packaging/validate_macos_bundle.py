#!/usr/bin/env python3
"""Validate a built ANIXO macOS .app, DMG, and uninstaller on macOS.

The script is deliberately conservative. It reports headless CI results
separately from real Finder, Gatekeeper, and playback behaviour, which cannot
be proven by a GitHub-hosted runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_IDENTIFIER = "io.anixo.app"
EXPECTED_NAME = "ANIXO"
MIN_MACOS = "13.0"
STATUS_UNTESTED = "NOT TESTED (headless CI cannot verify)"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_FAILED = "FAILED"
STATUS_VERIFIED = "VERIFIED"
SYSTEM_LIBRARY_PREFIXES = ("/usr/lib/", "/System/Library/")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(command, capture_output=True, text=True, check=False)


def add_failure(failures: list[str], message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    failures.append(message)


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError:
        return False
    return magic in {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
    }


def lipo_architectures(path: Path) -> set[str]:
    completed = run(["lipo", "-info", str(path)])
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    text = completed.stdout
    match = re.search(r"are:\s*(.+)$", text, flags=re.MULTILINE)
    if not match:
        match = re.search(r"is architecture:\s*(\S+)", text)
    if not match:
        raise RuntimeError(f"lipo output could not be parsed: {text.strip()}")
    tokens = re.findall(r"[A-Za-z0-9_]+", match.group(1))
    return {token for token in tokens if token in {"arm64", "arm64e", "x86_64", "i386"}}


def all_macho_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and is_macho(path)
    ]


def runtime_dependencies(binary: Path) -> list[str]:
    completed = run(["otool", "-L", str(binary)])
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    dependencies: list[str] = []
    for index, line in enumerate(completed.stdout.splitlines()):
        stripped = line.strip()
        if index == 0 or not stripped:
            continue
        token = stripped.split(maxsplit=1)[0]
        if token.startswith("/") or token.startswith("@"):
            dependencies.append(token)
    return dependencies


def is_system_shared_cache_dependency(dependency: str) -> bool:
    """Return True for OS libraries resolved from the macOS dyld shared cache."""
    return dependency.startswith(SYSTEM_LIBRARY_PREFIXES)


def find_bundled_resource(bundle: Path, relative: str) -> Path | None:
    """Locate an app resource, including PyInstaller's ``_internal`` layout."""
    runtime = bundle / "Contents" / "Resources" / "Anixo"
    relative_path = PurePosixPath(relative)
    native_relative = Path(*relative_path.parts)
    for candidate in (
        runtime / native_relative,
        runtime / "_internal" / native_relative,
    ):
        if candidate.is_file():
            return candidate
    for candidate in runtime.rglob(relative_path.name):
        if candidate.is_file() and candidate.as_posix().endswith(f"/{relative_path.as_posix()}"):
            return candidate
    return None


def _bundled_filename_exists(
    relative: str, bundle: Path, bundle_files: dict[str, Path] | None
) -> bool:
    name = PurePosixPath(relative).name
    if bundle_files is not None:
        return name in bundle_files
    return any(path.name == name for path in bundle.rglob(name))


def dependency_exists(
    dependency: str,
    binary: Path,
    bundle: Path,
    bundle_files: dict[str, Path] | None = None,
) -> bool:
    if dependency.startswith("@rpath/"):
        relative = dependency.removeprefix("@rpath/")
        bases = (
            binary.parent,
            binary.parent / ".dylibs",
            bundle / "Contents" / "Frameworks",
            bundle / "Contents" / "Resources" / "Anixo",
            bundle / "Contents" / "Resources" / "Anixo" / "_internal",
        )
        if any((base / relative).exists() for base in bases):
            return True
        return _bundled_filename_exists(relative, bundle, bundle_files)
    if dependency.startswith("@loader_path/"):
        relative = dependency.removeprefix("@loader_path/")
        return (binary.parent / relative).exists() or (
            binary.parent / ".dylibs" / relative
        ).exists()
    if dependency.startswith("@executable_path/"):
        relative = dependency.removeprefix("@executable_path/")
        candidates = (
            bundle / "Contents" / "MacOS" / relative,
            bundle / "Contents" / "Resources" / "Anixo" / relative,
            bundle / "Contents" / "Resources" / "Anixo" / "_internal" / relative,
        )
        if any(candidate.exists() for candidate in candidates):
            return True
        return _bundled_filename_exists(relative, bundle, bundle_files)
    return Path(dependency).exists()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_info_plist(plist_path: Path, version: str, failures: list[str]) -> dict[str, Any]:
    lint = run(["plutil", "-lint", str(plist_path)])
    if lint.returncode != 0:
        add_failure(failures, f"Info.plist lint failed: {lint.stderr.strip()}")
        return {}
    try:
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        add_failure(failures, f"Info.plist could not be parsed: {exc}")
        return {}

    expected = {
        "CFBundleName": EXPECTED_NAME,
        "CFBundleDisplayName": EXPECTED_NAME,
        "CFBundleExecutable": EXPECTED_NAME,
        "CFBundleIdentifier": EXPECTED_IDENTIFIER,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": MIN_MACOS,
    }
    for key, value in expected.items():
        if info.get(key) != value:
            add_failure(
                failures,
                f"Info.plist {key} is {info.get(key)!r}; expected {value!r}.",
            )
    return info


def verify_bundle_structure(bundle: Path, version: str, failures: list[str]) -> dict[str, Any]:
    required = (
        bundle / "Contents" / "Info.plist",
        bundle / "Contents" / "MacOS" / "ANIXO",
        bundle / "Contents" / "Resources" / "Anixo" / "Anixo",
        bundle / "Contents" / "Resources" / "ANIXO-Uninstaller",
        bundle / "Contents" / "Resources" / "anixo.icns",
    )
    required_resources = (
        "styles/theme.tcss",
        "assets/anixo.png",
        "certifi/cacert.pem",
    )
    for path in required:
        if not path.is_file():
            add_failure(failures, f"Missing bundled file: {path}")
    resolved_resources: dict[str, str] = {}
    for relative in required_resources:
        resource = find_bundled_resource(bundle, relative)
        if resource is None:
            add_failure(failures, f"Missing bundled resource: {relative}")
        else:
            resolved_resources[relative] = str(resource.relative_to(bundle))
    for path in (
        bundle / "Contents" / "MacOS" / "ANIXO",
        bundle / "Contents" / "Resources" / "Anixo" / "Anixo",
        bundle / "Contents" / "Resources" / "ANIXO-Uninstaller",
    ):
        if path.is_file() and not os.access(path, os.X_OK):
            add_failure(failures, f"Bundled executable is not executable: {path}")

    launcher = bundle / "Contents" / "MacOS" / "ANIXO"
    if launcher.is_file():
        text = launcher.read_text(encoding="utf-8", errors="replace")
        if "open -a Terminal" not in text:
            add_failure(failures, "Finder launcher does not open the console binary in Terminal.app.")

    info = verify_info_plist(bundle / "Contents" / "Info.plist", version, failures)
    return {
        "required_files": [str(path.relative_to(bundle)) for path in required],
        "required_resources": resolved_resources,
        "info_plist": info,
    }


def verify_architectures(
    bundle: Path, uninstaller: Path, expected_arch: str, failures: list[str]
) -> dict[str, Any]:
    main = bundle / "Contents" / "Resources" / "Anixo" / "Anixo"
    targets = [("main binary", main), ("standalone uninstaller", uninstaller)]
    targets.extend(
        (f"Mach-O {path.relative_to(bundle)}", path) for path in all_macho_files(bundle)
    )
    checked = 0
    details: dict[str, list[str]] = {}
    extra_architectures: dict[str, list[str]] = {}
    for label, path in targets:
        if not path.is_file():
            add_failure(failures, f"Cannot verify architecture of missing file: {path}")
            continue
        try:
            architectures = lipo_architectures(path)
        except RuntimeError as exc:
            add_failure(failures, f"lipo failed for {label}: {exc}")
            continue
        checked += 1
        details[label] = sorted(architectures)
        if expected_arch not in architectures:
            add_failure(
                failures,
                f"{label} does not contain {expected_arch}: {sorted(architectures)}",
            )
        extras = sorted(architectures - {expected_arch})
        if extras:
            # Universal system libraries are acceptable only when the target
            # architecture is present. We do not advertise a universal build.
            extra_architectures[label] = extras
    return {
        "checked_files": checked,
        "expected_architecture": expected_arch,
        "architectures": details,
        "additional_architectures_present": extra_architectures,
    }


def verify_dynamic_libraries(bundle: Path, uninstaller: Path, failures: list[str]) -> dict[str, Any]:
    binaries = [uninstaller, *all_macho_files(bundle)]
    bundle_files = {path.name: path for path in bundle.rglob("*") if path.is_file()}
    seen: set[str] = set()
    dependencies_by_binary: dict[str, list[str]] = {}
    system_dependencies: set[str] = set()
    dependency_count = 0
    for binary in binaries:
        try:
            dependencies = runtime_dependencies(binary)
        except RuntimeError as exc:
            add_failure(failures, f"otool failed for {binary}: {exc}")
            continue
        label = str(binary.relative_to(bundle)) if bundle in binary.parents else binary.name
        dependencies_by_binary[label] = dependencies
        for dependency in dependencies:
            key = f"{binary}:{dependency}"
            if key in seen:
                continue
            seen.add(key)
            dependency_count += 1
            if re.match(r"^[A-Za-z]:[\\/]", dependency) or "Program Files" in dependency:
                add_failure(failures, f"Windows path in dynamic dependencies: {dependency}")
                continue
            if dependency.startswith("@") and not dependency_exists(
                dependency, binary, bundle, bundle_files
            ):
                add_failure(failures, f"Unresolved bundled dynamic library: {dependency}")
                continue
            if dependency.startswith("/"):
                if is_system_shared_cache_dependency(dependency):
                    system_dependencies.add(dependency)
                elif not Path(dependency).exists():
                    add_failure(failures, f"Missing dynamic library: {dependency}")
    return {
        "binaries_checked": len(binaries),
        "dependencies_checked": dependency_count,
        "system_shared_cache_dependency_count": len(system_dependencies),
        "system_shared_cache_dependencies": sorted(system_dependencies),
        "locations": dependencies_by_binary,
    }


def verify_no_windows_artifacts(bundle: Path, failures: list[str]) -> dict[str, Any]:
    forbidden_suffixes = {".dll", ".exe", ".pyd", ".cmd", ".bat", ".msi"}
    found: list[str] = []
    files = [path for path in bundle.rglob("*") if path.is_file()]
    for path in files:
        if path.suffix.lower() in forbidden_suffixes:
            found.append(str(path.relative_to(bundle)))
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(2) == b"MZ":
                    found.append(f"{path.relative_to(bundle)} (PE/COFF executable)")
        except OSError:
            continue
    if found:
        add_failure(failures, f"Windows-only artifacts in .app: {', '.join(sorted(set(found)))}")
    return {"files_scanned": len(files), "windows_artifacts": found}


def verify_dmg(dmg: Path, failures: list[str]) -> dict[str, Any]:
    if not dmg.is_file() or dmg.stat().st_size < 1_000_000:
        add_failure(failures, f"DMG is missing or implausibly small: {dmg}")
        return {}
    with tempfile.TemporaryDirectory(prefix="anixo-dmg-") as temporary:
        mount = Path(temporary) / "mount"
        mount.mkdir()
        attach = run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint",
                str(mount),
                str(dmg),
            ]
        )
        try:
            if attach.returncode != 0:
                add_failure(failures, f"DMG could not be mounted: {attach.stderr.strip()}")
                return {"size": dmg.stat().st_size}
            mounted_app = mount / "ANIXO.app"
            if not mounted_app.is_dir():
                names = sorted(path.name for path in mount.iterdir())
                add_failure(failures, f"DMG does not contain ANIXO.app: {names}")
                return {"size": dmg.stat().st_size, "contents": names}
            mounted_plist = mounted_app / "Contents" / "Info.plist"
            identifier = None
            if not mounted_plist.is_file():
                add_failure(failures, "DMG app is missing Contents/Info.plist.")
            else:
                try:
                    with mounted_plist.open("rb") as stream:
                        info = plistlib.load(stream)
                    identifier = info.get("CFBundleIdentifier")
                    if identifier != EXPECTED_IDENTIFIER:
                        add_failure(
                            failures,
                            f"DMG app identifier is {identifier!r}; expected {EXPECTED_IDENTIFIER!r}.",
                        )
                except (OSError, plistlib.InvalidFileException) as exc:
                    add_failure(failures, f"DMG Info.plist could not be parsed: {exc}")
            return {
                "size": dmg.stat().st_size,
                "contains_app": True,
                "identifier": identifier,
                "top_level": sorted(path.name for path in mount.iterdir()),
            }
        finally:
            if attach.returncode == 0:
                detach = run(["hdiutil", "detach", "-quiet", str(mount)])
                if detach.returncode != 0:
                    add_failure(failures, f"DMG could not be detached: {detach.stderr.strip()}")


def verify_checksums(
    release_dir: Path, dmg: Path, uninstaller: Path, failures: list[str]
) -> dict[str, Any]:
    checksum_path = release_dir / "SHA256SUMS.txt"
    if not checksum_path.is_file():
        add_failure(failures, f"Missing checksum file: {checksum_path}")
        return {}
    expected_names = {dmg.name, uninstaller.name}
    parsed: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+(.+)$", line.strip())
        if match:
            parsed[match.group(2).strip()] = match.group(1).lower()
    if set(parsed) != expected_names:
        add_failure(
            failures,
            f"SHA256SUMS.txt covers {sorted(parsed)}; expected {sorted(expected_names)}.",
        )
    for name in expected_names:
        path = release_dir / name
        if not path.is_file():
            add_failure(failures, f"Checksum target is missing: {path}")
            continue
        actual = sha256(path)
        if parsed.get(name) != actual:
            add_failure(failures, f"Checksum mismatch for {name}: {parsed.get(name)} != {actual}")
    return {"files": sorted(parsed)}


def verify_signing(
    bundle: Path,
    dmg: Path,
    require_signing: bool,
    require_notarization: bool,
    failures: list[str],
) -> dict[str, str]:
    app_verify = run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)]
    )
    app_display = run(["codesign", "--display", "--verbose=4", str(bundle)])
    dmg_verify = run(["codesign", "--verify", "--strict", "--verbose=2", str(dmg)])
    dmg_display = run(["codesign", "--display", "--verbose=4", str(dmg)])
    signed = (
        app_verify.returncode == 0
        and "Authority=" in app_display.stdout
        and dmg_verify.returncode == 0
        and "Authority=" in dmg_display.stdout
    )

    if require_signing:
        if not signed:
            add_failure(
                failures,
                "Requested code signing was not verified on both ANIXO.app and the DMG: "
                f"{(app_verify.stderr or dmg_verify.stderr).strip()}",
            )
        signing_status = STATUS_VERIFIED if signed else STATUS_FAILED
    else:
        signing_status = STATUS_UNVERIFIED

    if require_notarization:
        stapler = run(["xcrun", "stapler", "validate", str(dmg)])
        notarization_status = STATUS_VERIFIED if stapler.returncode == 0 else STATUS_FAILED
        if stapler.returncode != 0:
            add_failure(
                failures,
                f"Requested notarization was not verified on the DMG: {stapler.stderr.strip()}",
            )
    else:
        notarization_status = STATUS_UNVERIFIED

    detail = "\n".join(
        part.strip()
        for part in (app_verify.stderr, dmg_verify.stderr, app_display.stdout, dmg_display.stdout)
        if part.strip()
    )
    return {
        "signing": signing_status,
        "notarization": notarization_status,
        "detail": detail[:1500],
    }


def write_reports(report: dict[str, Any], stem: Path) -> None:
    json_path = stem.with_suffix(".json")
    markdown_path = stem.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = ["# ANIXO macOS bundle validation", ""]
    for key, value in report["statuses"].items():
        lines.append(f"- **{key}:** {value}")
    lines.extend(["", "## Details", ""])
    for key, value in report["details"].items():
        lines.append(f"- {key}: `{json.dumps(value, ensure_ascii=False)}`")
    if report["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        lines.extend(f"- {failure}" for failure in report["failures"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {json_path} and {markdown_path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dmg", type=Path, required=True)
    parser.add_argument("--uninstaller", type=Path, required=True)
    parser.add_argument("--expected-arch", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--arch-label", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--report", type=Path, required=True, help="Report path stem, without extension")
    parser.add_argument("--build-passed", action="store_true")
    parser.add_argument("--tests-passed", action="store_true")
    parser.add_argument("--require-signing", action="store_true")
    parser.add_argument("--require-notarization", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.platform != "darwin":
        print("This validator must run on macOS.", file=sys.stderr)
        return 2

    for tool in ("lipo", "otool", "plutil", "hdiutil", "codesign", "shasum"):
        if shutil.which(tool) is None:
            print(f"Required macOS tool is missing: {tool}", file=sys.stderr)
            return 2

    failures: list[str] = []
    warnings: list[str] = []
    bundle = args.bundle.resolve()
    dmg = args.dmg.resolve()
    uninstaller = args.uninstaller.resolve()
    release_dir = dmg.parent

    for path in (bundle, dmg, uninstaller):
        if not path.exists():
            add_failure(failures, f"Expected artifact is missing: {path}")

    bundle_details: dict[str, Any] = {}
    architecture: dict[str, Any] = {}
    libraries: dict[str, Any] = {}
    windows: dict[str, Any] = {}
    dmg_details: dict[str, Any] = {}
    checksums: dict[str, Any] = {}
    signing: dict[str, str] = {
        "signing": STATUS_UNVERIFIED,
        "notarization": STATUS_UNVERIFIED,
    }

    if bundle.is_dir():
        architecture_start = len(failures)
        architecture = verify_architectures(bundle, uninstaller, args.expected_arch, failures)
        architecture_status = STATUS_VERIFIED if len(failures) == architecture_start else STATUS_FAILED

        packaging_start = len(failures)
        bundle_details = verify_bundle_structure(bundle, args.version, failures)
        libraries = verify_dynamic_libraries(bundle, uninstaller, failures)
        windows = verify_no_windows_artifacts(bundle, failures)
    else:
        architecture_status = STATUS_FAILED

    if dmg.exists():
        dmg_details = verify_dmg(dmg, failures)
        checksums = verify_checksums(release_dir, dmg, uninstaller, failures)

    if uninstaller.is_file() and not os.access(uninstaller, os.X_OK):
        add_failure(failures, f"Uninstaller is not executable: {uninstaller}")
    if uninstaller.is_file():
        for flag in ("--version", "--help"):
            completed = run([str(uninstaller), flag])
            if completed.returncode != 0:
                add_failure(failures, f"Uninstaller {flag} exited {completed.returncode}")

    if dmg.exists() and bundle.is_dir():
        packaging_status = STATUS_VERIFIED if len(failures) == packaging_start else STATUS_FAILED
    else:
        packaging_status = STATUS_FAILED
        if not dmg.exists():
            packaging_start = len(failures)

    signing = verify_signing(
        bundle,
        dmg,
        args.require_signing,
        args.require_notarization,
        failures,
    )

    if not args.require_signing:
        warnings.append("No Apple signing certificate was configured; code signing was not verified.")
    if not args.require_notarization:
        warnings.append("No Apple notary credentials were configured; notarization was not verified.")
    if libraries.get("system_shared_cache_dependency_count"):
        warnings.append(
            "Dependencies under /usr/lib and /System/Library were accepted as macOS "
            "dyld shared-cache libraries; their absence from disk is expected on macOS 13+."
        )

    statuses = {
        "Build successful": STATUS_VERIFIED if args.build_passed else STATUS_UNVERIFIED,
        "Automated tests successful": STATUS_VERIFIED if args.tests_passed else STATUS_UNVERIFIED,
        "Architecture verified": architecture_status,
        "Packaging verified": packaging_status,
        "Code signing verified": signing.get("signing", STATUS_UNVERIFIED),
        "Notarization verified": signing.get("notarization", STATUS_UNVERIFIED),
        "Real Finder launch verified": STATUS_UNTESTED,
        "Real Gatekeeper installation verified": STATUS_UNTESTED,
        "Real playback verified": STATUS_UNTESTED,
    }
    report = {
        "platform": "Darwin",
        "architecture": args.expected_arch,
        "architecture_label": args.arch_label,
        "version": args.version,
        "statuses": statuses,
        "details": {
            "bundle": bundle_details,
            "architecture_check": architecture,
            "dynamic_libraries": libraries,
            "windows_artifact_scan": windows,
            "dmg": dmg_details,
            "checksums": checksums,
            "signing": signing,
        },
        "warnings": warnings,
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_reports(report, args.report)

    for key, value in statuses.items():
        print(f"{key}: {value}", flush=True)
    if failures:
        print(f"MACOS BUNDLE VALIDATION FAILED ({len(failures)} issue(s))", file=sys.stderr)
        return 1
    print("MACOS BUNDLE VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
