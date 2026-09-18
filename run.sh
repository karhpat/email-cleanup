#!/bin/bash

# Inbox Cleanup launcher for macOS and Linux

set -e

# Change to the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check for --demo flag
DEMO_MODE=0
if [ "$1" = "--demo" ]; then
    DEMO_MODE=1
fi

# Need Python 3.11 or newer
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ and try again."
    exit 1
fi

# Create .venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
fi

# Install dependencies if needed
if [ ! -f ".venv/.deps-installed" ] || [ "requirements.txt" -nt ".venv/.deps-installed" ]; then
    echo "Installing dependencies..."
    .venv/bin/pip install -q -r requirements.txt
    touch .venv/.deps-installed
fi

# Copy .env.example to .env if .env doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    echo "Note: Set your ANTHROPIC_API_KEY in .env if you want AI labeling. See docs/SETUP.md for details."
    cp .env.example .env
fi

# Open browser after 2 seconds in the background
open_browser() {
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 && return; fi
    if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 && return; fi
    echo "Browser did not open automatically. Visit $1 in your browser."
}
open_browser "http://127.0.0.1:${PORT:-8765}" &

# Set DEMO mode if flag was passed
if [ $DEMO_MODE -eq 1 ]; then
    export DEMO=1
fi

# Run the app
exec .venv/bin/python -m backend.api
