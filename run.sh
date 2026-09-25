#!/usr/bin/env bash
# Quickstart script to launch the UR Robotics Lab Platform
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Activate virtual environment if available
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "../.venv" ]; then
    source ../.venv/bin/activate
fi

# Print Local IP addresses so students know what URL to type
echo "========================================================"
echo "    Universal Robots Lab Platform - Starting Up"
echo "========================================================"
echo "Instructor Dashboard: http://localhost:8000/admin"
echo ""
echo "Share one of these URLs with your students on the LAN:"
if command -v ip >/dev/null 2>&1; then
    ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -v '127.0.0.1' | awk '{print " -> http://" $1 ":8000"}'
elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig | grep "inet " | grep -v 127.0.0.1 | awk '{print " -> http://" $2 ":8000"}'
fi
echo "========================================================"

# Determine Python command and reload flag
PYTHON_CMD="python3"
if command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

RELOAD_FLAG=""
if [ "$1" == "--reload" ] || [ "$DEV_MODE" == "1" ]; then
    RELOAD_FLAG="--reload"
fi

# Run FastAPI server
$PYTHON_CMD -m uvicorn app:app --host 0.0.0.0 --port 8000 $RELOAD_FLAG

