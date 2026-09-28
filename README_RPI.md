# Raspberry Pi 3 Deployment Guide

This guide covers deploying the **UR & Niryo Educational Robotics Platform** on a **Raspberry Pi 3** (1GB RAM, ARM Cortex-A53).

---

## 1. Prerequisites & OS Recommendation

- **Operating System:** **Raspberry Pi OS (64-bit / aarch64)** (Debian Bookworm or Bullseye).
  > **Note:** 64-bit is strongly recommended because pre-compiled binary wheels (for `numpy`, `opencv-python-headless`, and `pydantic-core`) are readily available. 32-bit (`armv7l`) requires compiling these from source, which takes significantly longer.
- **Hardware:** Raspberry Pi 3 Model B or B+ with a 16GB+ microSD card (Class 10 / A1 or A2).
- **Network:** Connected to the lab Ethernet switch with the Universal Robot (e.g. `10.0.10.60`) and Niryo Ned (`10.0.130.49`).

---

## 2. Automated One-Command Installation

On your Raspberry Pi 3, navigate to the cloned folder and run the installer:

```bash
cd ur_platform
bash setup_rpi.sh
```

### What `setup_rpi.sh` does automatically (Zero Compilation):
1. **Uses Pre-compiled APT System Packages:** Installs `python3-numpy`, `python3-opencv`, `python3-pydantic`, `python3-websockets`, and `python3-aiosqlite` via Debian APT. This avoids compiling them from source and saves ~2-4 hours.
2. **Creates Virtual Environment with `--system-site-packages`:** Inherits the pre-compiled packages in 1 second.
3. **Installs Lightweight Packages via PiWheels / PyPI:** Uses `--prefer-binary` to download pre-built ARM `.whl` wheels for FastAPI, Uvicorn, and PyNiryo.
4. **No Forced C++ Compiles:** Skips slow C++ source compilation for `ur_rtde` unless you explicitly run with `--compile-ur`.
5. **Generates Systemd Service:** Generates a custom `ur-platform.service` tuned to your user and directory.

---

## 3. Running the Server

### Option A: Manual / Terminal Run
```bash
./run.sh
```
The script will display the local IP address for students to connect to (e.g. `http://10.0.10.62:8000`).

### Option B: Automatic Startup on Boot (Recommended for Lab Server)
To run the server continuously in the background as a systemd service:

```bash
sudo cp ur-platform.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ur-platform
```

To view live logs:
```bash
sudo journalctl -u ur-platform -f
```

To stop or restart:
```bash
sudo systemctl stop ur-platform
sudo systemctl restart ur-platform
```

---

## 4. Raspberry Pi 3 Optimization Notes

- **Headless OpenCV:** We use `opencv-python-headless` to avoid installing heavy X11/Qt GUI dependencies on the Pi.
- **Worker Configuration:** Uvicorn runs with `--workers 1` to minimize RAM usage (~80-120 MB RAM in idle).
- **Reloading Disabled in Production:** File watching is disabled by default in `run.sh` to save CPU cycles on low-power ARM cores. To run in dev mode with reload, use `./run.sh --reload`.
- **Emergency Stop Safety:** Emergency stop (`SPACE` key and `[STOP]` button) sends raw abort commands to both UR RTDE/Secondary ports and Niryo TCP socket, halting physical motions immediately.
