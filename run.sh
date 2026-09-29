#!/usr/bin/env bash
# Quickstart script to launch the UR Robotics Lab Platform
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Activate virtual environment if available
if [ -f ".venv/bin/activate" ]; then
    . .venv/bin/activate
elif [ -f "../.venv/bin/activate" ]; then
    . ../.venv/bin/activate
fi

# Print Local IP addresses so students know what URL to type
echo "========================================================"
echo "    Universal Robots Lab Platform - Starting Up"
echo "========================================================"
echo "Instructor Dashboard: http://localhost:8000/admin"
echo ""
echo "Share one of these URLs with your students on the LAN:"
if command -v ip >/dev/null 2>&1; then
    ip -4 addr show 2>/dev/null | awk '/inet / && !/127.0.0.1/ {sub(/\/.*/, "", $2); print " -> http://" $2 ":8000"}'
elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | awk '/inet / && !/127.0.0.1/ {print " -> http://" $2 ":8000"}'
fi
echo "========================================================"

# Determine Python command
PYTHON_CMD="python3"
if [ -f ".venv/bin/python" ]; then
    PYTHON_CMD=".venv/bin/python"
elif [ -f "../.venv/bin/python" ]; then
    PYTHON_CMD="../.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

# Check if uvicorn is available
if ! $PYTHON_CMD -c "import uvicorn" >/dev/null 2>&1; then
    echo "❌ Error: 'uvicorn' is not installed in the selected Python environment ($PYTHON_CMD)."
    echo ""
    echo "Quick setup:"
    echo "  1) python3 -m venv .venv"
    echo "  2) source .venv/bin/activate"
    echo "  3) pip install -r requirements.txt"
    echo ""
    exit 1
fi

RELOAD_FLAG=""
if [ "$1" = "--reload" ] || [ "$DEV_MODE" = "1" ]; then
    RELOAD_FLAG="--reload"
fi

# Run FastAPI server
exec $PYTHON_CMD -m uvicorn app:app --host 0.0.0.0 --port 8000 $RELOAD_FLAG

