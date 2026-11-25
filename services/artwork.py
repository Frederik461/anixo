"""Asynchronous artwork download, validation, processing, and caching."""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from services.image_cache import CacheKey, ImageCache

MAX_ORIGINAL_BYTES = 20 * 1024 * 1024
USER_AGENT = "ANIXO/0.1.0"


@dataclass(frozen=True, slots=True)
class ArtworkResult:
    path: Path
    url: str
    width: int
    height: int
    generation: int
    cell_rows: int
    render_width: int
    source_size: tuple[int, int]
    pixel_size: tuple[int, int]
    source_path: Path
    from_cache: bool
    upscaled: bool


@dataclass(frozen=True, slots=True)
class _Layout:
    render_width: int
    cell_rows: int
    pixel_size: tuple[int, int]
    upscaled: bool


class ArtworkService:
    """Downloads once, processes only for a requested size, and caches forever.

    Originals are immutable and keep their downloaded PNG/JPEG/WebP bytes.
    When the downloaded pixels already match the renderer request, the
    original file is passed through untouched.  Otherwise a processed PNG is
    written with a single high-quality LANCZOS downscale at the exact pixel
    size the renderer will request, so ``textual-image`` never has to rescale
    it.  The layout is capped to the source dimensions, so a small thumbnail
    is never upscaled into a fake high-resolution poster.
    """

    def __init__(
        self,
        cache: ImageCache,
        *,
        timeout_seconds: float = 12.0,
        max_original_bytes: int = MAX_ORIGINAL_BYTES,
    ) -> None:
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.max_original_bytes = max_original_bytes
        self.generation = 0
        self.download_count = 0
        self.process_count = 0
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._process_locks: dict[str, asyncio.Lock] = {}
        self._colors: OrderedDict[str, str] = OrderedDict()
        self._color_limit = 256
        self._lock_limit = 256
        self._image_slots = asyncio.Semaphore(6)

    def _lock(self, store: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
        """Return a shared lock, pruning stale entries to bound memory use."""
        lock = store.get(key)
        if lock is None:
            if len(store) >= self._lock_limit:
                for stale in list(store)[: self._lock_limit // 2]:
                    if not store[stale].locked():
                        del store[stale]
            lock = store.setdefault(key, asyncio.Lock())
        return lock

    def invalidate(self) -> None:
        """Discard results belonging to an older cache generation."""
        self.generation += 1
        self._colors.clear()

    async def clear_cache(self) -> None:
        self.invalidate()
        await asyncio.to_thread(self.cache.clear, "all")
        self._download_locks.clear()
        self._process_locks.clear()

    async def processed_image(
        self,
        url: str,
        width: int,
        height: int,
        *,
        renderer: str = "halfcell",
        cell_size: tuple[int, int] = (10, 20),
    ) -> ArtworkResult | None:
        """Return a cached processed image, downloading/processing if needed."""
        if not url or width <= 0 or height <= 0:
            return None
        generation = self.generation
        renderer_key = f"{renderer}:{cell_size[0]}x{cell_size[1]}"
        key = CacheKey(url=url, width=width, height=height, renderer=renderer_key)

        cached = await asyncio.to_thread(self.cache.get_processed, key)
        if cached:
            return await self._result_from_processed(
                cached, url, width, height, renderer, cell_size, generation, True
            )

        # Cache hits return above. Bound only downloads and PIL work so a large
        # grid cannot saturate the thread pool or the network.
        async with self._image_slots:
            lock = self._lock(self._process_locks, key.digest())
            async with lock:
                cached = await asyncio.to_thread(self.cache.get_processed, key)
                if cached:
                    return await self._result_from_processed(
                        cached, url, width, height, renderer, cell_size, generation, True
                    )

                original = await self._ensure_original(url)
                if original is None or generation != self.generation:
                    return None

                source_size = await asyncio.to_thread(self._source_size, original)
                if source_size is None:
                    return None
                layout = self._layout(width, source_size, renderer, cell_size)

                if layout.pixel_size == source_size:
                    # Already the exact raster the renderer wants; reuse it.
                    return ArtworkResult(
                        path=original,
                        url=url,
                        width=width,
                        height=height,
                        generation=generation,
                        cell_rows=layout.cell_rows,
                        render_width=layout.render_width,
                        source_size=source_size,
                        pixel_size=layout.pixel_size,
                        source_path=original,
                        from_cache=False,
                        upscaled=False,
                    )

                self.process_count += 1
                try:
                    image = await asyncio.to_thread(
                        self._process, original, layout.pixel_size
                    )
                except (OSError, ValueError, UnidentifiedImageError):
                    return None
                if generation != self.generation:
                    return None

                path = await asyncio.to_thread(self.cache.store_processed, key, image)
                await asyncio.to_thread(self.cache.cleanup)
                return ArtworkResult(
                    path=path,
                    url=url,
                    width=width,
                    height=height,
                    generation=generation,
                    cell_rows=layout.cell_rows,
                    render_width=layout.render_width,
                    source_size=source_size,
                    pixel_size=layout.pixel_size,
                    source_path=original,
                    from_cache=False,
                    upscaled=layout.upscaled,
                )

    async def cover_color(self, url: str) -> str:
        """Return a stable average color for the placeholder while art loads."""
        if url in self._colors:
            self._colors.move_to_end(url)
            return self._colors[url]
        original = await self._ensure_original(url)
        if original is None:
            return "#a78bfa"
        try:
            color = await asyncio.to_thread(self._average_color, original)
        except (OSError, UnidentifiedImageError):
            color = "#a78bfa"
        self._colors[url] = color
        self._colors.move_to_end(url)
        while len(self._colors) > self._color_limit:
            self._colors.popitem(last=False)
        return color

    # ------------------------------------------------------------------
    # Internals

    async def _result_from_processed(
        self,
        path: Path,
        url: str,
        width: int,
        height: int,
        renderer: str,
        cell_size: tuple[int, int],
        generation: int,
        from_cache: bool,
    ) -> ArtworkResult | None:
        original = await asyncio.to_thread(self.cache.get_original, url)
        source_path = original or path
        source_size = await asyncio.to_thread(self._source_size, source_path)
        processed_size = await asyncio.to_thread(self._source_size, path)
        if source_size is None or processed_size is None:
            return None

        # A cached processed file may have been written before passthrough was
        # added. Prefer the untouched original whenever the pixels are equal.
        render_path = source_path if original and source_size == processed_size else path
        pixel_width, pixel_height = processed_size
        units_x, units_y = self._units(renderer, cell_size)
        return ArtworkResult(
            path=render_path,
            url=url,
            width=width,
            height=height,
            generation=generation,
            cell_rows=max(1, pixel_height // units_y),
            render_width=max(1, pixel_width // units_x),
            source_size=source_size,
            pixel_size=processed_size,
            source_path=source_path,
            from_cache=from_cache,
            upscaled=(
                processed_size[0] > source_size[0]
                or processed_size[1] > source_size[1]
            ),
        )

    @staticmethod
    def _round_half_up(value: float) -> int:
        """Avoid Python's banker's rounding for half-cell row counts."""
        return max(1, int(value + 0.5))

    @staticmethod
    def _units(renderer: str, cell_size: tuple[int, int]) -> tuple[int, int]:
        """Pixels consumed by one rendered column and one rendered row."""
        if renderer in {"ansi", "halfcell"}:
            return 1, 2
        if renderer == "unicode":
            return 1, 1
        return max(1, cell_size[0]), max(1, cell_size[1])

    @classmethod
    def _layout(
        cls,
        width: int,
        source_size: tuple[int, int],
        renderer: str,
        cell_size: tuple[int, int],
    ) -> _Layout:
        """Derive aspect-correct rows without ever upscaling the source."""
        source_width, source_height = source_size
        cell_width, cell_height = cell_size
        units_x, units_y = cls._units(renderer, cell_size)

        ratio = source_width / max(1, source_height)
        rows_per_column = cell_width / (ratio * cell_height)
        max_width = max(1, source_width // units_x)
        max_rows = max(1, source_height // units_y)

        render_width = min(
            width,
            max_width,
            max(1, int(max_rows / rows_per_column + 1e-9)),
        )
        rows = cls._round_half_up(render_width * rows_per_column)
        while rows > max_rows and render_width > 1:
            render_width -= 1
            rows = cls._round_half_up(render_width * rows_per_column)

        pixel_size = (render_width * units_x, rows * units_y)
        return _Layout(
            render_width=render_width,
            cell_rows=rows,
            pixel_size=pixel_size,
            upscaled=pixel_size[0] > source_width or pixel_size[1] > source_height,
        )

    @staticmethod
    def _source_size(path: Path) -> tuple[int, int] | None:
        try:
            with Image.open(path) as image:
                return image.size
        except (OSError, ValueError, UnidentifiedImageError):
            return None

    @staticmethod
    def _process(path: Path, target: tuple[int, int]) -> Image.Image:
        with Image.open(path) as source:
            image = source.convert("RGB")
            background = image.resize((1, 1)).getpixel((0, 0))
        fitted = ImageOps.contain(image, target, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", target, background)
        left = (target[0] - fitted.width) // 2
        top = (target[1] - fitted.height) // 2
        canvas.paste(fitted, (left, top))
        return canvas

    @staticmethod
    def _average_color(path: Path) -> str:
        with Image.open(path) as source:
            pixel = source.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return "#{:02x}{:02x}{:02x}".format(*pixel)

    async def _ensure_original(self, url: str) -> Path | None:
        existing = await asyncio.to_thread(self.cache.get_original, url)
        if existing:
            return existing
        lock = self._lock(self._download_locks, url)
        async with lock:
            existing = await asyncio.to_thread(self.cache.get_original, url)
            if existing:
                return existing
            self.download_count += 1
            data = await self._download(url)
            if not data:
                return None
            extension = await asyncio.to_thread(self._image_extension, data)
            if not extension:
                return None
            return await asyncio.to_thread(self.cache.store_original, url, data, extension)

    async def _download(self, url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_original_bytes:
                        return None
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        buffer.extend(chunk)
                        if len(buffer) > self.max_original_bytes:
                            return None
                    data = bytes(buffer)
            await asyncio.to_thread(self._validate, data)
            return data
        except (httpx.HTTPError, OSError, ValueError, UnidentifiedImageError):
            return None

    @staticmethod
    def _validate(data: bytes) -> None:
        if not data:
            raise ValueError("empty image")
        with Image.open(io.BytesIO(data)) as image:
            image.verify()

    @staticmethod
    def _image_extension(data: bytes) -> str | None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                return (image.format or "").lower()
        except (OSError, UnidentifiedImageError):
            return None
