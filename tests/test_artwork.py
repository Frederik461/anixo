from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image

from services.artwork import ArtworkService
from services.image_cache import CacheKey, ImageCache

URL = "https://example.test/cover.jpg"


def make_png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (140, 90, 210)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def service(tmp_path: Path) -> ArtworkService:
    return ArtworkService(ImageCache(tmp_path / "cache"))


def test_download_is_shared_and_image_is_only_processed_once(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(600, 900)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    async def run() -> None:
        first, second = await asyncio.gather(
            service.processed_image(URL, 14, 11, renderer="halfcell", cell_size=(10, 20)),
            service.processed_image(URL, 14, 11, renderer="halfcell", cell_size=(10, 20)),
        )
        assert first is not None and second is not None
        assert first.path == second.path
        assert service.download_count == 1
        assert service.process_count == 1

    asyncio.run(run())
    key = CacheKey(URL, 14, 11, "halfcell:10x20")
    processed_path = service.cache.get_processed(key)
    assert processed_path is not None
    with Image.open(processed_path) as image:
        # 14 columns, 11 half-cell rows: a 14x22 raster at 2:3 source ratio.
        assert image.size == (14, 22)


def test_processed_size_follows_source_aspect(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(600, 600)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    result = asyncio.run(
        service.processed_image(
            URL, 14, 11, renderer="halfcell", cell_size=(10, 20)
        )
    )

    assert result is not None
    assert result.cell_rows == 7
    with Image.open(result.path) as image:
        assert image.size == (14, 14)


def test_different_dimensions_do_not_reuse_a_processed_image(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(600, 900)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    async def run() -> None:
        small = await service.processed_image(URL, 14, 11, renderer="halfcell", cell_size=(10, 20))
        large = await service.processed_image(URL, 20, 15, renderer="halfcell", cell_size=(10, 20))
        assert small and large
        assert small.path != large.path
        assert service.download_count == 1
        assert service.process_count == 2

    asyncio.run(run())


def test_failed_download_does_not_corrupt_cache(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_download(self: ArtworkService, url: str) -> bytes | None:
        return None

    monkeypatch.setattr(ArtworkService, "_download", failed_download)

    result = asyncio.run(service.processed_image(URL, 14, 21))

    assert result is None
    assert service.cache.counts() == {"original": 0, "processed": 0}


def test_invalidation_marks_results_with_the_new_generation(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(30, 45)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)
    service.invalidate()

    result = asyncio.run(service.processed_image(URL, 14, 21))

    assert result is not None
    assert result.generation == service.generation


def test_sixel_raster_uses_terminal_cell_size(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(600, 900)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    result = asyncio.run(
        service.processed_image(
            URL, 16, 12, renderer="sixel", cell_size=(10, 20)
        )
    )

    assert result is not None
    assert result.render_width == 16
    assert result.cell_rows == 12
    assert result.pixel_size == (160, 240)
    assert result.upscaled is False
    with Image.open(result.path) as image:
        assert image.size == (160, 240)
    key = CacheKey(URL, 16, 12, "sixel:10x20")
    assert service.cache.get_processed(key) == result.path


def test_original_is_used_directly_when_no_resize_is_needed(
    service: ArtworkService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return make_png(80, 120)

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    result = asyncio.run(
        service.processed_image(
            URL, 16, 12, renderer="sixel", cell_size=(10, 20)
        )
    )

    assert result is not None
    assert result.source_size == (80, 120)
    assert result.pixel_size == (80, 120)
    assert result.path == result.source_path
    assert result.upscaled is False
    assert service.process_count == 0
    assert service.cache.counts() == {"original": 1, "processed": 0}
    with Image.open(result.path) as image:
        assert image.size == (80, 120)


@pytest.mark.parametrize(
    ("image_format", "extension"),
    (("JPEG", "jpeg"), ("WEBP", "webp")),
)
def test_jpeg_and_webp_originals_are_preserved(
    service: ArtworkService,
    monkeypatch: pytest.MonkeyPatch,
    image_format: str,
    extension: str,
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), (40, 120, 180)).save(buffer, format=image_format)
    payload = buffer.getvalue()

    async def fake_download(self: ArtworkService, url: str) -> bytes | None:
        return payload

    monkeypatch.setattr(ArtworkService, "_download", fake_download)

    result = asyncio.run(
        service.processed_image(
            URL, 12, 9, renderer="sixel", cell_size=(10, 20)
        )
    )

    assert result is not None
    assert result.source_path.suffix == f".{extension}"
    assert result.source_size == (600, 900)
    with Image.open(result.source_path) as image:
        assert image.format == image_format
        assert image.size == (600, 900)
    with Image.open(result.path) as image:
        assert image.size == (120, 180)
