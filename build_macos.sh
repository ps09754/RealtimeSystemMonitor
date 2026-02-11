#!/bin/zsh
set -euo pipefail

APP_NAME="RealtimeSystemMonitor"
ICON_PATH="${1:-}"

ARGS=(
  --windowed
  --noconfirm
  --clean
  --name "$APP_NAME"
  --hidden-import AppKit
  --hidden-import Foundation
  --hidden-import objc
  --collect-submodules PySide6
  --collect-submodules pyqtgraph
  --collect-submodules objc
)

if [[ -n "$ICON_PATH" ]]; then
  EXT="${ICON_PATH##*.}"
  EXT="${EXT:l}"
  if [[ "$EXT" == "png" || "$EXT" == "jpg" || "$EXT" == "jpeg" ]]; then
    ICONSET_DIR="build/icon.iconset"
    mkdir -p "$ICONSET_DIR"
    ICON_SRC="build/icon_source.png"
    /usr/bin/sips -s format png "$ICON_PATH" --out "$ICON_SRC" >/dev/null
    /usr/bin/sips -z 16 16 "$ICON_SRC" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
    /usr/bin/sips -z 32 32 "$ICON_SRC" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
    /usr/bin/sips -z 32 32 "$ICON_SRC" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
    /usr/bin/sips -z 64 64 "$ICON_SRC" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
    /usr/bin/sips -z 128 128 "$ICON_SRC" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
    /usr/bin/sips -z 256 256 "$ICON_SRC" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
    /usr/bin/sips -z 256 256 "$ICON_SRC" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
    /usr/bin/sips -z 512 512 "$ICON_SRC" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
    /usr/bin/sips -z 512 512 "$ICON_SRC" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
    /usr/bin/sips -z 1024 1024 "$ICON_SRC" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null
    /usr/bin/iconutil -c icns "$ICONSET_DIR" -o "build/AppIcon.icns"
    ICON_PATH="build/AppIcon.icns"
  fi
  ARGS+=(--icon "$ICON_PATH")
fi

python -m PyInstaller "${ARGS[@]}" realtime_overlay.py

# Mark as background menu bar app (hide dock icon)
PLIST="dist/$APP_NAME.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"

# Ensure app icon is embedded in bundle
if [[ -n "$ICON_PATH" ]]; then
  /bin/mkdir -p "dist/$APP_NAME.app/Contents/Resources"
  /bin/cp "$ICON_PATH" "dist/$APP_NAME.app/Contents/Resources/AppIcon.icns"
  /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$PLIST"
fi

# Ad-hoc sign to reduce Gatekeeper issues
codesign --deep --force --sign - "dist/$APP_NAME.app"

DMG_PATH="dist/$APP_NAME.dmg"
hdiutil create -volname "$APP_NAME" -srcfolder "dist/$APP_NAME.app" -ov -format UDZO "$DMG_PATH"

echo "Built: dist/$APP_NAME.app"
echo "DMG: $DMG_PATH"

# Keep only DMG if requested
rm -rf build
rm -rf "dist/$APP_NAME.app"
