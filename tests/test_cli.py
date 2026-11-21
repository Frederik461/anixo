from __future__ import annotations

import json
from pathlib import Path

from main import build_parser, main
from platforms import get_platform_adapter


def test_supported_options_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--no-images",
            "--diagnostics",
            "--config=custom.json",
            "--data-dir",
            "data",
            "--migrate-from",
            "legacy",
        ]
    )

    assert args.no_images is True
    assert args.diagnose is True
    assert args.config == Path("custom.json")
    assert args.data_dir == Path("data")
    assert args.migrate_from == Path("legacy")


def test_installer_options_parse() -> None:
    args = build_parser().parse_args(
        [
            "--record-install",
            "--install-dir",
            "programs",
            "--install-kind",
            "windows-installer",
        ]
    )
    assert args.record_install is True
    assert args.install_dir == Path("programs")
    assert args.install_kind == "windows-installer"


def test_record_install_writes_platform_manifest(tmp_path: Path, capsys) -> None:
    install_dir = tmp_path / "Programs" / "ANIXO"
    install_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"

    code = main(
        [
            "--record-install",
            "--install-dir",
            str(install_dir),
            "--install-kind",
            "test-installer",
            "--data-dir",
            str(data_dir),
            "--config",
            str(tmp_path / "config.json"),
        ]
    )

    assert code == 0
    assert "Recorded ANIXO" in capsys.readouterr().out
    path = data_dir / f"install-manifest-{get_platform_adapter().id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = payload["records"]["app"]
    assert record["install_location"] == str(install_dir.resolve())
    assert record["installed_by_anixo"] is True
    assert payload["installation_kind"] == "test-installer"
    paths = (install_dir / "uninstall-paths.ini").read_text(encoding="utf-8")
    assert f"Data={data_dir.resolve()}" in paths
    assert "Cache=" in paths
    assert "Log=" in paths


def test_version_option_exits_without_starting_app(capsys) -> None:
    assert main(["--version"]) == 0
    assert "ANIXO 0.1.0-beta.1" in capsys.readouterr().out


def test_unknown_option_is_rejected(capsys) -> None:
    try:
        main(["--not-an-option"])
    except SystemExit as error:
        assert error.code == 2
    else:  # pragma: no cover - argparse always raises for unknown options
        raise AssertionError("unknown option was accepted")
    assert "unrecognized arguments" in capsys.readouterr().err
