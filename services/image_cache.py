"""A small, explicit filesystem cache for original and processed artwork."""

from __future__ import annotations

import hashlib
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError

CacheKind = Literal["original", "processed", "all"]


@dataclass(frozen=True, slots=True)
class CacheKey:
    url: str
    width: int
    height: int
    renderer: str

    def digest(self) -> str:
        raw = f"{self.url}\n{self.width}x{self.height}\n{self.renderer}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ImageCache:
    """Stores originals separately from dimension-specific processed images.

    Cache entries are immutable: a larger processed image never overwrites a
    smaller one because each rendering size has its own key.
    """

    VERSION = "v2"

    def __init__(self, root: Path, max_entries: int = 128) -> None:
        self.root = root
        self.originals = root / "originals"
        self.processed = root / "processed"
        self.max_entries = max(8, max_entries)
        self.originals.mkdir(parents=True, exist_ok=True)
        self.processed.mkdir(parents=True, exist_ok=True)
        self._pins: dict[Path, int] = {}
        # A valid processed file is immutable. Remember the stat signature of
        # files we have already verified so repeat hits do not decode the PNG.
        self._validated: OrderedDict[Path, tuple[int, int]] = OrderedDict()
        self._validated_limit = 512

    @staticmethod
    def original_digest(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def original_candidates(self, url: str) -> list[Path]:
        digest = self.original_digest(url)
        return sorted(
            path
            for path in self.originals.glob(f"{digest}.*")
            if not path.name.endswith(".tmp") and path.is_file()
        )

    def get_original(self, url: str) -> Path | None:
        candidates = self.original_candidates(url)
        return candidates[0] if candidates else None

    def store_original(self, url: str, data: bytes, extension: str = "img") -> Path:
        """Atomically store an original image. Existing bytes are never replaced."""
        existing = self.get_original(url)
        if existing:
            try:
                if existing.stat().st_size > 0:
                    return existing
            except OSError:
                pass
        suffix = "".join(character for character in extension if character.isalnum())
        suffix = suffix[:8] or "img"
        target = self.originals / f"{self.original_digest(url)}.{suffix}"
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target

    def pin(self, path: Path) -> None:
        """Keep a processed file alive while a widget is displaying it."""
        self._pins[path] = self._pins.get(path, 0) + 1

    def unpin(self, path: Path) -> None:
        """Release one display reference to a processed file."""
        remaining = self._pins.get(path, 0) - 1
        if remaining > 0:
            self._pins[path] = remaining
        else:
            self._pins.pop(path, None)

    def is_pinned(self, path: Path) -> bool:
        return path in self._pins

    def processed_path(self, key: CacheKey) -> Path:
        return self.processed / f"{self.VERSION}-{key.digest()}.png"

    def get_processed(self, key: CacheKey) -> Path | None:
        path = self.processed_path(key)
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._validated.pop(path, None)
            return None
        if signature[1] <= 0:
            self._validated.pop(path, None)
            return None
        if self._validated.get(path) == signature:
            self._validated.move_to_end(path)
            return path
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, UnidentifiedImageError):
            path.unlink(missing_ok=True)
            self._validated.pop(path, None)
            return None
        self._validated[path] = signature
        self._validated.move_to_end(path)
        while len(self._validated) > self._validated_limit:
            self._validated.popitem(last=False)
        return path

    def store_processed(self, key: CacheKey, image: Image.Image) -> Path:
        target = self.processed_path(key)
        try:
            if target.is_file() and target.stat().st_size > 0:
                return target
        except OSError:
            pass
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        image.convert("RGB").save(temporary, format="PNG", optimize=False)
        temporary.replace(target)
        return target

    def clear(self, kind: CacheKind = "all") -> None:
        if kind in ("processed", "all"):
            self._validated.clear()
        for directory in (
            [self.originals, self.processed]
            if kind == "all"
            else [self.originals if kind == "original" else self.processed]
        ):
            for path in directory.iterdir():
                try:
                    if path.is_file():
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

    def cleanup(self) -> int:
        """Remove least-recently-used processed files beyond the configured bound.

        In-progress ``.tmp`` writes and files pinned by mounted posters are
        ignored. Disappearing files are tolerated because several poster workers
        may clean up concurrently.
        """
        files: list[tuple[float, Path]] = []
        prefix = f"{self.VERSION}-"
        for path in self.processed.iterdir():
            if path.name.endswith(".tmp"):
                continue
            if not (path.name.startswith(prefix) and path.name.endswith(".png")):
                continue
            if path in self._pins:
                continue
            try:
                if path.is_file():
                    files.append((path.stat().st_mtime, path))
            except OSError:
                continue

        files.sort(key=lambda item: item[0])
        removed = 0
        while len(files) > self.max_entries:
            _, victim = files.pop(0)
            try:
                victim.unlink()
                self._validated.pop(victim, None)
                removed += 1
            except OSError:
                continue
        return removed

    def counts(self) -> dict[str, int]:
        def count(directory: Path) -> int:
            total = 0
            for path in directory.iterdir():
                if path.name.endswith(".tmp"):
                    continue
                try:
                    if path.is_file():
                        total += 1
                except OSError:
                    continue
            return total

        return {"original": count(self.originals), "processed": count(self.processed)}
