#!/bin/bash
# Build DJApp.app standalone — no Python required
set -e
cd "$(dirname "$0")"

PYTHON=/Users/hiura/kyudo_env/bin/python
PYINSTALLER=/Users/hiura/kyudo_env/bin/pyinstaller

echo "==> Killing any running instances..."
pkill -9 -f "python.*dj_app.py" 2>/dev/null || true
sleep 1

echo "==> Building DJApp.app..."
"$PYINSTALLER" dj_app.spec --clean --noconfirm

echo ""
echo "Done! App is at: dist/LuchaPinchadiscos.app"
echo "To run: open dist/LuchaPinchadiscos.app"
