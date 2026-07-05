#!/bin/bash
# ============================================================
# Mac equivalent of "Start App.bat" - same logic, same order of
# checks, same messages where they make sense on this platform.
# Double-click this file in Finder to run it (Terminal opens
# automatically). If double-clicking just opens it as text instead
# of running it, right-click -> Open, or run:
#   chmod +x "Start App.command"
# once from Terminal to mark it executable, then double-click again.
# ============================================================

# Always work from the folder this file is actually in, regardless of
# how it was launched - same reasoning as "cd /d %~dp0" on Windows.
cd "$(dirname "$0")"

echo "============================================================"
echo "  Orthographic Template Generator"
echo "============================================================"
echo
echo "This window will set everything up and then open the app in"
echo "your browser. The first run takes a few minutes; after that"
echo "it will start in a few seconds."
echo
echo "Please don't close this window while the app is running -"
echo "closing it will shut the app down."
echo
echo "============================================================"
echo

# --- Find a working Python launcher -------------------------------
# Macs ship "python3" by default; plain "python" is often missing or
# points at an old Python 2. Try python3 first, then python, before
# giving up - same "try more than one name" approach as the .bat.
PYTHON_CMD=""

if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD=python
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "------------------------------------------------------------"
    echo "  Python isn't installed yet - that's the only thing"
    echo "  missing before this will work."
    echo "------------------------------------------------------------"
    echo
    echo "  1. Go to https://www.python.org/downloads/"
    echo "  2. Download and run the installer"
    echo "  3. Once that finishes, double-click this file again"
    echo
    echo "Opening the download page now..."
    open "https://www.python.org/downloads/"
    echo
    echo "You can close this window once you've installed Python,"
    echo "then double-click this file again to continue."
    echo
    read -p "Press Return to close this window..."
    exit 1
fi

echo "Found Python - checking it's a recent enough version..."
if ! "$PYTHON_CMD" "_check_python_version.py" >/dev/null 2>&1; then
    echo
    echo "------------------------------------------------------------"
    echo "  The Python on this Mac is older than this app needs"
    echo "  (3.9 or newer required)."
    echo "------------------------------------------------------------"
    echo "  Please install a newer version from:"
    echo "  https://www.python.org/downloads/"
    echo
    open "https://www.python.org/downloads/"
    echo
    read -p "Press Return to close this window..."
    exit 1
fi

echo "Python looks good."
echo

# --- Install/update dependencies -----------------------------------
# Same marker-file trick as the .bat: only re-run pip when
# requirements.txt has actually changed since the last successful
# install, so every launch after the first is fast.
MARKER=".deps_installed"
NEED_INSTALL=1

if [ -f "$MARKER" ] && cmp -s "$MARKER" "requirements.txt"; then
    NEED_INSTALL=0
fi

if [ "$NEED_INSTALL" = "1" ]; then
    echo "Setting up required components - this only happens once"
    echo "and may take a few minutes, depending on your internet"
    echo "connection. Please be patient..."
    echo
    "$PYTHON_CMD" -m pip install --upgrade pip --quiet
    if ! "$PYTHON_CMD" -m pip install -r requirements.txt; then
        echo
        echo "------------------------------------------------------------"
        echo "  Something went wrong while setting up. The message"
        echo "  above this box has more detail."
        echo "------------------------------------------------------------"
        echo
        echo "  A common fix: just run this file again - sometimes a"
        echo "  slow download just needs a retry."
        echo
        echo "  If it keeps happening, send a screenshot of this"
        echo "  whole window so it can be looked into."
        echo
        read -p "Press Return to close this window..."
        exit 1
    fi
    cp "requirements.txt" "$MARKER"
    echo
    echo "Setup complete."
    echo
else
    echo "Already set up - skipping straight to launch."
    echo
fi

# --- Launch the app ---------------------------------------------
echo "Starting the app..."
echo "Your browser should open automatically in a few seconds."
echo
echo "------------------------------------------------------------"
echo "  Leave this window open while you use the app."
echo "  Close this window when you're done to shut it down."
echo "------------------------------------------------------------"
echo

"$PYTHON_CMD" app.py

echo
echo "------------------------------------------------------------"
echo "  The app has stopped."
echo "------------------------------------------------------------"
echo
read -p "Press Return to close this window..."
