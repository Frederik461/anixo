# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS one-folder ANIXO console binary.

The .app bundle and DMG are assembled by packaging/build_macos.sh from this
output so the same source tree can be built on Apple Silicon and Intel runners.
"""

from pathlib import Path

import certifi
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH)  # noqa: F821 - supplied by PyInstaller

datas = [
    (str(ROOT / "styles" / "theme.tcss"), "styles"),
    (str(ROOT / "assets" / "anixo.png"), "assets"),
    (str(Path(certifi.where())), "certifi"),
]

binaries = []
binaries.extend(collect_dynamic_libs("PIL"))
datas.extend(collect_data_files("PIL"))

for _dist in ("textual", "textual-image", "Pillow", "rich", "httpx", "certifi"):
    try:
        datas.extend(copy_metadata(_dist))
    except Exception:  # pragma: no cover - metadata is optional at runtime
        pass

hiddenimports = []
for _package in ("textual", "textual_image", "PIL"):
    hiddenimports.extend(collect_submodules(_package))
hiddenimports = sorted(set(hiddenimports))

excludes = [
    "rich_pixels",
    "tkinter",
    "curses",
    "matplotlib",
    "numpy",
    "pytest",
    "_pytest",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging")],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "anixo_runtime_hook.py")],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Anixo",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Anixo",
)
