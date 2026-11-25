"""ANIXO command-line entry point."""

from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path

from app import AnixoApp
from config import (
    ConfigStore,
    default_cache_dir,
    default_config_path,
    default_data_dir,
    default_log_dir,
    expand_path,
)
from platforms import get_platform_adapter
from services.ani_cli import AniCliService
from services.dependency_manager import DependencyManager
from services.diagnostics import build_report
from services.image_cache import ImageCache
from services.install_manifest import ManifestStore
from services.terminal import detect_capabilities
from version import APP_DESCRIPTION, APP_NAME, APP_TAGLINE, VERSION

__version__ = VERSION

_EPILOG = """examples:
  Anixo                           Launch ANIXO with normal user data
  Anixo --diagnose                Print dependency and terminal diagnostics
  Anixo --no-images               Start with poster downloads disabled
  Anixo --config /path/config.json
  Anixo --data-dir /path/ANIXO
  Anixo --migrate-from /path/to/legacy/data
  Anixo --record-install --install-dir /path/to/ANIXO
  Anixo release beta               Publish the next beta through GitHub Actions
  Anixo release stable             Convert the current beta to a stable release
  Anixo release check [tag]        Resume workflow/asset verification
  Anixo release history [limit]    Show local tags and GitHub Releases
"""


def _write_uninstall_paths(
    install_dir: Path, data_dir: Path, cache_dir: Path, log_dir: Path
) -> Path:
    """Record effective data paths beside the app for OS-level uninstall.

    Inno Setup's uninstall step runs without the installer's command-line
    arguments, so it cannot infer a custom ``--data-dir``.  This small INI file
    lets the bundled uninstall wrapper reproduce the install-time paths without
    writing anything outside the installation folder.
    """
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser["Paths"] = {
        "Data": str(data_dir.resolve()),
        "Cache": str(cache_dir.resolve()),
        "Log": str(log_dir.resolve()),
    }
    path = install_dir / "uninstall-paths.ini"
    with path.open("w", encoding="utf-8") as stream:
        parser.write(stream, space_around_delimiters=False)
    return path


def _set_console_title(title: str) -> None:
    """Set the Windows console title; harmless when no console is attached."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Anixo",
        description=f"{APP_NAME} — {APP_DESCRIPTION} {APP_TAGLINE}",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="store_true", help="Print version and exit.")
    parser.add_argument(
        "--diagnose",
        "--diagnostics",
        action="store_true",
        help="Print a dependency report and exit.",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Disable artwork downloads for this session.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the first-launch setup wizard even if setup is complete.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Use a specific config.json (equivalent to ANIXO_CONFIG).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory for the SQLite database and user data.",
    )
    parser.add_argument(
        "--migrate-from",
        type=Path,
        default=None,
        metavar="DIR",
        help="Legacy data directory to import once.",
    )
    parser.add_argument(
        "--record-install",
        action="store_true",
        help="Write the installation manifest and exit (used by installers).",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Program directory recorded by --record-install.",
    )
    parser.add_argument(
        "--install-kind",
        default="",
        metavar="KIND",
        help="Installation kind recorded by --record-install.",
    )

    commands = parser.add_subparsers(dest="command")
    release = commands.add_parser(
        "release",
        help="Bump versions and publish through the existing GitHub Actions workflow.",
        description="Manage ANIXO releases without manual version/tag edits.",
    )
    release_actions = release.add_subparsers(dest="release_command", required=True)

    def _add_timeout(action_parser: argparse.ArgumentParser) -> None:
        action_parser.add_argument(
            "--timeout-minutes",
            type=int,
            default=None,
            metavar="MINUTES",
            help="Workflow wait timeout (5-180; default from config, usually 45).",
        )

    for action in ("beta", "stable"):
        action_parser = release_actions.add_parser(
            action,
            help=f"Publish the next {action} release.",
        )
        _add_timeout(action_parser)

    check = release_actions.add_parser(
        "check",
        help="Resume checking an existing tag's workflow run and published assets.",
    )
    check.add_argument("tag", nargs="?", default="", metavar="TAG")
    _add_timeout(check)

    history = release_actions.add_parser(
        "history",
        help="List local release tags and GitHub Releases.",
    )
    history.add_argument("limit", nargs="?", type=int, default=10, metavar="COUNT")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0

    data_dir = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    configured_config = (
        os.environ.get("ANIXO_CONFIG")
        or os.environ.get("ANIME_HUB_CONFIG")
        or default_config_path()
    )
    config_path = Path(args.config).resolve() if args.config else Path(configured_config).resolve()
    if args.config is None and not os.environ.get("ANIXO_CONFIG") and not os.environ.get(
        "ANIME_HUB_CONFIG"
    ) and args.data_dir:
        config_path = data_dir / "config.json"

    config = ConfigStore(path=config_path, data_dir=data_dir).load()

    if args.command == "release":
        from services.release_manager import ReleaseManager

        manager = ReleaseManager(
            Path(__file__).resolve().parent,
            config,
            timeout_minutes=getattr(args, "timeout_minutes", None),
        )
        if args.release_command in {"beta", "stable"}:
            return manager.run_release(args.release_command)
        if args.release_command == "check":
            return manager.check_release(args.tag)
        if args.release_command == "history":
            return manager.history(args.limit)
        raise AssertionError("unhandled release command")  # pragma: no cover

    if args.no_images:
        config.images = "off"
    if args.setup:
        config.setup_completed = False

    _set_console_title(f"{APP_NAME} — {APP_TAGLINE}")

    if args.record_install:
        adapter = get_platform_adapter()
        install_dir = (
            Path(args.install_dir).resolve()
            if args.install_dir
            else (
                Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent
            )
        )
        cache_dir = (
            Path(expand_path(config.cache_path)).resolve()
            if config.cache_path
            else (data_dir / "cache" if args.data_dir else default_cache_dir())
        )
        _write_uninstall_paths(install_dir, data_dir, cache_dir, default_log_dir())
        manager = DependencyManager(
            config,
            data_dir,
            cache_dir,
            adapter,
            ManifestStore(data_dir, adapter),
        )
        record = manager.record_app_install(
            install_dir,
            installation_kind=args.install_kind or f"{adapter.id}-installer",
            installer_source="ANIXO installer CLI",
        )
        print(f"Recorded {record.name} {record.version} at {record.install_location}")
        return 0

    if args.diagnose:
        capabilities = detect_capabilities()
        install_dir = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent
        )
        cache_root = (
            Path(expand_path(config.cache_path))
            if config.cache_path
            else default_cache_dir()
        )
        cache_counts = ImageCache(cache_root / "artwork").counts()
        print(
            build_report(
                app_version=VERSION,
                install_dir=install_dir,
                data_dir=data_dir,
                log_dir=default_log_dir(),
                config=config,
                ani_cli=AniCliService(config),
                capabilities=capabilities,
                cache_counts=cache_counts,
            ),
            end="",
        )
        return 0

    legacy_dir = Path(args.migrate_from).resolve() if args.migrate_from else None
    AnixoApp(
        config=config,
        data_dir=data_dir,
        legacy_data_dir=legacy_dir,
        config_path=config_path,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
