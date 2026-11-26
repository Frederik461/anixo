"""Generate the signed-ready release manifest consumed by ANIXO's updater.

The manifest is deterministic: asset names, digests and sizes are read from the
finished release directory, and every download URL is built from an HTTPS base
URL.  Secrets and credentials are never read or embedded.

Usage:
    python packaging/make_release_manifest.py \
        --release-dir dist/release \
        --version 0.1.0-beta.6 \
        --base-url https://github.com/OWNER/REPO/releases/download/v0.1.0-beta.6 \
        --output dist/release/release-manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 1

# filename template, platform, architecture, kind
_ASSET_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("ANIXO-Setup-v{version}.exe", "windows", "x86_64", "installer"),
    ("ANIXO-Portable-v{version}.zip", "windows", "x86_64", "portable"),
    ("ANIXO-Updater-windows-x64.exe", "windows", "x86_64", "helper"),
    ("ANIXO-v{version}-macos-arm64.dmg", "macos", "arm64", "installer"),
    ("ANIXO-Updater-macos-arm64.zip", "macos", "arm64", "helper"),
    ("ANIXO-v{version}-macos-x86_64.dmg", "macos", "x86_64", "installer"),
    ("ANIXO-Updater-macos-x86_64.zip", "macos", "x86_64", "helper"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _channel(version: str) -> str:
    return "beta" if "-" in version else "stable"


def build_manifest(
    release_dir: Path,
    version: str,
    base_url: str,
    *,
    notes: str = "",
    published_at: str = "",
) -> dict[str, Any]:
    """Validate the release directory and return the manifest payload."""
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Release asset base URL must use HTTPS.")
    if not version or not version[0].isdigit():
        raise ValueError("Version must start with a numeric component.")
    root = Path(release_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Release directory does not exist: {root}")

    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for template, system, arch, kind in _ASSET_SPECS:
        name = template.format(version=version)
        path = root / name
        if not path.is_file():
            continue
        key = (system, arch, kind)
        if key in seen:
            raise ValueError(f"Duplicate {system}/{arch}/{kind} asset in manifest.")
        seen.add(key)
        assets.append(
            {
                "name": name,
                "url": f"{base_url.rstrip('/')}/{name}",
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "platform": system,
                "arch": arch,
                "kind": kind,
            }
        )

    if not assets:
        raise ValueError("No release assets were found for this version.")
    for system in {item["platform"] for item in assets}:
        kinds = {item["kind"] for item in assets if item["platform"] == system}
        if "installer" not in kinds and "portable" not in kinds:
            raise ValueError(f"{system} has no installable application asset.")
        if "helper" not in kinds:
            raise ValueError(f"{system} is missing its updater helper asset.")

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "channel": _channel(version),
        "published_at": published_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": notes or f"ANIXO {version} — Your anime. Your world.",
        "assets": assets,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--published-at", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    notes = ""
    if args.notes_file:
        try:
            notes = args.notes_file.read_text(encoding="utf-8")
        except OSError as error:
            print(f"Could not read release notes: {error}", file=sys.stderr)
            return 2
    try:
        payload = build_manifest(
            args.release_dir,
            args.version,
            args.base_url,
            notes=notes,
            published_at=args.published_at,
        )
    except ValueError as error:
        print(f"Release manifest validation failed: {error}", file=sys.stderr)
        return 2
    output = args.output or args.release_dir / "release-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"Wrote {output} with {len(payload['assets'])} verified assets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
