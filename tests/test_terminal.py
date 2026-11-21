from __future__ import annotations

from services.terminal import TerminalCapabilities, resolve_renderer


def _caps(*, sixel: bool, tgp: bool, is_tty: bool) -> TerminalCapabilities:
    return TerminalCapabilities(
        is_tty=is_tty,
        sixel=sixel,
        tgp=tgp,
        cell_width=10,
        cell_height=20,
    )


def test_auto_prefers_sixel_even_when_tgp_is_available() -> None:
    renderer, note = resolve_renderer("auto", _caps(sixel=True, tgp=True, is_tty=True))
    assert renderer == "sixel"
    assert note == ""


def test_auto_uses_tgp_only_when_sixel_is_unavailable() -> None:
    renderer, note = resolve_renderer("auto", _caps(sixel=False, tgp=True, is_tty=True))
    assert renderer == "tgp"
    assert note == ""


def test_auto_fallback_is_explicit_on_a_tty() -> None:
    renderer, note = resolve_renderer(
        "auto", _caps(sixel=False, tgp=False, is_tty=True)
    )
    assert renderer == "halfcell"
    assert "Sixel/TGP unavailable" in note
    assert "half-block" in note


def test_auto_fallback_is_explicit_without_a_tty() -> None:
    renderer, note = resolve_renderer(
        "auto", _caps(sixel=False, tgp=False, is_tty=False)
    )
    assert renderer == "unicode"
    assert "Sixel/TGP unavailable" in note
    assert "non-interactive" in note


def test_explicit_sixel_when_unavailable_uses_detected_native_protocol() -> None:
    renderer, note = resolve_renderer("sixel", _caps(sixel=False, tgp=True, is_tty=True))
    assert renderer == "tgp"
    assert "SIXEL unavailable" in note


def test_explicit_sixel_never_silently_becomes_halfcell() -> None:
    renderer, note = resolve_renderer(
        "sixel", _caps(sixel=False, tgp=False, is_tty=True)
    )
    assert renderer == "halfcell"
    assert "SIXEL unavailable" in note
    assert "half-block" in note


def test_manual_low_resolution_override_is_labeled() -> None:
    renderer, note = resolve_renderer("halfcell", _caps(sixel=True, tgp=False, is_tty=True))
    assert renderer == "halfcell"
    assert "Manual override" in note
    assert "SIXEL" in note


def test_off_disables_rendering_without_a_note() -> None:
    renderer, note = resolve_renderer("off", _caps(sixel=True, tgp=True, is_tty=True))
    assert renderer == "off"
    assert note == ""
