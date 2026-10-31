#!/bin/bash
# Double-clickable launcher: starts the app in a Terminal window.
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate
python app.py
echo
echo "App stopped. You can close this window."
