#!/usr/bin/env bash
# Build and validate the macOS ANIXO .app and DMG.
#
# Run on a macOS runner or Mac with Python 3.12 and uv available:
#   bash packaging/build_macos.sh 0.1.0-beta.4
#
# Signing/notarization happen only when the Apple credentials are present.
# An unsigned DMG is still produced and is clearly reported as unsigned.
set -euo pipefail

VERSION="${1:-0.1.0-beta.4}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build_macos.sh must run on macOS; current host is $(uname -s)." >&2
  exit 2
fi

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) TARGET_ARCH="arm64" ;;
  x86_64) TARGET_ARCH="x86_64" ;;
  *)
    echo "Unsupported macOS architecture: $ARCH" >&2
    exit 2
    ;;
esac

RELEASE_DIR="$ROOT/dist/release/macos-$TARGET_ARCH"
BUILD_DIR="$ROOT/build/macos-$TARGET_ARCH"
APP_DIR="$RELEASE_DIR/ANIXO.app"
DMG_PATH="$RELEASE_DIR/ANIXO-v${VERSION}-macos-${TARGET_ARCH}.dmg"
UNINSTALLER_PATH="$RELEASE_DIR/ANIXO-Uninstaller-v${VERSION}-macos-${TARGET_ARCH}"

mkdir -p "$RELEASE_DIR" "$BUILD_DIR"
rm -rf "$APP_DIR" "$BUILD_DIR" "$RELEASE_DIR/ANIXO" "$DMG_PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to build ANIXO from source." >&2
  exit 2
fi

echo "ANIXO macOS build: version=$VERSION arch=$TARGET_ARCH"
if [[ "${ANIXO_SKIP_SYNC_AND_TESTS:-0}" == "1" ]]; then
  echo "ANIXO_SKIP_SYNC_AND_TESTS=1; the caller already synced and ran the tests."
else
  uv sync --locked --group build
  uv run pytest -q
fi

echo "Building the console binary."
uv run --group build pyinstaller --noconfirm --clean anixo_macos.spec \
  --distpath "$RELEASE_DIR" --workpath "$BUILD_DIR/app"

echo "Building the standalone uninstaller."
uv run --group build pyinstaller --noconfirm --clean uninstaller_macos.spec \
  --distpath "$RELEASE_DIR" --workpath "$BUILD_DIR/uninstaller"

BINARY="$RELEASE_DIR/Anixo/Anixo"
if [[ ! -x "$BINARY" ]]; then
  echo "Missing PyInstaller output: $BINARY" >&2
  exit 2
fi
"$BINARY" --version
"$BINARY" --diagnose >/dev/null

UNINSTALLER_BIN="$RELEASE_DIR/ANIXO-Uninstaller"
if [[ ! -x "$UNINSTALLER_BIN" ]]; then
  echo "Missing uninstaller output: $UNINSTALLER_BIN" >&2
  exit 2
fi
"$UNINSTALLER_BIN" --version
"$UNINSTALLER_BIN" --help >/dev/null
cp "$UNINSTALLER_BIN" "$UNINSTALLER_PATH"
chmod +x "$UNINSTALLER_PATH"

echo "Generating the .icns icon."
ICONSET="$BUILD_DIR/anixo.iconset"
ICNS="$BUILD_DIR/anixo.icns"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$ROOT/assets/anixo.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$ROOT/assets/anixo.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ICNS"

echo "Assembling ANIXO.app."
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp -R "$RELEASE_DIR/Anixo" "$APP_DIR/Contents/Resources/Anixo"
cp "$UNINSTALLER_BIN" "$APP_DIR/Contents/Resources/ANIXO-Uninstaller"
cp "$ICNS" "$APP_DIR/Contents/Resources/anixo.icns"
chmod +x "$APP_DIR/Contents/Resources/ANIXO-Uninstaller"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ANIXO</string>
  <key>CFBundleDisplayName</key><string>ANIXO</string>
  <key>CFBundleExecutable</key><string>ANIXO</string>
  <key>CFBundleIdentifier</key><string>io.anixo.app</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleIconFile</key><string>anixo.icns</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# ANIXO is a terminal application. Finder launching opens the bundled binary
# in Terminal.app instead of silently starting it without a TTY.
cat > "$APP_DIR/Contents/MacOS/ANIXO" <<'LAUNCHER'
#!/bin/bash
here="$(cd "$(dirname "$0")" && pwd)"
open -a Terminal "$here/../Resources/Anixo/Anixo" "$@"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/ANIXO"

if [[ -n "${MACOS_SIGNING_IDENTITY:-}" ]]; then
  echo "Signing ANIXO.app with the configured Apple identity."
  codesign --force --deep --options runtime --sign "$MACOS_SIGNING_IDENTITY" "$APP_DIR"
else
  echo "MACOS_SIGNING_IDENTITY absent; producing an unsigned .app."
fi

echo "Creating DMG."
hdiutil create -volname ANIXO -srcfolder "$APP_DIR" -ov -format UDZO "$DMG_PATH"
if [[ -n "${MACOS_SIGNING_IDENTITY:-}" ]]; then
  echo "Signing the distributed DMG."
  codesign --force --sign "$MACOS_SIGNING_IDENTITY" "$DMG_PATH"
  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    echo "Submitting the signed DMG for notarization."
    xcrun notarytool submit "$DMG_PATH" \
      --apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" \
      --team-id "$APPLE_TEAM_ID" --wait
    echo "Stapling the notarization ticket to the DMG."
    xcrun stapler staple "$DMG_PATH"
  else
    echo "Apple credentials absent; the DMG is signed but not notarized."
  fi
else
  echo "MACOS_SIGNING_IDENTITY absent; producing an unsigned DMG."
fi

(
  cd "$RELEASE_DIR"
  shasum -a 256 "$(basename "$DMG_PATH")" "$(basename "$UNINSTALLER_PATH")" > SHA256SUMS.txt
)

echo "macOS build complete:"
echo "  $APP_DIR"
echo "  $DMG_PATH"
echo "  $UNINSTALLER_PATH"
