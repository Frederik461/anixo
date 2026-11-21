# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone macOS ANIXO uninstaller."""

from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - supplied by PyInstaller

a = Analysis(
    [str(ROOT / "uninstaller.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["platformdirs"],
    hookspath=[str(ROOT / "packaging")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "textual",
        "textual_image",
        "PIL",
        "httpx",
        "pytest",
        "_pytest",
        "tkinter",
        "curses",
    ],
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
    console=True,
    debug=False,
    strip=False,
    upx=False,
    onefile=True,
)
