#!/bin/bash
# Framecycler macOS Runner Script

# Exit immediately if a command exits with a non-zero status
set -e

# Change directory to the repository root
cd "$(dirname "$0")"

echo "==================================================="
echo "              Starting Framecycler"
echo "==================================================="

# 1. Check for python3
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 could not be found."
    echo "Please install Python 3.12+."
    exit 1
fi

# 2. Check for virtual environment
if [ ! -d ".venv" ]; then
    echo "[INFO] Virtual environment not found. Creating one in .venv..."
    python3 -m venv .venv
fi

# 3. Activate virtual environment
echo "[INFO] Activating virtual environment..."
source .venv/bin/activate

# 4. Check/Install Python dependencies
if ! python3 -c "import PySide6, numpy, OpenColorIO, OpenImageIO, cmake" &> /dev/null; then
    echo "[INFO] Dependencies missing or incomplete. Installing from requirements.txt..."
    pip install -r requirements.txt
else
    echo "[INFO] Python dependencies are up-to-date."
fi

# 5. Check if native C++ engine is compiled (.so or .pyd)
# On macOS it compiles to a .so file
ENGINE_FOUND=0
if ls src/framecycler/framecycler_engine*.so &> /dev/null || ls src/framecycler/framecycler_engine*.pyd &> /dev/null; then
    ENGINE_FOUND=1
fi

if [ $ENGINE_FOUND -eq 0 ]; then
    echo "[INFO] Native engine binary not found. Compiling extension..."
    python3 build.py
else
    echo "[INFO] Native engine binary found."
fi

# 6. Run Framecycler
echo "[INFO] Generating build version metadata..."
python3 scripts/generate_version.py

echo "[INFO] Running Framecycler..."
python3 -m src.framecycler "$@"
