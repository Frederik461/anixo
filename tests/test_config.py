from __future__ import annotations

import json
from pathlib import Path

from config import Config, ConfigStore


def test_default_renderer_prefers_capability_detection() -> None:
    assert Config().artwork_renderer == "auto"


def test_legacy_auto_renderer_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"images": "auto", "artworkRenderer": "auto"}),
        encoding="utf-8",
    )
    store = ConfigStore(path=path, data_dir=tmp_path)

    assert store.load().artwork_renderer == "auto"
    assert store.load().artwork_renderer == "auto"


def test_generated_halfcell_default_is_upgraded_once(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"images": "auto", "artwork_renderer": "halfcell"}),
        encoding="utf-8",
    )
    store = ConfigStore(path=path, data_dir=tmp_path)

    assert store.load().artwork_renderer == "auto"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["renderer_auto_upgraded"] is True

    raw["artwork_renderer"] = "halfcell"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert ConfigStore(path=path, data_dir=tmp_path).load().artwork_renderer == "halfcell"


def test_legacy_config_keys_are_recognized() -> None:
    config = Config.from_mapping(
        {
            "theme": "aurora",
            "accent": "#5eead4",
            "animations": False,
            "images": "auto",
            "artworkRenderer": "ansi",
            "aniCliPath": "C:\\ani-cli\\ani-cli.cmd",
            "mpvPath": "C:\\mpv\\mpv.exe",
            "gitBashPath": "C:\\Git\\bin\\bash.exe",
            "metadataEndpoint": "https://example.test/graphql",
            "metadataCacheTtlHours": 999,
            "watchlistPath": "",
            "historyPath": "",
            "cachePath": "",
        }
    )

    # Legacy "ansi" is normalized to the capability-driven default; the
    # poster layer still understands an explicit "ansi" value.
    assert config.artwork_renderer == "auto"
    assert config.metadata_endpoint == "https://example.test/graphql"
    assert config.metadata_cache_ttl_hours == 168
    assert config.mpv_path.endswith("mpv.exe")
    assert config.git_bash_path.endswith("bash.exe")
    assert config.accent == "#5EEAD4"
    assert config.animations is False


def test_corrupt_config_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")

    store = ConfigStore(path=path, data_dir=tmp_path)
    config = store.load()

    assert config.theme == "midnight"
    assert any(
        item.name.startswith("config.json.corrupt-") for item in tmp_path.iterdir()
    )


def test_preferred_audio_normalizes_and_round_trips(tmp_path: Path) -> None:
    config = Config(preferredAudio="DUB")
    assert config.preferred_audio == "dub"

    config.preferred_audio = "unexpected"
    assert config.preferred_audio == "sub"

    path = tmp_path / "config.json"
    store = ConfigStore(path=path, data_dir=tmp_path)
    store.save(Config(preferred_audio="dub"))

    assert json.loads(path.read_text(encoding="utf-8"))["preferred_audio"] == "dub"
    assert store.load().preferred_audio == "dub"


def test_config_round_trip_preserves_aliases(tmp_path: Path) -> None:
    store = ConfigStore(path=tmp_path / "config.json", data_dir=tmp_path)
    config = Config(ani_cli_history_path="C:\\history\\ani-hsts", artwork_renderer="off")
    store.save(config)
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))

    assert raw["ani_cli_history_path"] == "C:\\history\\ani-hsts"
    assert raw["artwork_renderer"] == "off"
    assert store.load().artwork_renderer == "off"
