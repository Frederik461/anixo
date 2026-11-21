"""Standalone ANIXO uninstaller.

This module is usable from source and is packaged as a separate console
executable.  It reads the persistent installation manifest and removes only
components positively recorded as ANIXO-owned.  User data is preserved unless
``--mode complete`` is explicitly selected and confirmed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from config import ConfigStore, default_cache_dir, default_data_dir, default_log_dir
from platforms import get_platform_adapter
from services.install_manifest import ManifestStore
from services.uninstall import (
    UninstallMode,
    UninstallPlanner,
    UninstallSafetyError,
    read_pending,
    wait_for_pid,
)
from version import APP_NAME, VERSION

_CATEGORIES = ("app", "dependencies", "cache", "settings")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ANIXO-Uninstaller",
        description=f"Remove {APP_NAME} components recorded in its installation manifest.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument(
        "--mode",
        choices=("app-only", "complete"),
        default="app-only",
        help="app-only keeps settings and personal data; complete removes them",
    )
    parser.add_argument("--data-dir", type=Path, help="override the ANIXO data directory")
    parser.add_argument(
        "--manifest-dir", type=Path, help="alias for --data-dir (manifest location)"
    )
    parser.add_argument("--cache-dir", type=Path, help="override the ANIXO cache directory")
    parser.add_argument("--log-dir", type=Path, help="override the ANIXO log directory")
    parser.add_argument("--config", type=Path, help="override the ANIXO config file path")
    parser.add_argument(
        "--include",
        choices=_CATEGORIES,
        action="append",
        help="include a category (default: all applicable categories)",
    )
    parser.add_argument(
        "--exclude",
        choices=_CATEGORIES,
        action="append",
        help="exclude a category from the plan",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    parser.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--execute-pending",
        action="store_true",
        help="read a pending request written by the in-app uninstall UI",
    )
    parser.add_argument(
        "--wait-for-pid",
        type=int,
        help="wait for a process to exit before removing program files",
    )
    return parser


def _directories(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    data_dir = Path(args.data_dir or args.manifest_dir or default_data_dir()).expanduser().resolve()
    cache_dir = (
        Path(args.cache_dir).expanduser().resolve()
        if args.cache_dir
        else (data_dir / "cache" if args.data_dir or args.manifest_dir else default_cache_dir())
    )
    log_dir = (
        Path(args.log_dir).expanduser().resolve()
        if args.log_dir
        else (data_dir / "logs" if args.data_dir or args.manifest_dir else default_log_dir())
    )
    return data_dir, cache_dir, log_dir


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _print_plan(plan: object) -> None:
    print(f"{APP_NAME} uninstall plan")
    print(f"  mode:       {plan.mode}")  # type: ignore[attr-defined]
    print(f"  platform:   {plan.platform}")  # type: ignore[attr-defined]
    print(f"  manifest:   {plan.manifest_path}")  # type: ignore[attr-defined]
    print(f"  app version:{plan.app_version}")  # type: ignore[attr-defined]
    print()
    for category in _CATEGORIES:
        items = [item for item in plan.items if item.category == category]  # type: ignore[attr-defined]
        if not items:
            continue
        print(f"[{category}]")
        for item in items:
            flag = "x" if item.selected else " "
            size = f" ({_human_size(item.size)})" if item.size else ""
            print(f"  [{flag}] {item.label}{size}")
            if item.warning:
                print(f"        ! {item.warning}")
        print()
    if plan.warnings:  # type: ignore[attr-defined]
        print("Warnings:")
        for warning in plan.warnings:  # type: ignore[attr-defined]
            print(f"  - {warning}")
        print()
    print(f"Selected size: {_human_size(plan.total_size())}")  # type: ignore[attr-defined]


def _selections(args: argparse.Namespace, pending: dict | None) -> dict[str, bool]:
    defaults = {category: True for category in _CATEGORIES}
    defaults["settings"] = args.mode == "complete"
    source = (pending or {}).get("selections", {})
    if isinstance(source, dict):
        for key, value in source.items():
            category = str(key).removeprefix("include_")
            if category in defaults and isinstance(value, bool):
                defaults[category] = value
    if args.include:
        for category in args.include:
            defaults[category] = True
    if args.exclude:
        for category in args.exclude:
            defaults[category] = False
    return defaults


def _confirm(mode: str, selected: dict[str, bool]) -> bool:
    if mode == "complete":
        print("Complete removal permanently deletes settings, history, watchlist,")
        print("progress, API configuration, caches, and application data.")
        try:
            return input("Type DELETE to continue: ").strip() == "DELETE"
        except EOFError:
            return False
    try:
        answer = input("Proceed with removal? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pending: dict | None = None
    if args.execute_pending:
        lookup_dir = Path(
            args.data_dir or args.manifest_dir or default_data_dir()
        ).expanduser().resolve()
        try:
            pending = read_pending(lookup_dir)
        except UninstallSafetyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        args.mode = "complete" if pending["mode"] == "complete" else "app-only"
        for flag, key in (
            ("data_dir", "data_dir"),
            ("cache_dir", "cache_dir"),
            ("log_dir", "log_dir"),
            ("config", "config_path"),
        ):
            if getattr(args, flag) is None and pending.get(key):
                setattr(args, flag, Path(str(pending[key])))

    data_dir, cache_dir, log_dir = _directories(args)
    config_path = (
        Path(args.config).expanduser().resolve() if args.config else data_dir / "config.json"
    )
    adapter = get_platform_adapter()

    wait_pid = args.wait_for_pid
    if pending and pending.get("wait_for_pid") and wait_pid is None:
        try:
            wait_pid = int(pending["wait_for_pid"])
        except (TypeError, ValueError):
            wait_pid = None
    if wait_pid:
        wait_for_pid(wait_pid)

    mode: UninstallMode = "complete" if args.mode == "complete" else "app_only"
    selections = _selections(args, pending)
    store = ManifestStore(data_dir, adapter)
    planner = UninstallPlanner(
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        store=store,
        adapter=adapter,
        config_store=ConfigStore(path=config_path, data_dir=data_dir),
    )
    try:
        plan = planner.plan(
            mode,
            include_app=selections["app"],
            include_dependencies=selections["dependencies"],
            include_cache=selections["cache"],
            include_settings=selections["settings"],
        )
    except UninstallSafetyError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "Manual cleanup guidance is in INSTALLATION.md. "
            "Portable installations can be removed by deleting their folder.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": plan.mode,
                        "platform": plan.platform,
                        "items": [
                            {
                                "key": item.key,
                                "label": item.label,
                                "category": item.category,
                                "kind": item.kind,
                                "target": item.target,
                                "size": item.size,
                                "selected": item.selected,
                            }
                            for item in plan.items
                        ],
                        "warnings": list(plan.warnings),
                        "total_size": plan.total_size(),
                    },
                    indent=2,
                )
            )
        else:
            _print_plan(plan)
            print("Dry run only; nothing was removed.")
        return 0

    if not args.json:
        _print_plan(plan)
    if not args.yes and not _confirm(mode, selections):
        print("Uninstall cancelled; nothing was removed.")
        return 130

    def report_progress(completed: int, total: int, item: object, error: str) -> None:
        if args.json:
            return
        state = "FAILED" if error else "done"
        print(f"  [{completed}/{total}] {state}: {item.describe()}")  # type: ignore[attr-defined]
        if error:
            print(f"          reason: {error}")

    if not args.json:
        print("\nRemoving selected components:")
    report = planner.execute(plan, progress=report_progress)
    if args.json:
        print(json.dumps(report.to_mapping(), indent=2))
    else:
        print("\nCompletion report")
        print(f"  status:  {'success' if report.success else 'finished with failures'}")
        print(f"  removed: {len(report.removed)} item(s)")
        for item in report.removed:
            print(f"    - {item}")
        if report.kept:
            print(f"  kept:    {len(report.kept)} item(s)")
            for item, reason in report.kept:
                print(f"    - {item} ({reason})")
        print(f"  failed removals: {len(report.failed)}")
        for item, reason in report.failed:
            print(f"    - {item} ({reason})")
        print("  manual cleanup:")
        steps = report.manual_steps()
        if steps:
            for step in steps:
                print(f"    - {step}")
        else:
            print("    - None. No further action is required.")
        if report.log_path:
            print(f"  log:     {report.log_path}")
        print("Uninstall complete." if report.success else "Uninstall finished with failures.")
    return 0 if report.success else 1


if __name__ == "__main__":  # pragma: no cover - exercised through tests via main()
    raise SystemExit(main())
