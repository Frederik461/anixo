from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "validate_macos_bundle.py"
_SPEC = importlib.util.spec_from_file_location("anixo_validate_macos_bundle", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
validator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validator)


def test_finds_resources_in_pyinstaller_internal_layout(tmp_path: Path) -> None:
    bundle = tmp_path / "ANIXO.app"
    resource = (
        bundle
        / "Contents"
        / "Resources"
        / "Anixo"
        / "_internal"
        / "styles"
        / "theme.tcss"
    )
    resource.parent.mkdir(parents=True)
    resource.write_text("/* theme */", encoding="utf-8")

    found = validator.find_bundled_resource(bundle, "styles/theme.tcss")

    assert found == resource


def test_resolves_rpath_library_from_nested_dylibs(tmp_path: Path) -> None:
    bundle = tmp_path / "ANIXO.app"
    binary = bundle / "Contents" / "Resources" / "Anixo" / "_internal" / "PIL" / "_webp.so"
    library = binary.parent / ".dylibs" / "libwebp.7.dylib"
    binary.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    library.write_bytes(b"library")
    bundle_files = {path.name: path for path in bundle.rglob("*") if path.is_file()}

    assert validator.dependency_exists(
        "@rpath/libwebp.7.dylib", binary, bundle, bundle_files
    )


def test_recognizes_only_os_shared_cache_dependencies() -> None:
    assert validator.is_system_shared_cache_dependency("/usr/lib/libSystem.B.dylib")
    assert validator.is_system_shared_cache_dependency(
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
    )
    assert not validator.is_system_shared_cache_dependency("/usr/local/lib/libffi.dylib")
    assert not validator.is_system_shared_cache_dependency("/opt/homebrew/lib/libffi.dylib")
