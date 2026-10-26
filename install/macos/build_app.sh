#!/usr/bin/env bash
# Build "PDF Semantic Search.app" and a drag-to-Applications .dmg.
#
#   python3.12 -m venv .venv && .venv/bin/pip install -r requirements-build.txt
#   install/macos/build_app.sh
#
# Signing: ad-hoc by default (fine on your own Mac; first launch via right-click → Open).
# Set CODESIGN_IDENTITY="Developer ID Application: …" to sign for distribution, then notarize.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${PYTHON:-$PROJECT/.venv/bin/python}"
APP_NAME="PDF Semantic Search"
BUNDLE_ID="io.github.galsuchetzky.pdfsemanticsearch"
VERSION="1.0.0"
IDENTITY="${CODESIGN_IDENTITY:--}"
WORK="$PROJECT/build/macos"
APP="$PROJECT/dist/$APP_NAME.app"
DMG="$PROJECT/dist/PDF-Semantic-Search-$VERSION.dmg"

[[ -x "$PYTHON" ]] || { echo "Create .venv and install requirements-build.txt first (or set PYTHON)." >&2; exit 1; }
"$PYTHON" -m PyInstaller --version >/dev/null

cd "$PROJECT"
rm -rf "$WORK" "$APP" "$DMG"
mkdir -p "$WORK/AppIcon.iconset"

# Icon: render at 1024px, then let iconutil build the .icns.
"$PYTHON" install/make_icon.py "$WORK/icon-1024.png"
for size in 16 32 128 256 512; do
  sips -z $size $size "$WORK/icon-1024.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z $double $double "$WORK/icon-1024.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/AppIcon.iconset" -o "$WORK/AppIcon.icns"

"$PYTHON" -m PyInstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --icon "$WORK/AppIcon.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --workpath "$WORK/pyinstaller" --specpath "$WORK" --distpath "$PROJECT/dist" \
  --add-data "$PROJECT/install/assets:assets" \
  --collect-submodules viewers --collect-submodules app \
  --exclude-module tkinter \
  main.py

PLIST="$APP/Contents/Info.plist"
set_plist() { /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" >/dev/null 2>&1 || true; /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"; }
set_plist CFBundleDisplayName string "$APP_NAME"
set_plist CFBundleShortVersionString string "$VERSION"
set_plist CFBundleVersion string "$VERSION"
set_plist LSMinimumSystemVersion string "12.0"
# Shown by macOS when the user switches on viewer integration (Preview/Skim automation).
set_plist NSAppleEventsUsageDescription string "Optional: lists PDFs open in Preview or Skim and opens search results at the matching page."

# Editing Info.plist invalidates PyInstaller's signature; sign the finished bundle.
# The hardened runtime (required for notarization) enforces library validation,
# which rejects ad-hoc signed bundled libraries, so use it only with a real identity.
if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - "$APP"
else
  codesign --force --deep --options runtime --timestamp \
    --entitlements "$PROJECT/install/macos/entitlements.plist" --sign "$IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"

STAGE="$WORK/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null

echo "App: $APP"
echo "DMG: $DMG"
