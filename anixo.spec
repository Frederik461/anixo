# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ANIXO one-folder Windows distribution.

Build from the repository root with::

    pyinstaller --noconfirm --clean anixo.spec

The result is ``dist/Anixo/Anixo.exe`` plus its bundled data files. A separate
one-file *validation* build is produced by ``packaging/build_windows.ps1``.
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
ICON = ROOT / "assets" / "anixo.ico"

datas = [
    (str(ROOT / "styles" / "theme.tcss"), "styles"),
    (str(ICON), "assets"),
    (str(ROOT / "assets" / "anixo-installer.ico"), "assets"),
    # certifi is used by httpx; bundle its CA bundle explicitly so HTTPS works
    # on machines that do not have a usable system certificate store.
    (str(Path(certifi.where())), "certifi"),
]

binaries = []
binaries.extend(collect_dynamic_libs("PIL"))
datas.extend(collect_data_files("PIL"))

# Keep distribution metadata available at runtime (importlib.metadata lookups).
for _dist in ("textual", "textual-image", "Pillow", "rich", "httpx", "certifi"):
    try:
        datas.extend(copy_metadata(_dist))
    except Exception:  # pragma: no cover - metadata is optional at runtime
        pass

hiddenimports = []
for _package in ("textual", "textual_image", "PIL"):
    hiddenimports.extend(collect_submodules(_package))
# collect_submodules() above already discovers every Textual widget and
# textual-image renderer. Explicit legacy widget names are deliberately avoided
# because Textual 8.x moved several widgets to private modules.
hiddenimports = sorted(set(hiddenimports))

excludes = [
    "rich_pixels",  # unused legacy dependency
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

# console=True is deliberate: ANIXO is a terminal application.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Anixo",
    icon=str(ICON),
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
