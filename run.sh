#!/usr/bin/env bash
# Quickstart script to launch the UR Robotics Lab Platform
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Activate virtual environment
source ../.venv/bin/activate

# Print Local IP addresses so students know what URL to type
echo "========================================================"
echo "    Universal Robots Lab Platform - Starting Up"
echo "========================================================"
echo "Instructor Dashboard: http://localhost:8000/admin"
echo ""
echo "Share one of these URLs with your students on the LAN:"
ifconfig | grep "inet " | grep -v 127.0.0.1 | awk '{print " -> http://" $2 ":8000"}'
echo "========================================================"

# Run FastAPI server
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
