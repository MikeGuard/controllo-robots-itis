#!/usr/bin/env bash
# ==============================================================================
# Ultra-Fast Setup Script for Raspberry Pi & Linux Server
# ==============================================================================
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Detect real user even if script is invoked with sudo
TARGET_USER="${SUDO_USER:-$(whoami)}"

# Options
INSTALL_SERVICE=false
COMPILE_UR=false

for arg in "$@"; do
    case "$arg" in
        --service|-s|--install-service)
            INSTALL_SERVICE=true
            ;;
        --compile-ur)
            COMPILE_UR=true
            ;;
        --help|-h)
            echo "Usage: ./setup_rpi.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --service, -s, --install-service   Automatically install and enable as systemd service"
            echo "  --compile-ur                       Manually compile ur_rtde from C++ source"
            echo "  --help, -h                         Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Run ./setup_rpi.sh --help for available options."
            exit 1
            ;;
    esac
done

echo "=========================================================="
echo "    UR & Niryo Platform - Raspberry Pi Fast Installer     "
echo "=========================================================="
echo "Target User:      $TARGET_USER"
echo "Working Directory: $DIR"

ARCH=$(uname -m)
echo "[1/4] Detected Architecture: $ARCH"
if [ "$ARCH" = "armv7l" ]; then
    echo "ℹ️  Running on 32-bit ($ARCH). Pre-compiled system packages will be used."
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
echo "[3/4] Creating virtual environment (.venv) using pre-compiled site packages..."
if [ ! -d ".venv" ]; then
    if [ -n "$SUDO_USER" ]; then
        sudo -u "$TARGET_USER" python3 -m venv --system-site-packages .venv
    else
        python3 -m venv --system-site-packages .venv
    fi
fi

# Ensure .venv ownership belongs to the target user (prevents Permission Denied)
if [ -n "$SUDO_USER" ]; then
    sudo chown -R "$TARGET_USER:$TARGET_USER" "$DIR/.venv"
fi

source .venv/bin/activate

# 3. Install remaining lightweight Python packages using pre-built wheels only
echo "[4/4] Installing remaining packages from pre-compiled wheels (PiWheels / PyPI)..."
pip install --upgrade pip

# Install core web framework and async database packages
pip install --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    fastapi "uvicorn[standard]" jinja2 aiosqlite websockets python-multipart

# Install pyniryo without dependencies to prevent pulling obsolete enum34 on Python 3.4+
pip install --no-deps pyniryo==1.2.5

# Ensure enum34 legacy backport is purged if present (breaks Python 3.7+ standard library enum)
pip uninstall -y enum34 2>/dev/null || true

# Attempt to install pre-built ur_rtde wheel without compiling
echo "Checking for pre-compiled ur_rtde binary wheel..."
if ! pip install --only-binary :all: --prefer-binary ur_rtde 2>/dev/null; then
    echo "ℹ️  No pre-compiled binary wheel found for ur_rtde on $ARCH."
    echo "   Skipping source compilation to save time (RPi 3 would take ~45 minutes)."
    echo "   -> Platform features fully active: 3D Simulator, Niryo Ned, Web UI, Emergency Stop."
    echo "   -> (If you need physical UR3 RTDE driver compiled from source later, run: ./setup_rpi.sh --compile-ur)"
fi

# If the user explicitly passed --compile-ur, compile it on demand
if [ "$COMPILE_UR" = true ]; then
    echo "⚠️  User requested manual source compilation of ur_rtde..."
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

# Generate ur-platform.service tailored to TARGET_USER and DIR
cat <<EOF > "$DIR/ur-platform.service"
[Unit]
Description=Universal Robots & Niryo Educational Platform Server
After=network.target

[Service]
Type=simple
User=${TARGET_USER}
WorkingDirectory=${DIR}
ExecStart=${DIR}/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Ensure files in directory have correct ownership
if [ -n "$SUDO_USER" ]; then
    sudo chown -R "$TARGET_USER:$TARGET_USER" "$DIR"
fi

# Ask interactively if neither flag was given and running in a terminal
if [ "$INSTALL_SERVICE" = false ] && [ -t 0 ]; then
    echo ""
    read -p "Do you want to automatically install and enable the platform as a systemd background service on boot? [y/N]: " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        INSTALL_SERVICE=true
    fi
fi

# Install systemd service if requested
if [ "$INSTALL_SERVICE" = true ]; then
    echo ""
    echo "=========================================================="
    echo "   ⚙️  Installing and Enabling Systemd Service...         "
    echo "=========================================================="
    sudo cp "$DIR/ur-platform.service" /etc/systemd/system/ur-platform.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now ur-platform.service
    echo "✓ Service 'ur-platform' successfully installed and started!"
    echo "  Status: sudo systemctl status ur-platform"
    echo "  Logs:   sudo journalctl -u ur-platform -f"
    echo "=========================================================="
else
    echo "=========================================================="
    echo "   ✓ Setup completed successfully!                        "
    echo "=========================================================="
    echo "To run the platform manually:"
    echo "   ./run.sh"
    echo ""
    echo "To install as a background service later:"
    echo "   ./setup_rpi.sh --service"
    echo "=========================================================="
fi
