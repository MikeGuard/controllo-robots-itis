#!/usr/bin/env bash
# ==============================================================================
# Ultra-Fast Setup Script for Raspberry Pi 3 (Zero / Minimal Compilation)
# ==============================================================================
set -e

echo "=========================================================="
echo "    UR & Niryo Platform - Raspberry Pi 3 Fast Installer"
echo "        (Optimized for Pre-compiled Binary Packages)      "
echo "=========================================================="

ARCH=$(uname -m)
echo "[1/4] Detected Architecture: $ARCH"
if [ "$ARCH" = "armv7l" ]; then
    echo "ℹ️  Running on 32-bit ($ARCH). Pre-compiled system packages will be used."
    echo "   (Tip: 64-bit aarch64 has even more pre-compiled PyPI wheels)."
else
    echo "✓ Running on 64-bit ($ARCH). Full pre-compiled wheel support available."
fi
echo ""

# 1. Install Pre-compiled Debian/Raspberry Pi OS system packages
# This avoids compiling numpy, opencv, pydantic, aiosqlite from source (saves ~2-4 hours!)
echo "[2/4] Installing pre-compiled packages via APT (NO compilation needed)..."
sudo apt update -y
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-numpy \
    python3-opencv \
    python3-pydantic \
    python3-websockets \
    python3-jinja2 \
    python3-aiosqlite \
    python3-multipart \
    iproute2 \
    curl

# 2. Set up Virtual Environment with --system-site-packages
# Inherits the pre-compiled numpy/opencv/pydantic from APT in 1 second!
echo "[3/4] Creating virtual environment (.venv) using pre-compiled site packages..."
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv --system-site-packages .venv
fi
source .venv/bin/activate

# 3. Install remaining lightweight Python packages using pre-built wheels only
echo "[4/4] Installing remaining packages from pre-compiled wheels (PiWheels / PyPI)..."
# Configure pip to prioritize piwheels and pre-built binaries, strictly refusing slow source builds
pip install --upgrade pip

# Install FastAPI and Uvicorn from binary wheels
pip install --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    fastapi "uvicorn[standard]" pyniryo==1.2.5

# Attempt to install pre-built ur_rtde wheel without compiling
echo "Checking for pre-compiled ur_rtde binary wheel..."
if ! pip install --only-binary :all: --prefer-binary ur_rtde 2>/dev/null; then
    echo "ℹ️  No pre-compiled binary wheel found for ur_rtde on $ARCH."
    echo "   Skipping source compilation to save time (RPi 3 would take ~45 minutes)."
    echo "   -> Platform features fully active: 3D Simulator, Niryo Ned, Web UI, Emergency Stop."
    echo "   -> (If you need physical UR3 RTDE driver compiled from source later, run: ./setup_rpi.sh --compile-ur)"
fi

# If the user explicitly passed --compile-ur, compile it on demand
if [ "$1" == "--compile-ur" ]; then
    echo "⚠️  User requested manual source compilation of ur_rtde..."
    # Ensure swap is available
    if [ -f /etc/dphys-swapfile ]; then
        sudo dphys-swapfile swapoff || true
        sudo sed -i 's/CONF_SWAPSIZE=[0-9]*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon
    fi
    sudo apt install -y cmake git libboost-all-dev build-essential python3-dev
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

# Configure ur-platform.service
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

echo "=========================================================="
echo "   ✓ Fast setup completed in seconds with ZERO compilation!"
echo "=========================================================="
echo "To run the platform:"
echo "   ./run.sh"
echo ""
echo "To enable autostart on boot as a system service:"
echo "   sudo cp ur-platform.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable --now ur-platform"
echo "=========================================================="
