# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone ANIXO uninstaller.

Build from the repository root with::

    pyinstaller --noconfirm --clean uninstaller.spec

The result is a single console executable named ``ANIXO-Uninstaller``
(``ANIXO-Uninstaller.exe`` on Windows). It never launches the TUI.
"""

from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - supplied by PyInstaller
ICON = ROOT / "assets" / "anixo-installer.ico"

datas = []
binaries = []
hiddenimports = ["platformdirs"]
excludes = [
    "textual",
    "textual_image",
    "PIL",
    "httpx",
    "pytest",
    "_pytest",
    "tkinter",
    "curses",
]

a = Analysis(
    [str(ROOT / "uninstaller.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ANIXO-Uninstaller",
    icon=str(ICON) if ICON.exists() else None,
    console=True,
    debug=False,
    strip=False,
    upx=False,
    onefile=True,
)
