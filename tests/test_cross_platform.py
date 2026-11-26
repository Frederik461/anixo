from __future__ import annotations

from pathlib import Path

import pytest

from platforms import MACOS, WINDOWS, PlatformAdapter, get_installed_platform_id


def _clean_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for name in (
        "ANIXO_DATA_DIR",
        "ANIME_HUB_DATA_DIR",
        "ANIXO_CONFIG",
        "ANIME_HUB_CONFIG",
        "ANIXO_CACHE_DIR",
        "ANIXO_LOG_DIR",
        "ANIXO_ANI_CLI_DIR",
        "APPDATA",
        "LOCALAPPDATA",
        "GIT_INSTALL_ROOT",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HOME",
        "USERPROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_platform_ids_and_display_names() -> None:
    assert PlatformAdapter(WINDOWS).id == "windows"
    assert PlatformAdapter(MACOS).id == "macos"
    assert PlatformAdapter("Linux").display_name == "Linux"
    assert get_installed_platform_id("Darwin") == "macos"


def test_env_overrides_win_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    custom = tmp_path / "custom"
    monkeypatch.setenv("ANIXO_DATA_DIR", str(custom / "data"))
    monkeypatch.setenv("ANIXO_CONFIG", str(custom / "config.json"))
    monkeypatch.setenv("ANIXO_CACHE_DIR", str(custom / "cache"))
    monkeypatch.setenv("ANIXO_LOG_DIR", str(custom / "logs"))

    adapter = PlatformAdapter("Linux")
    assert adapter.data_dir() == (custom / "data").resolve()
    assert adapter.config_path() == (custom / "config.json").resolve()
    assert adapter.cache_dir() == (custom / "cache").resolve()
    assert adapter.log_dir() == (custom / "logs").resolve()


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_data_dir_uses_platform_roots(
    system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    adapter = PlatformAdapter(system)
    expected = {
        "Windows": tmp_path / "roaming" / "ANIXO",
        "Darwin": home / "Library" / "Application Support" / "ANIXO",
        "Linux": home / ".config" / "ANIXO",
    }[system]
    assert adapter.data_dir() == expected


def test_windows_state_cache_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    adapter = PlatformAdapter(WINDOWS)
    root = tmp_path / "local" / "ANIXO"
    assert adapter.state_dir() == root
    assert adapter.cache_dir() == root / "cache"
    assert adapter.log_dir() == root / "logs"


def test_macos_state_cache_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    adapter = PlatformAdapter(MACOS)
    support = home / "Library" / "Application Support" / "ANIXO"
    assert adapter.state_dir() == support
    assert adapter.cache_dir() == home / "Library" / "Caches" / "ANIXO"
    assert adapter.log_dir() == home / "Library" / "Logs" / "ANIXO"


def test_linux_xdg_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    adapter = PlatformAdapter("Linux")
    assert adapter.data_dir() == tmp_path / "xdg-config" / "ANIXO"
    assert adapter.cache_dir() == tmp_path / "xdg-cache" / "ANIXO"
    assert adapter.log_dir() == tmp_path / "xdg-state" / "ANIXO" / "log"


def test_path_conversion_and_shell_helpers(tmp_path: Path) -> None:
    windows = PlatformAdapter(WINDOWS)
    assert windows.to_shell_path(r"C:\Users\ANIXO\tools") == "/c/Users/ANIXO/tools"
    assert windows.supports_cmd_wrappers() is True
    assert windows.open_command(tmp_path / "file") is None

    macos = PlatformAdapter(MACOS)
    assert macos.to_shell_path("/Users/anixo/tools") == "/Users/anixo/tools"
    assert macos.supports_cmd_wrappers() is False
    assert macos.open_command(tmp_path / "file") == ("open", str(tmp_path / "file"))


def test_expected_install_roots_and_portable_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clean_environment(monkeypatch, home)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    adapter = PlatformAdapter(WINDOWS)
    standard = tmp_path / "local" / "Programs" / "ANIXO"
    assert adapter.is_expected_install_root(standard) is True
    assert adapter.is_expected_install_root(tmp_path / "random") is False
    portable = home / "USB" / "ANIXO"
    assert adapter.is_expected_install_root(portable, portable=True) is True
    assert adapter.installer_suffixes() == (".exe",)
    assert PlatformAdapter(MACOS).installer_suffixes()[0] == ".dmg"
