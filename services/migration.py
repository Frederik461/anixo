"""Import legacy artwork originals into the new filesystem cache."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from data.database import Database
from services.image_cache import ImageCache


def import_legacy_artwork(
    legacy_data_dir: Path,
    image_cache: ImageCache,
    database: Database,
) -> int:
    """Copy legacy ``<sha256>.original`` files without importing derived images."""
    if database.meta("legacy_artwork_imported") == "1":
        return int(database.meta("legacy_artwork_rows", "0"))

    legacy_dir = legacy_data_dir / "cache" / "artwork"
    if not legacy_dir.exists():
        database.set_meta("legacy_artwork_imported", "1")
        database.set_meta("legacy_artwork_rows", "0")
        return 0

    # Import every valid original, even if its URL is no longer referenced;
    # artwork is immutable and cheap to keep, and stale JSON metadata may not
    # contain every URL that produced a cached original.
    imported = 0
    for source in sorted(legacy_dir.glob("*.original")):
        digest = source.stem
        try:
            data = source.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        extension = _extension(data) or "img"
        target = image_cache.originals / f"{digest}.{extension}"
        if not target.exists() or target.stat().st_size == 0:
            try:
                target.write_bytes(data)
            except OSError:
                continue
        imported += 1

    database.set_meta("legacy_artwork_imported", "1")
    database.set_meta("legacy_artwork_rows", str(imported))
    return imported


def _extension(data: bytes) -> str | None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return (image.format or "").lower()
    except (OSError, UnidentifiedImageError):
        return None
