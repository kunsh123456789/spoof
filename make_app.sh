#!/bin/bash
# Builds "iPhone Location.app" (macOS): double-click it and the simulator opens in its own window.
# Usage: ./make_app.sh [destination-folder]      (default: ~/Applications)
#
# The app is a small launcher that points at this project folder and its .venv, so don't move the folder
# afterwards (run this script again if you do). Logs: ~/Library/Logs/iPhone Location.log (rotated)
# and iPhone Location.startup.log (only if it crashes while starting).
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/iPhone Location.app"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "Set up the virtualenv first:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi
if ! "$PROJECT/.venv/bin/python" -c "import webview" 2>/dev/null; then
  echo "The window toolkit (pywebview) is missing. Install it with:" >&2
  echo "  source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# icon: assets/icon.png -> AppIcon.icns (needs the macOS tools sips and iconutil)
ICON_KEY=""
if [ -f "$PROJECT/assets/icon.png" ] && command -v sips >/dev/null && command -v iconutil >/dev/null; then
  SET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$PROJECT/assets/icon.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$PROJECT/assets/icon.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  if iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null; then
    ICON_KEY="  <key>CFBundleIconFile</key><string>AppIcon</string>"
  fi
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>iPhone Location</string>
  <key>CFBundleDisplayName</key><string>iPhone Location</string>
  <key>CFBundleIdentifier</key><string>local.iphone-location</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>2.0</string>
$ICON_KEY
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

{
  echo '#!/bin/bash'
  printf 'PROJECT=%q\n' "$PROJECT"
  cat <<'LAUNCH'
LOG="$HOME/Library/Logs/iPhone Location.startup.log"   # only start-up crashes land here; the app keeps its own rotated log
mkdir -p "$(dirname "$LOG")"
cd "$PROJECT" || exit 1
: > "$LOG"
exec "$PROJECT/.venv/bin/python" "$PROJECT/desktop.py" "$@" >>"$LOG" 2>&1
LAUNCH
} > "$APP/Contents/MacOS/launch"
chmod +x "$APP/Contents/MacOS/launch"

echo "Created: $APP"
echo "Double-click it (or drag it to your Dock). Closing the window quits the app."
echo "If it does not open, look at:  ~/Library/Logs/iPhone Location.startup.log  and  iPhone Location.log"
