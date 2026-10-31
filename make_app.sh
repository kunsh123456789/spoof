#!/bin/bash
# Builds "iPhone Location.app" (macOS). Usage: ./make_app.sh [destination-folder]
# The app is a small launcher that points at this project folder, so don't move the folder afterwards
# (re-run this script if you do).
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/iPhone Location.app"

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "Set up the virtualenv first:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

chmod +x "$PROJECT/run.command"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>iPhone Location</string>
  <key>CFBundleDisplayName</key><string>iPhone Location</string>
  <key>CFBundleIdentifier</key><string>local.iphone-location</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST

{
  echo '#!/bin/bash'
  printf 'PROJECT=%q\n' "$PROJECT"
  cat <<'LAUNCH'
URL="http://localhost:8000"
up() { curl -s -o /dev/null -m 1 "$URL/api/status"; }

if ! up; then
  open -a Terminal "$PROJECT/run.command"
  for _ in $(seq 1 60); do up && break; sleep 0.5; done
fi
open "$URL"
LAUNCH
} > "$APP/Contents/MacOS/launch"
chmod +x "$APP/Contents/MacOS/launch"

echo "Created: $APP"
echo "Double-click it, or drag it to your Dock. Quit the app by pressing Ctrl+C in its Terminal window."
