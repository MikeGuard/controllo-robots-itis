#!/usr/bin/env bash
# ==============================================================================
# Setup Script for UR & Niryo Robotics Platform on Raspberry Pi 3
# ==============================================================================
set -e

echo "=========================================================="
echo "    UR & Niryo Platform - Raspberry Pi 3 Installer"
echo "=========================================================="

# 1. Architecture Check
ARCH=$(uname -m)
echo "[1/6] Detecting System Architecture: $ARCH"
if [ "$ARCH" = "armv7l" ]; then
    echo "⚠️  WARNING: You are running 32-bit Raspberry Pi OS ($ARCH)."
    echo "   Many binary wheels (numpy, pydantic, opencv) are only available for 64-bit (aarch64)."
    echo "   Compilation from source may take a long time on RPi 3."
    echo "   RECOMMENDATION: Use Raspberry Pi OS 64-bit (aarch64) for best performance."
    echo ""
fi

# 2. Swap Memory Check (Critical for RPi 3 with 1GB RAM)
echo "[2/6] Checking Swap Space (Required for C++ build & wheel installation)..."
TOTAL_SWAP=$(free -m | awk '/Swap:/ {print $2}')
if [ "$TOTAL_SWAP" -lt 1500 ]; then
    echo "⚠️  Total swap is currently ${TOTAL_SWAP}MB (less than 1500MB)."
    echo "   Compiling ur_rtde or wheels will likely trigger Out-Of-Memory (OOM) killer."
    if [ -f /etc/dphys-swapfile ]; then
        echo "   Increasing swap to 2048MB in /etc/dphys-swapfile..."
        sudo dphys-swapfile swapoff || true
        sudo sed -i 's/CONF_SWAPSIZE=[0-9]*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon
        echo "   ✓ Swap increased to $(free -m | awk '/Swap:/ {print $2}')MB."
    else
        echo "   Notice: /etc/dphys-swapfile not found. Ensure at least 2GB swap is active."
    fi
else
    echo "   ✓ Sufficient swap detected: ${TOTAL_SWAP}MB."
fi

# 3. Install Required System Dependencies
echo "[3/6] Updating APT repositories & installing C++/Boost/Python packages..."
sudo apt update
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    cmake \
    git \
    libboost-all-dev \
    iproute2 \
    curl

# 4. Set up Virtual Environment
echo "[4/6] Setting up Python virtual environment (.venv)..."
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

pip install --upgrade pip setuptools wheel

# 5. Install Python Dependencies
echo "[5/6] Installing Python packages from requirements.txt..."
# Try standard pip install first
if ! pip install -r requirements.txt; then
    echo "⚠️  Standard pip install encountered an error. Retrying with headless fallback..."
    pip install fastapi "uvicorn[standard]" aiosqlite websockets pydantic numpy python-multipart jinja2 pyniryo==1.2.5 opencv-python-headless
    
    # Check if ur_rtde needs manual source compilation
    if ! python3 -c "import rtde_control" >/dev/null 2>&1; then
        echo "Building ur_rtde from source using cmake & make -j2 (safe for 1GB RAM)..."
        TEMP_BUILD_DIR=$(mktemp -d)
        git clone https://gitlab.com/sdurobotics/ur_rtde.git "$TEMP_BUILD_DIR"
        mkdir -p "$TEMP_BUILD_DIR/build"
        cd "$TEMP_BUILD_DIR/build"
        cmake -DPYBIND11_PYTHON_VERSION=3 -DCMAKE_BUILD_TYPE=Release ..
        make -j2
        sudo make install
        cd "$DIR"
        rm -rf "$TEMP_BUILD_DIR"
    fi
fi

# Configure ur-platform.service with current user and working dir
CURRENT_USER=$(whoami)
cat <<EOF > ur-platform.service
[Unit]
Description=Universal Robots & Niryo Educational Platform Server
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${DIR}
ExecStart=${DIR}/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 6. Final Instructions
echo "=========================================================="
echo "   ✓ Setup completed successfully on Raspberry Pi 3!"
echo "=========================================================="
echo "To run the platform manually:"
echo "   ./run.sh"
echo ""
echo "To install as a systemd service running at startup:"
echo "   sudo cp ur-platform.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable --now ur-platform"
echo "=========================================================="
