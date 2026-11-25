#!/usr/bin/env python3
"""Validate packaged ANIXO startup, paths, ownership, and uninstaller on macOS.

These checks exercise the built console binary and standalone uninstaller with
redirected user-data directories. They do not launch a GUI Finder session or
stream video; those remain manual validation items.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

STATUS_VERIFIED = "VERIFIED"
STATUS_FAILED = "FAILED"
STATUS_UNTESTED = "NOT TESTED (headless CI cannot verify)"


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(command, capture_output=True, text=True, env=env, check=False)


def add_failure(failures: list[str], message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    failures.append(message)


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "ANIXO_DATA_DIR",
        "ANIME_HUB_DATA_DIR",
        "ANIXO_CONFIG",
        "ANIME_HUB_CONFIG",
        "ANIXO_CACHE_DIR",
        "ANIXO_LOG_DIR",
        "ANIXO_UPDATE_REPO",
    ):
        env.pop(name, None)
    return env


def install_record(
    key: str,
    name: str,
    path: Path,
    *,
    owned: bool = True,
    config_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "platform": "macos",
        "installed_by_anixo": owned,
        "managed": owned,
        "safe_to_remove": owned,
        "install_location": str(path),
        "version": "0.1.0-beta.2",
        "installed_at": "2026-01-01T00:00:00+00:00",
        "source": "macos-ci-validation",
        "install_method": "test-fixture",
        "expected_sha256": "",
        "observed_sha256": "",
        "license": "",
        "paths": [str(path)],
        "uninstall_commands": [],
        "config_keys": list(config_keys),
        "notes": "",
    }


def prepare_uninstall_case(root: Path, version: str) -> dict[str, Path]:
    data = root / "data"
    cache = root / "cache"
    logs = root / "logs"
    unrelated = root / "unrelated"
    app_root = data / "Applications" / "ANIXO.app"
    managed_dep = cache / "tools" / "ani-cli"
    preexisting_dep = unrelated / "preexisting-mpv"
    data_sentinel = data / "user-data-sentinel.txt"
    outside_sentinel = unrelated / "outside-sentinel.txt"
    config = data / "config.json"

    for directory in (data, cache / "tools", logs, app_root / "Contents", unrelated):
        directory.mkdir(parents=True, exist_ok=True)
    (app_root / "Contents" / "Anixo").write_bytes(b"bundled app")
    managed_dep.write_text("official ani-cli fixture", encoding="utf-8")
    preexisting_dep.write_bytes(b"pre-existing dependency")
    data_sentinel.write_text("history and watchlist sentinel", encoding="utf-8")
    outside_sentinel.write_text("unrelated file sentinel", encoding="utf-8")
    config.write_text(json.dumps({"ani_cli_path": str(managed_dep)}), encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "app_name": "ANIXO",
        "app_version": version,
        "platform": "macos",
        "installation_kind": "macos-dmg",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "records": {
            "app": install_record("app", "ANIXO", app_root),
            "ani-cli": install_record(
                "ani-cli", "ani-cli", managed_dep, config_keys=("ani_cli_path",)
            ),
            "mpv": install_record(
                "mpv", "pre-existing mpv", preexisting_dep, owned=False
            ),
        },
    }
    manifest_path = data / "install-manifest-macos.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "data": data,
        "cache": cache,
        "logs": logs,
        "unrelated": unrelated,
        "app_root": app_root,
        "managed_dep": managed_dep,
        "preexisting_dep": preexisting_dep,
        "data_sentinel": data_sentinel,
        "outside_sentinel": outside_sentinel,
        "config": config,
        "manifest": manifest_path,
    }


def run_uninstaller(
    uninstaller: Path, mode: str, paths: dict[str, Path], env: dict[str, str]
) -> dict[str, Any]:
    command = [
        str(uninstaller),
        "--mode",
        mode,
        "--yes",
        "--json",
        "--data-dir",
        str(paths["data"]),
        "--cache-dir",
        str(paths["cache"]),
        "--log-dir",
        str(paths["logs"]),
        "--config",
        str(paths["config"]),
    ]
    completed = run(command, env)
    if completed.returncode != 0:
        raise RuntimeError(
            f"uninstaller exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return json.loads(completed.stdout)


def write_reports(report: dict[str, Any], stem: Path) -> None:
    json_path = stem.with_suffix(".json")
    markdown_path = stem.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = ["# ANIXO macOS runtime validation", ""]
    for key, value in report["statuses"].items():
        lines.append(f"- **{key}:** {value}")
    lines.extend(["", "## Details", ""])
    for key, value in report["details"].items():
        lines.append(f"- {key}: `{json.dumps(value, ensure_ascii=False)}`")
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        lines.extend(f"- {failure}" for failure in report["failures"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {json_path} and {markdown_path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-binary", type=Path, required=True)
    parser.add_argument("--uninstaller", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-arch", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--report", type=Path, required=True, help="Report path stem, without extension")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.platform != "darwin":
        print("This validator must run on macOS.", file=sys.stderr)
        return 2

    failures: list[str] = []
    app_binary = args.app_binary.resolve()
    uninstaller = args.uninstaller.resolve()
    env = clean_environment()
    details: dict[str, Any] = {}

    if not app_binary.is_file() or not os.access(app_binary, os.X_OK):
        add_failure(failures, f"ANIXO binary is missing or not executable: {app_binary}")
    if not uninstaller.is_file() or not os.access(uninstaller, os.X_OK):
        add_failure(failures, f"Uninstaller is missing or not executable: {uninstaller}")
    if failures:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_reports(
            {
                "platform": "Darwin",
                "architecture": args.expected_arch,
                "statuses": {"Runtime validation": STATUS_FAILED},
                "details": {},
                "warnings": [],
                "failures": failures,
            },
            args.report,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="anixo-macos-runtime-") as temporary:
        root = Path(temporary)
        data = root / "data"
        cache = root / "cache"
        logs = root / "logs"
        config = data / "config.json"
        for directory in (data, cache, logs):
            directory.mkdir(parents=True)
        env.update(
            {
                "ANIXO_DATA_DIR": str(data),
                "ANIXO_CONFIG": str(config),
                "ANIXO_CACHE_DIR": str(cache),
                "ANIXO_LOG_DIR": str(logs),
            }
        )

        startup_start = len(failures)
        diagnose_text = ""
        for flag in ("--version", "--help", "--diagnose"):
            completed = run([str(app_binary), flag], env)
            details[f"startup_{flag.strip('-')}"] = {
                "exit_code": completed.returncode,
                "stdout": completed.stdout[:2000],
                "stderr": completed.stderr[:1000],
            }
            if completed.returncode != 0:
                add_failure(failures, f"ANIXO {flag} exited {completed.returncode}")
            if flag == "--version" and args.version not in completed.stdout:
                add_failure(failures, f"ANIXO --version omitted {args.version}")
            if flag == "--help" and "--diagnose" not in completed.stdout:
                add_failure(failures, "ANIXO --help did not list --diagnose")
            if flag == "--diagnose":
                diagnose_text = completed.stdout
                expected_lines = ("ANIXO DIAGNOSTIC REPORT", "Darwin", args.expected_arch)
                for needle in expected_lines:
                    if needle not in diagnose_text:
                        add_failure(failures, f"ANIXO --diagnose output omitted {needle!r}")
        startup_status = STATUS_VERIFIED if len(failures) == startup_start else STATUS_FAILED

        custom_path_start = len(failures)
        for needle in (str(data), str(logs)):
            if needle not in diagnose_text:
                add_failure(failures, f"ANIXO --diagnose did not honour path override {needle}")
        custom_paths_status = STATUS_VERIFIED if len(failures) == custom_path_start else STATUS_FAILED

        ownership_start = len(failures)
        install_dir = root / "Programs" / "ANIXO"
        install_dir.mkdir(parents=True)
        record = run(
            [
                str(app_binary),
                "--record-install",
                "--install-dir",
                str(install_dir),
                "--install-kind",
                "macos-ci",
            ],
            env,
        )
        details["record_install"] = {
            "exit_code": record.returncode,
            "stdout": record.stdout[:1000],
            "stderr": record.stderr[:1000],
        }
        if record.returncode != 0:
            add_failure(failures, f"ANIXO --record-install exited {record.returncode}")
        manifest_path = data / "install-manifest-macos.json"
        if not manifest_path.is_file():
            add_failure(failures, f"macOS ownership manifest was not created: {manifest_path}")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                app_record = manifest.get("records", {}).get("app", {})
                if manifest.get("platform") != "macos":
                    add_failure(failures, "Ownership manifest platform is not macos")
                if not all(
                    app_record.get(key) for key in ("installed_by_anixo", "managed", "safe_to_remove")
                ):
                    add_failure(failures, "Ownership manifest app record is not managed and safe to remove")
                details["manifest"] = {
                    "platform": manifest.get("platform"),
                    "installation_kind": manifest.get("installation_kind"),
                    "app_record": app_record,
                }
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                add_failure(failures, f"Ownership manifest could not be parsed: {exc}")

        ini_path = install_dir / "uninstall-paths.ini"
        if not ini_path.is_file():
            add_failure(failures, "uninstall-paths.ini was not written beside the app")
        else:
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read(ini_path, encoding="utf-8")
            actual = {
                key: parser.get("Paths", key, fallback="")
                for key in ("Data", "Cache", "Log")
            }
            expected = {"Data": str(data), "Cache": str(cache), "Log": str(logs)}
            details["uninstall_paths"] = {"actual": actual, "expected": expected}
            # macOS resolves /var to /private/var, so compare canonical paths.
            if {key: Path(value).resolve() for key, value in actual.items()} != {
                key: Path(value).resolve() for key, value in expected.items()
            }:
                add_failure(failures, f"Recorded uninstall paths do not match overrides: {actual}")
        ownership_status = STATUS_VERIFIED if len(failures) == ownership_start else STATUS_FAILED

        dependency_start = len(failures)
        if "External dependencies" not in diagnose_text or "ani-cli:" not in diagnose_text:
            add_failure(failures, "Diagnostics did not report external dependency detection")
        dependency_status = STATUS_VERIFIED if len(failures) == dependency_start else STATUS_FAILED

        app_only_root = root / "uninstall-app-only"
        complete_root = root / "uninstall-complete"
        app_only_paths = prepare_uninstall_case(app_only_root, args.version)
        complete_paths = prepare_uninstall_case(complete_root, args.version)

        app_only_start = len(failures)
        try:
            app_only_report = run_uninstaller(uninstaller, "app-only", app_only_paths, env)
            details["app_only_report"] = app_only_report
            if not app_only_report.get("success"):
                add_failure(failures, "App-only uninstaller report was not successful")
            for path_key in ("app_root", "managed_dep", "manifest"):
                if app_only_paths[path_key].exists():
                    add_failure(failures, f"App-only uninstall left {path_key} behind")
            for path_key in ("data", "data_sentinel", "outside_sentinel", "preexisting_dep"):
                if not app_only_paths[path_key].exists():
                    add_failure(failures, f"App-only uninstall removed protected {path_key}")
            if "not installed by ANIXO" not in " ".join(app_only_report.get("warnings", [])):
                add_failure(failures, "App-only uninstall did not warn about a pre-existing dependency")
            if app_only_paths["config"].is_file():
                saved = json.loads(app_only_paths["config"].read_text(encoding="utf-8"))
                if saved.get("ani_cli_path"):
                    add_failure(failures, "App-only uninstall left a stale ani-cli config pointer")
        except (RuntimeError, OSError, json.JSONDecodeError, ValueError) as exc:
            add_failure(failures, f"App-only uninstaller validation failed: {exc}")
        app_only_status = STATUS_VERIFIED if len(failures) == app_only_start else STATUS_FAILED

        complete_start = len(failures)
        try:
            complete_report = run_uninstaller(uninstaller, "complete", complete_paths, env)
            details["complete_report"] = complete_report
            if not complete_report.get("success"):
                add_failure(failures, "Complete uninstaller report was not successful")
            for path_key in ("data", "data_sentinel", "app_root", "managed_dep", "manifest"):
                if complete_paths[path_key].exists():
                    add_failure(failures, f"Complete uninstall left {path_key} behind")
            for path_key in ("outside_sentinel", "preexisting_dep"):
                if not complete_paths[path_key].exists():
                    add_failure(failures, f"Complete uninstall removed protected {path_key}")
        except (RuntimeError, OSError, json.JSONDecodeError, ValueError) as exc:
            add_failure(failures, f"Complete uninstaller validation failed: {exc}")
        complete_status = STATUS_VERIFIED if len(failures) == complete_start else STATUS_FAILED
        protection_status = (
            STATUS_VERIFIED
            if app_only_status == STATUS_VERIFIED and complete_status == STATUS_VERIFIED
            else STATUS_FAILED
        )

    statuses = {
        "Packaged application startup/shutdown": startup_status,
        "Custom data/cache/log paths": custom_paths_status,
        "Installation ownership record": ownership_status,
        "Dependency detection": dependency_status,
        "App-only uninstall preserves user data": app_only_status,
        "Complete uninstall removes personal data": complete_status,
        "Outside sentinels preserved": protection_status,
        "Pre-existing dependency preserved": protection_status,
        "Real Finder launch verified": STATUS_UNTESTED,
        "Real Gatekeeper installation verified": STATUS_UNTESTED,
        "Real playback verified": STATUS_UNTESTED,
    }
    report = {
        "platform": "Darwin",
        "architecture": args.expected_arch,
        "version": args.version,
        "statuses": statuses,
        "details": details,
        "warnings": [],
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_reports(report, args.report)

    for key, value in statuses.items():
        print(f"{key}: {value}", flush=True)
    if failures:
        print(f"MACOS RUNTIME VALIDATION FAILED ({len(failures)} issue(s))", file=sys.stderr)
        return 1
    print("MACOS RUNTIME VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
