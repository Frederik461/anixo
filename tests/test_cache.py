from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from services.image_cache import CacheKey, ImageCache


def make_png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_original_image_is_never_modified(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path)
    original = make_png(600, 900)

    stored = cache.store_original("https://example.test/cover.jpg", original, "jpeg")
    assert stored.read_bytes() == original

    key = CacheKey("https://example.test/cover.jpg", 14, 21, "halfcell:10x20")
    image = Image.open(io.BytesIO(original)).convert("RGB").resize((14, 42))
    processed = cache.store_processed(key, image)

    assert stored.read_bytes() == original
    with Image.open(processed) as result:
        assert result.size == (14, 42)


def test_different_render_sizes_get_separate_cache_entries(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path)
    url = "https://example.test/cover.jpg"
    small = CacheKey(url, 14, 21, "halfcell:10x20")
    large = CacheKey(url, 20, 30, "sixel:10x20")

    assert cache.processed_path(small) != cache.processed_path(large)

    small_path = cache.store_processed(small, Image.new("RGB", (14, 42)))
    large_path = cache.store_processed(large, Image.new("RGB", (200, 600)))

    with Image.open(small_path) as image:
        assert image.size == (14, 42)
    with Image.open(large_path) as image:
        assert image.size == (200, 600)
    assert cache.counts()["processed"] == 2


def test_cleanup_removes_oldest_processed_entries(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path, max_entries=8)
    url = "https://example.test/cover.jpg"
    for index in range(12):
        key = CacheKey(url, index + 1, 1, "halfcell")
        cache.store_processed(key, Image.new("RGB", (2, 2)))
    assert cache.counts()["processed"] == 12
    assert cache.cleanup() == 4
    assert cache.counts()["processed"] == 8


def test_clear_removes_all_cache_kinds(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path)
    url = "https://example.test/cover.jpg"
    cache.store_original(url, make_png(4, 6), "png")
    cache.store_processed(CacheKey(url, 4, 6, "halfcell"), Image.new("RGB", (4, 12)))
    assert cache.counts() == {"original": 1, "processed": 1}

    cache.clear("processed")
    assert cache.counts() == {"original": 1, "processed": 0}
    cache.clear()
    assert cache.counts() == {"original": 0, "processed": 0}
