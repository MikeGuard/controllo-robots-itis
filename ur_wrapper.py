"""
Safe Universal Robots Wrapper for Student Submissions.
Enforces kinematic limits, speed clamping, workspace bounding boxes,
and provides simulated fallback if robot hardware is not reached.
"""

import builtins
import math
import socket
import sys
import time
from typing import List, Optional

# Load default robot IP from configuration
def _get_default_ip() -> str:
    try:
        import config
        return config.ROBOT_IP
    except Exception:
        return "10.0.10.60"


def printf(*args, **kwargs):
    """
    C-style or Python-style printf that auto-flushes stdout.
    Supports printf("Format %s: %d", name, val) or printf("Hello", "world").
    """
    kwargs.setdefault("flush", True)
    if len(args) > 1 and isinstance(args[0], str) and ("%" in args[0]):
        try:
            print(args[0] % args[1:], **kwargs)
            return
        except Exception:
            pass
    print(*args, **kwargs)


# Inject into builtins so any script importing ur_wrapper has printf globally
builtins.printf = printf

# Hardware safety limits
MAX_JOINT_SPEED = 0.5       # rad/s (approx 28 deg/s safe lab speed)
MAX_JOINT_ACCEL = 0.8       # rad/s^2
MAX_LINEAR_SPEED = 0.20     # m/s (200 mm/s)
MAX_LINEAR_ACCEL = 0.50     # m/s^2

# Cartesian Safety Bounding Box (Meters, base frame)
WORKSPACE_Z_MIN = 0.02      # Prevent crashing into table/baseplate
WORKSPACE_Z_MAX = 0.85
WORKSPACE_R_MAX = 0.85      # Max radius from base center (UR3/UR5 reach guard)


def _probe_ur_port(ip: str, port: int = 30004, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (socket.timeout, OSError):
        return False


class SafetyViolationError(Exception):
    pass


class RobotArm:
    def __init__(self, ip: Optional[str] = None, simulate_if_unreachable: bool = True):
        self.ip = ip or _get_default_ip()
        self.is_simulated = False
        self.rtde_c = None
        self.rtde_r = None

        print(f"[RobotArm] Checking connection to UR controller at {self.ip}:30004...", flush=True)
        online = _probe_ur_port(self.ip, 30004, timeout=0.8)
        if not online:
            # Also check port 30002 before giving up on physical arm
            online = _probe_ur_port(self.ip, 30002, timeout=0.8)

        if online:
            try:
                import rtde_control
                import rtde_receive
                self.rtde_r = rtde_receive.RTDEReceiveInterface(self.ip)
                self.rtde_c = rtde_control.RTDEControlInterface(self.ip)
                print(f"[RobotArm] Successfully connected to PHYSICAL robot at {self.ip}.", flush=True)
            except Exception as e:
                print(f"[RobotArm] RTDE connection error at {self.ip}: {e}", flush=True)
                online = False
        else:
            print(f"[RobotArm] Robot at {self.ip} did not respond on port 30004/30002.", flush=True)

        if not online:
            if simulate_if_unreachable:
                print(f"[RobotArm] NOTE: Physical robot offline. Running in SIMULATION MODE.", flush=True)
                self.is_simulated = True
                self._sim_joints = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
                self._sim_tcp = [0.3, -0.2, 0.3, 0.0, 3.14, 0.0]
            else:
                raise ConnectionError(f"Could not reach UR robot at {self.ip}")

    def _clamp_joint_limits(self, a: float, v: float) -> (float, float):
        safe_a = min(abs(a), MAX_JOINT_ACCEL)
        safe_v = min(abs(v), MAX_JOINT_SPEED)
        if safe_a != a or safe_v != v:
            print(f"[Safety Guard] Clamped joint motion dynamics to max safe limits (a={safe_a:.2f}, v={safe_v:.2f}).", flush=True)
        return safe_a, safe_v

    def _clamp_linear_limits(self, a: float, v: float) -> (float, float):
        safe_a = min(abs(a), MAX_LINEAR_ACCEL)
        safe_v = min(abs(v), MAX_LINEAR_SPEED)
        if safe_a != a or safe_v != v:
            print(f"[Safety Guard] Clamped linear motion dynamics to max safe limits (a={safe_a:.2f}, v={safe_v:.2f}).", flush=True)
        return safe_a, safe_v

    def _verify_cartesian_pose(self, pose: List[float]):
        x, y, z = pose[0], pose[1], pose[2]
        r = math.sqrt(x * x + y * y)
        if z < WORKSPACE_Z_MIN:
            raise SafetyViolationError(
                f"Target Z={z:.3f}m is below safe table limit (min: {WORKSPACE_Z_MIN}m). Collision risk!"
            )
        if z > WORKSPACE_Z_MAX:
            raise SafetyViolationError(
                f"Target Z={z:.3f}m exceeds upper ceiling limit (max: {WORKSPACE_Z_MAX}m)."
            )
        if r > WORKSPACE_R_MAX:
            raise SafetyViolationError(
                f"Target radial distance R={r:.3f}m exceeds max radius limit ({WORKSPACE_R_MAX}m)."
            )

    def movej(self, q: List[float], a: float = 0.5, v: float = 0.3):
        """Move to joint position with safety clamping."""
        if len(q) != 6:
            raise ValueError(f"Joint target must contain 6 angles (got {len(q)})")
        safe_a, safe_v = self._clamp_joint_limits(a, v)

        print(f"[RobotArm] Executing movej: {[round(x, 3) for x in q]} (v={safe_v:.2f}, a={safe_a:.2f})", flush=True)
        if self.is_simulated:
            time.sleep(0.5)
            self._sim_joints = list(q)
            print("[RobotArm (Sim)] Joint motion completed.", flush=True)
            return True
        else:
            return self.rtde_c.moveJ(q, safe_v, safe_a)

    def movel(self, pose: List[float], a: float = 0.3, v: float = 0.2):
        """Move to Cartesian pose [x, y, z, rx, ry, rz] with boundary checks."""
        if len(pose) != 6:
            raise ValueError(f"Cartesian pose must contain 6 elements [x, y, z, rx, ry, rz] (got {len(pose)})")
        self._verify_cartesian_pose(pose)
        safe_a, safe_v = self._clamp_linear_limits(a, v)

        print(f"[RobotArm] Executing movel: {[round(x, 3) for x in pose]} (v={safe_v:.2f}, a={safe_a:.2f})", flush=True)
        if self.is_simulated:
            time.sleep(0.5)
            self._sim_tcp = list(pose)
            print("[RobotArm (Sim)] Linear motion completed.", flush=True)
            return True
        else:
            return self.rtde_c.moveL(pose, safe_v, safe_a)

    def set_digital_out(self, pin: int, value: bool):
        """Set digital output pin (e.g. for pneumatic gripper)."""
        print(f"[RobotArm] Digital Out {pin} -> {value}", flush=True)
        if self.is_simulated:
            return True
        else:
            return self.rtde_c.setStandardDigitalOut(pin, value)

    def sleep(self, seconds: float):
        """Safe sleep with upper bound (max 10s)."""
        safe_sec = min(max(0.0, seconds), 10.0)
        time.sleep(safe_sec)

    def get_actual_joint_positions(self) -> List[float]:
        if self.is_simulated:
            return self._sim_joints
        return self.rtde_r.getActualQ()

    def get_actual_tcp_pose(self) -> List[float]:
        if self.is_simulated:
            return self._sim_tcp
        return self.rtde_r.getActualTCPPose()

    def stop(self):
        """Emergency stop helper."""
        print("[RobotArm] STOP command triggered.", flush=True)
        if not self.is_simulated and self.rtde_c:
            try:
                self.rtde_c.stopScript()
            except Exception:
                pass

    def __del__(self):
        self.stop()


if __name__ == "__main__":
    printf("[ur_wrapper] Initializing RobotArm test...")
    arm = RobotArm()
    printf("[ur_wrapper] Is Simulated: %s", arm.is_simulated)
    printf("[ur_wrapper] Actual Joints: %s", [round(x, 4) for x in arm.get_actual_joint_positions()])
    printf("[ur_wrapper] Actual TCP: %s", [round(x, 4) for x in arm.get_actual_tcp_pose()])
