from __future__ import annotations

from pathlib import Path

from components.navigation import Selection
from components.poster import Poster
from services.artwork import ArtworkResult

URL = "https://example.test/cover.jpg"


def test_selection_clamps_and_wraps_pages() -> None:
    selection = Selection(page_size=4)
    assert selection.clamp(0) == 0

    assert selection.move(10, 10) == 9
    assert selection.page_start == 8
    assert selection.page_move(1, 10) == 9
    assert selection.move(1, 10) == 9

    assert selection.move(-20, 10) == 0
    assert selection.page_start == 0


def test_old_async_result_cannot_overwrite_newer_poster() -> None:
    poster = Poster(url=URL)
    poster._token = 2
    old = ArtworkResult(
        Path("old.png"), URL, 14, 11, 1, 10, 14, (600, 900), (14, 20),
        Path("old-src.jpg"), False, False,
    )
    new = ArtworkResult(
        Path("new.png"), URL, 14, 11, 2, 10, 14, (600, 900), (14, 20),
        Path("new-src.jpg"), True, False,
    )

    poster._apply_result(1, old)
    assert poster._result is None

    poster._apply_result(2, new)
    assert poster._result is new


def test_selection_change_does_not_change_poster_image() -> None:
    poster = Poster(url=URL, width=14, height=21)
    result = ArtworkResult(
        Path("cover.png"), URL, 14, 11, 1, 10, 14, (600, 900), (14, 20),
        Path("src.jpg"), False, False,
    )
    poster._apply_result(poster._token, result)

    before = poster._result
    poster.fallback_color = "#ff0000"
    assert poster._result is before
