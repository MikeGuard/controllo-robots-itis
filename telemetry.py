"""
Live Robot Telemetry & Debug Hub (strictly READ-ONLY).

Provides, for both the UR3 (ur_rtde RTDEReceive + primary-interface messages + dashboard
allow-list) and the Niryo Ned (pyniryo), a cached sampler that the admin Run page polls or
streams over WebSocket. Nothing in this module ever commands motion: it only *reads* state
so it is safe to run while a student program is driving the arm.

Every blocking SDK call runs inside worker threads (asyncio.to_thread) with short timeouts,
so a disconnected / unreachable arm can never freeze the FastAPI event loop.
"""

import asyncio
import math
import re
import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

import config

HISTORY_LEN = 1200          # ~4 minutes at 5 Hz
DEBUG_LEN = 600
SAMPLE_MIN_INTERVAL = 0.15  # seconds between hardware reads (shared by all subscribers)

UR_SAFETY_MODES = {
    1: "NORMAL", 2: "REDUCED", 3: "PROTECTIVE STOP", 4: "RECOVERY", 5: "SAFEGUARD STOP",
    6: "SYSTEM EMERGENCY STOP", 7: "ROBOT EMERGENCY STOP", 8: "VIOLATION", 9: "FAULT",
    10: "VALIDATE JOINT ID", 11: "UNDEFINED", 12: "AUTOMATIC MODE SAFEGUARD STOP",
    13: "SYSTEM THREE-POSITION ENABLING STOP",
}
UR_ROBOT_MODES = {
    -1: "NO CONTROLLER", 0: "DISCONNECTED", 1: "CONFIRM SAFETY", 2: "BOOTING", 3: "POWER OFF",
    4: "POWER ON", 5: "IDLE", 6: "BACKDRIVE", 7: "RUNNING", 8: "UPDATING FIRMWARE",
}
UR_RUNTIME_STATES = {0: "STOPPING", 1: "STOPPED", 2: "PLAYING", 3: "PAUSING", 4: "PAUSED", 5: "RESUMING"}
UR_SAFE_MODES_OK = (1, 2)

# Read-only UR dashboard-server queries an admin may issue from the debug console.
DASHBOARD_ALLOWLIST = (
    "robotmode", "safetymode", "safetystatus", "running", "programstate",
    "get loaded program", "get serial number", "polyscopeversion", "get robot model",
    "isinremotecontrol", "is in remote control", "get operational mode",
)

_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")


def _norm(vec) -> float:
    try:
        return math.sqrt(sum(float(v) * float(v) for v in vec))
    except Exception:
        return 0.0


def _probe(ip: str, port: int, timeout: float = 0.6) -> Optional[str]:
    """Returns None when reachable, otherwise a short error string."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return None
    except OSError as e:
        return f"{ip}:{port} unreachable ({e.__class__.__name__})"


class _BaseSampler:
    model = ""

    def __init__(self):
        self.lock = threading.Lock()
        self.history: deque = deque(maxlen=HISTORY_LEN)
        self.debug: deque = deque(maxlen=DEBUG_LEN)
        self.debug_seq = 0
        self.connected = False
        self.error = ""
        self.next_retry = 0.0
        self.last_sample: Optional[Dict[str, Any]] = None
        self.last_sample_ts = 0.0
        self._prev_pose: Optional[List[float]] = None
        self._prev_q: Optional[List[float]] = None
        self._prev_t: Optional[float] = None
        self.path_length = 0.0
        self.max_speed = 0.0
        self.speed_sum = 0.0
        self.speed_n = 0
        self.moving_time = 0.0
        self.started_at = time.time()
        self._state_cache: Dict[str, Any] = {}

    # ----- debug log -----
    def log(self, level: str, text: str, source: str = "supervisor"):
        with self.lock:
            self.debug_seq += 1
            self.debug.append({
                "seq": self.debug_seq, "ts": time.time(),
                "level": level, "source": source, "text": text,
            })

    def debug_since(self, seq: int) -> List[Dict[str, Any]]:
        with self.lock:
            return [d for d in self.debug if d["seq"] > seq]

    def _track_change(self, key: str, value: Any, describer):
        old = self._state_cache.get(key, "__unset__")
        if old != value:
            self._state_cache[key] = value
            if old != "__unset__":
                msg, level = describer(old, value)
                self.log(level, msg, source="state")

    # ----- stats -----
    def reset_stats(self):
        with self.lock:
            self.history.clear()
            self.path_length = 0.0
            self.max_speed = 0.0
            self.speed_sum = 0.0
            self.speed_n = 0
            self.moving_time = 0.0
            self._prev_pose = None
            self._prev_q = None
            self._prev_t = None
            self.started_at = time.time()

    def _integrate(self, now: float, pose: Optional[List[float]], q: Optional[List[float]],
                   speed: Optional[float]) -> Dict[str, Any]:
        """Derives speed / path length / joint speeds from successive samples."""
        dt = (now - self._prev_t) if self._prev_t else 0.0
        derived: Dict[str, Any] = {}
        if pose and self._prev_pose and dt > 1e-3:
            d = _norm([pose[i] - self._prev_pose[i] for i in range(3)])
            if speed is None:
                speed = d / dt
            if d < 1.0:  # ignore teleport glitches
                self.path_length += d
        if q and self._prev_q and dt > 1e-3:
            derived["joint_speed_fd"] = [(q[i] - self._prev_q[i]) / dt for i in range(min(len(q), len(self._prev_q)))]
        if speed is not None:
            self.max_speed = max(self.max_speed, speed)
            self.speed_sum += speed
            self.speed_n += 1
            if speed > 0.003 and dt > 0:
                self.moving_time += dt
        if pose:
            self._prev_pose = list(pose)
        if q:
            self._prev_q = list(q)
        self._prev_t = now
        derived["speed"] = speed or 0.0
        return derived

    def stats(self) -> Dict[str, Any]:
        return {
            "path_length": self.path_length,
            "max_speed": self.max_speed,
            "avg_speed": (self.speed_sum / self.speed_n) if self.speed_n else 0.0,
            "moving_time": self.moving_time,
            "samples": self.speed_n,
            "window_sec": time.time() - self.started_at,
        }

    # ----- to be implemented -----
    def _ip(self) -> str:
        raise NotImplementedError

    def _read(self) -> Dict[str, Any]:
        raise NotImplementedError

    def close(self):
        pass

    def sample(self) -> Dict[str, Any]:
        """Blocking: reads hardware once (rate-limited) and returns a snapshot dict."""
        now = time.time()
        if self.last_sample and (now - self.last_sample_ts) < SAMPLE_MIN_INTERVAL:
            return self.last_sample

        base = {"model": self.model, "ip": self._ip(), "ts": now}
        try:
            data = self._read()
            if not self.connected:
                self.connected = True
                self.log("info", f"Connected to {self.model.upper()} telemetry at {self._ip()}")
            self.error = ""
            snap = {**base, "connected": True, "error": "", **data}
        except Exception as e:  # noqa: BLE001 - any SDK failure => offline snapshot
            if self.connected:
                self.log("warn", f"Telemetry link lost: {e}")
            self.connected = False
            self.error = str(e)
            self.close()
            self.next_retry = time.time() + 2.5
            snap = {**base, "connected": False, "error": self.error}

        snap["stats"] = self.stats()
        self.last_sample = snap
        self.last_sample_ts = now

        if snap.get("connected"):
            pose = snap.get("tcp_pose") or []
            with self.lock:
                self.history.append({
                    "t": now,
                    "speed": snap.get("tcp_speed", 0.0),
                    "x": pose[0] if len(pose) > 0 else 0.0,
                    "y": pose[1] if len(pose) > 1 else 0.0,
                    "z": pose[2] if len(pose) > 2 else 0.0,
                    "q": snap.get("q"),
                    "qd": snap.get("qd"),
                    "cur": snap.get("joint_current"),
                    "tmp": snap.get("joint_temp"),
                })
        return snap


# ---------------------------------------------------------------------------
# UR3
# ---------------------------------------------------------------------------

class URSampler(_BaseSampler):
    model = "ur"

    def __init__(self):
        super().__init__()
        self.rcv = None
        self._rcv_ip: Optional[str] = None
        self._msg_thread: Optional[threading.Thread] = None
        self._msg_stop = threading.Event()
        self._msg_ip: Optional[str] = None

    def _ip(self) -> str:
        return getattr(config, "UR_IP", "")

    def close(self):
        try:
            if self.rcv is not None:
                self.rcv.disconnect()
        except Exception:
            pass
        self.rcv = None
        self._rcv_ip = None

    def _ensure_msg_listener(self):
        ip = self._ip()
        if self._msg_thread and self._msg_thread.is_alive() and self._msg_ip == ip:
            return
        self._msg_stop.set()
        self._msg_stop = threading.Event()
        self._msg_ip = ip
        self._msg_thread = threading.Thread(target=self._msg_loop, args=(ip, self._msg_stop), daemon=True)
        self._msg_thread.start()

    def _connect(self):
        ip = self._ip()
        if self.rcv is not None and self._rcv_ip == ip:
            return
        self.close()
        if time.time() < self.next_retry:
            raise ConnectionError(self.error or "waiting to retry")
        err = _probe(ip, getattr(config, "ROBOT_RTDE_PORT", 30004))
        if err:
            raise ConnectionError(err)
        import rtde_receive
        try:
            self.rcv = rtde_receive.RTDEReceiveInterface(ip, 20.0)
        except TypeError:
            self.rcv = rtde_receive.RTDEReceiveInterface(ip)
        self._rcv_ip = ip
        self._ensure_msg_listener()

    def _g(self, name: str, default=None):
        try:
            return getattr(self.rcv, name)()
        except Exception:
            return default

    def _read(self) -> Dict[str, Any]:
        self._connect()
        if not self.rcv.isConnected():
            raise ConnectionError("RTDE receive interface disconnected")
        now = time.time()
        q = self._g("getActualQ", [])
        qd = self._g("getActualQd", [])
        pose = self._g("getActualTCPPose", [])
        tcp_speed_vec = self._g("getActualTCPSpeed", [])
        speed = _norm(tcp_speed_vec[:3]) if tcp_speed_vec else None
        derived = self._integrate(now, pose, q, speed)

        safety = self._g("getSafetyMode", 0)
        rmode = self._g("getRobotMode", -1)
        rstate = self._g("getRuntimeState", -1)
        scale = self._g("getSpeedScaling", 1.0)

        self._track_change("safety", safety, lambda o, n: (
            f"Safety mode: {UR_SAFETY_MODES.get(o, o)} -> {UR_SAFETY_MODES.get(n, n)}",
            "info" if n in UR_SAFE_MODES_OK else "error"))
        self._track_change("rmode", rmode, lambda o, n: (
            f"Robot mode: {UR_ROBOT_MODES.get(o, o)} -> {UR_ROBOT_MODES.get(n, n)}", "info"))
        self._track_change("rstate", rstate, lambda o, n: (
            f"Controller program state: {UR_RUNTIME_STATES.get(o, o)} -> {UR_RUNTIME_STATES.get(n, n)}", "info"))

        return {
            "q": q, "qd": qd,
            "tcp_pose": pose,
            "tcp_speed_vec": tcp_speed_vec,
            "tcp_speed": derived["speed"],
            "tcp_force": self._g("getActualTCPForce", []),
            "joint_current": self._g("getActualCurrent", []),
            "joint_temp": self._g("getJointTemperatures", []),
            "joint_voltage": self._g("getActualJointVoltage", []),
            "robot_voltage": self._g("getActualRobotVoltage", None),
            "robot_current": self._g("getActualRobotCurrent", None),
            "main_voltage": self._g("getActualMainVoltage", None),
            "momentum": self._g("getActualMomentum", None),
            "safety_mode": safety,
            "safety_text": UR_SAFETY_MODES.get(safety, str(safety)),
            "robot_mode": rmode,
            "robot_mode_text": UR_ROBOT_MODES.get(rmode, str(rmode)),
            "runtime_state": rstate,
            "runtime_text": UR_RUNTIME_STATES.get(rstate, str(rstate)),
            "speed_scaling": scale,
            "protective_stop": bool(self._g("isProtectiveStopped", False)),
            "emergency_stop": bool(self._g("isEmergencyStopped", False)),
            "dio_out": self._g("getActualDigitalOutputBits", 0),
            "dio_in": self._g("getActualDigitalInputBits", 0),
            "ai0": self._g("getStandardAnalogInput0", None),
            "ai1": self._g("getStandardAnalogInput1", None),
            "tool_accel": self._g("getActualToolAccelerometer", []),
        }

    # ----- primary-interface robot messages (the controller's own log / debug stream) -----
    def _msg_loop(self, ip: str, stop: threading.Event):
        port = 30001
        while not stop.is_set():
            sock = None
            try:
                if _probe(ip, port, 0.8):
                    stop.wait(3.0)
                    continue
                sock = socket.create_connection((ip, port), timeout=2.0)
                sock.settimeout(2.0)
                self.log("info", f"Listening to UR controller messages on {ip}:{port}", source="controller")
                buf = b""
                while not stop.is_set():
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 5:
                        (ln,) = struct.unpack(">i", buf[:4])
                        if ln < 5 or ln > 1 << 20:
                            buf = b""
                            break
                        if len(buf) < ln:
                            break
                        pkt, buf = buf[:ln], buf[ln:]
                        if pkt[4] == 20:  # ROBOT_MESSAGE
                            self._parse_robot_message(pkt[5:])
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"Controller message stream error: {e}", source="controller")
            finally:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass
            stop.wait(3.0)

    _MSG_TYPES = {0: "TEXT", 1: "LABEL", 2: "POPUP", 3: "VERSION", 5: "SAFETY", 6: "ERROR", 7: "KEY",
                  9: "REQUEST", 10: "EXCEPTION"}

    def _parse_robot_message(self, payload: bytes):
        if len(payload) < 10:
            return
        source = payload[8]
        mtype = payload[9]
        body = payload[10:]
        runs = [r.decode("ascii", "ignore").strip() for r in _PRINTABLE_RUN.findall(body)]
        text = " | ".join(r for r in runs if r)
        label = self._MSG_TYPES.get(mtype, f"TYPE{mtype}")
        if mtype == 1 and not text:
            return
        if not text:
            return
        level = "error" if mtype in (5, 6, 10) else "info"
        if mtype == 0 and re.search(r"(error|fault|violation|protective)", text, re.I):
            level = "error"
        # de-duplicate identical consecutive messages
        key = f"{label}:{text}"
        if self._state_cache.get("_last_msg") == key:
            return
        self._state_cache["_last_msg"] = key
        self.log(level, f"[{label} src={source}] {text}", source="controller")

    # ----- dashboard read-only console -----
    def dashboard_query(self, command: str) -> str:
        cmd = (command or "").strip()
        if cmd.lower() not in DASHBOARD_ALLOWLIST:
            raise PermissionError(
                "Only read-only dashboard queries are allowed: " + ", ".join(DASHBOARD_ALLOWLIST))
        ip = self._ip()
        with socket.create_connection((ip, 29999), timeout=1.5) as s:
            s.settimeout(1.5)
            try:
                s.recv(1024)  # greeting
            except socket.timeout:
                pass
            s.sendall((cmd + "\n").encode())
            data = s.recv(2048).decode("utf-8", "replace").strip()
        return data


# ---------------------------------------------------------------------------
# Niryo Ned
# ---------------------------------------------------------------------------

class NiryoSampler(_BaseSampler):
    model = "niryo"

    def __init__(self):
        super().__init__()
        self.robot = None
        self._robot_ip: Optional[str] = None
        self._hw_ts = 0.0
        self._hw_cache: Dict[str, Any] = {}

    def _ip(self) -> str:
        return getattr(config, "NIRYO_IP", "")

    def close(self):
        try:
            if self.robot is not None:
                try:
                    self.robot.close_connection()
                except Exception:
                    self.robot.end()
        except Exception:
            pass
        self.robot = None
        self._robot_ip = None

    def _connect(self):
        ip = self._ip()
        if self.robot is not None and self._robot_ip == ip:
            return
        self.close()
        if time.time() < self.next_retry:
            raise ConnectionError(self.error or "waiting to retry")
        err = _probe(ip, getattr(config, "NIRYO_PORT", 9090))
        if err:
            raise ConnectionError(err)
        from pyniryo import NiryoRobot
        self.robot = NiryoRobot(ip)
        self._robot_ip = ip

    def _hardware(self) -> Dict[str, Any]:
        now = time.time()
        if now - self._hw_ts < 1.5 and self._hw_cache:
            return self._hw_cache
        hw = self.robot.get_hardware_status()

        def a(name, default=None):
            return getattr(hw, name, default)

        data = {
            "joint_temp": list(a("temperatures", []) or []),
            "joint_voltage": list(a("voltages", []) or []),
            "hardware_errors": list(a("hardware_errors", []) or []),
            "rpi_temperature": a("rpi_temperature"),
            "calibration_needed": a("calibration_needed"),
            "calibration_in_progress": a("calibration_in_progress"),
            "hardware_version": a("hardware_version"),
            "motor_names": list(a("motor_names", []) or []),
            "error_message": a("error_message", ""),
        }
        self._hw_cache = data
        self._hw_ts = now

        self._track_change("calib", data["calibration_needed"], lambda o, n: (
            f"Calibration needed: {o} -> {n}", "warn" if n else "info"))
        self._track_change("hwerr", tuple(data["hardware_errors"]) if any(data["hardware_errors"]) else (), lambda o, n: (
            f"Hardware errors changed: {list(n) or 'none'}", "error" if n else "info"))
        return data

    def _read(self) -> Dict[str, Any]:
        self._connect()
        now = time.time()
        q = [float(v) for v in self.robot.get_joints()]
        pose_obj = self.robot.get_pose()
        pose = pose_obj.to_list() if hasattr(pose_obj, "to_list") else list(pose_obj)
        derived = self._integrate(now, pose, q, None)
        qd = derived.get("joint_speed_fd") or [0.0] * len(q)

        hw = {}
        try:
            hw = self._hardware()
        except Exception as e:  # noqa: BLE001
            hw = {"hardware_error_read": str(e)}

        learning = None
        try:
            learning = bool(self.robot.get_learning_mode())
            self._track_change("learning", learning, lambda o, n: (f"Learning mode: {o} -> {n}", "info"))
        except Exception:
            pass

        hw_err = [e for e in (hw.get("hardware_errors") or []) if e]
        return {
            "q": q, "qd": qd,
            "tcp_pose": pose,
            "tcp_speed": derived["speed"],
            "learning_mode": learning,
            "safety_mode": 0 if hw_err else 1,
            "safety_text": "HARDWARE ERROR" if hw_err else "NORMAL",
            "robot_mode_text": "LEARNING MODE" if learning else "ACTIVE",
            "runtime_text": "—",
            "protective_stop": False,
            "emergency_stop": False,
            **hw,
        }


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

class TelemetryHub:
    def __init__(self):
        self.samplers: Dict[str, _BaseSampler] = {"ur": URSampler(), "niryo": NiryoSampler()}
        self._locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def normalize(model: Optional[str]) -> str:
        return "niryo" if model and "niryo" in model.lower() else "ur"

    def sampler(self, model: Optional[str]) -> _BaseSampler:
        return self.samplers[self.normalize(model)]

    async def get(self, model: Optional[str]) -> Dict[str, Any]:
        m = self.normalize(model)
        lock = self._locks.setdefault(m, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(asyncio.to_thread(self.samplers[m].sample), timeout=3.0)
            except asyncio.TimeoutError:
                s = self.samplers[m]
                return {"model": m, "ip": s._ip(), "ts": time.time(), "connected": False,
                        "error": "telemetry read timed out", "stats": s.stats()}

    def history(self, model: Optional[str], limit: int = 600) -> List[Dict[str, Any]]:
        s = self.sampler(model)
        with s.lock:
            return list(s.history)[-limit:]

    def debug_since(self, model: Optional[str], seq: int = 0) -> List[Dict[str, Any]]:
        return self.sampler(model).debug_since(seq)

    def debug_seq(self, model: Optional[str]) -> int:
        return self.sampler(model).debug_seq

    def log(self, model: Optional[str], level: str, text: str, source: str = "supervisor"):
        self.sampler(model).log(level, text, source)

    def reset_stats(self, model: Optional[str]):
        self.sampler(model).reset_stats()

    async def dashboard(self, model: Optional[str], command: str) -> str:
        if self.normalize(model) != "ur":
            raise PermissionError("Dashboard console is only available for the UR3.")
        s = self.samplers["ur"]
        assert isinstance(s, URSampler)
        out = await asyncio.wait_for(asyncio.to_thread(s.dashboard_query, command), timeout=4.0)
        s.log("info", f"> {command}\n{out}", source="dashboard")
        return out


hub = TelemetryHub()
