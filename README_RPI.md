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

On your Raspberry Pi or Linux machine, navigate to the cloned folder and run the installer:

```bash
cd controllo-robots-itis
./setup_rpi.sh
```

> **Tip:** If you want to automatically register and start the server as a background service on boot, simply run:
> ```bash
> ./setup_rpi.sh --service
> ```

### What `setup_rpi.sh` does automatically:
1. **Handles User Ownership Correctly:** Safely detects the real user (even when invoked via `sudo`) ensuring `.venv` and project files have correct non-root permissions.
2. **Uses Pre-compiled APT System Packages:** Installs `python3-numpy`, `python3-opencv`, `python3-pydantic`, `python3-websockets`, and `python3-aiosqlite` via Debian APT to save hours of compilation.
3. **Creates Virtual Environment with `--system-site-packages`:** Inherits the pre-compiled packages instantly.
4. **Installs Clean Dependencies:** Uses pre-built wheels and explicitly avoids the obsolete `enum34` conflict on Python 3.
5. **Configures & Installs Systemd Service:** Generates `ur-platform.service` and automatically enables and starts it if `--service` (or interactive confirmation) is chosen.

---

## 3. Running the Server

### Option A: Automatic Background Service (Recommended)
If you ran `./setup_rpi.sh --service`, the server is already active and will automatically launch on every reboot!

- Check service status:
  ```bash
  sudo systemctl status ur-platform
  ```
- View live console logs:
  ```bash
  sudo journalctl -u ur-platform -f
  ```
- Restart or stop the service:
  ```bash
  sudo systemctl restart ur-platform
  sudo systemctl stop ur-platform
  ```

### Option B: Manual / Foreground Run
```bash
./run.sh
```
The script will display the local IP address for students to connect to (e.g. `http://10.0.10.62:8000`).


---

## 4. Raspberry Pi 3 Optimization Notes

- **Headless OpenCV:** We use `opencv-python-headless` to avoid installing heavy X11/Qt GUI dependencies on the Pi.
- **Worker Configuration:** Uvicorn runs with `--workers 1` to minimize RAM usage (~80-120 MB RAM in idle).
- **Reloading Disabled in Production:** File watching is disabled by default in `run.sh` to save CPU cycles on low-power ARM cores. To run in dev mode with reload, use `./run.sh --reload`.
- **Emergency Stop Safety:** Emergency stop (`SPACE` key and `[STOP]` button) sends raw abort commands to both UR RTDE/Secondary ports and Niryo TCP socket, halting physical motions immediately.
