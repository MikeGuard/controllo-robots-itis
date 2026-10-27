"""
Accurate, time-parameterised UR3 & Niryo Ned kinematic simulator.

What the simulator guarantees (these were the root causes of the "janky / wrong movements" reports):

* Every motion command is expanded into a *time-stamped* trajectory using the real controller
  speed / acceleration arguments (trapezoidal velocity profile) instead of a fixed 10 steps, so
  slow moves look slow, fast moves look fast, and `time.sleep()` / `robot.wait()` appear on the
  timeline.  The browser plays the trajectory back by clock, not with ad-hoc timers.
* Cartesian moves interpolate orientation with a proper rotation-matrix slerp (the old code
  linearly interpolated rotation vectors which flips violently near +/-pi) and every IK solution is
  verified against forward kinematics, so unreachable targets are *reported* instead of silently
  producing a wrong pose.
* UR3 kinematics use the exact DH parameters; the Niryo model is built from the very same
  transform chain the 3D viewer draws, so what you see is what the maths computed.
* The result carries analytics (duration, path length, max/avg TCP speed, joint ranges, command
  timeline with source line numbers, warnings) that the Run & Simulate pages display.

No physical robot I/O or network access can happen while simulating.
"""

import os
import tempfile
import shutil
import math
import sys
import time
import types
from typing import Any, Dict, List, Tuple, Optional
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox_io

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_HZ = 25.0          # waypoint density along a motion
MIN_STEPS = 4
MAX_STEPS = 150
MAX_WAYPOINTS = 6000
MAX_SIM_SECONDS = 3600.0
IK_TIME_BUDGET_SEC = 8.0

# Standard UR3 DH parameters [a, d, alpha]
DH_UR3 = [
    [0.0,      0.1519,   np.pi / 2],    # Joint 1 (Base Pan to Shoulder)
    [-0.24365, 0.0,      0.0],          # Joint 2 (Upper Arm)
    [-0.21325, 0.0,      0.0],          # Joint 3 (Forearm)
    [0.0,      0.11235,  np.pi / 2],    # Joint 4 (Wrist 1)
    [0.0,      0.08535, -np.pi / 2],    # Joint 5 (Wrist 2)
    [0.0,      0.0819,   0.0]           # Joint 6 (Wrist 3 / Tool Flange)
]
UR_HOME_Q = [0.0, -math.pi / 2, math.pi / 2, -math.pi / 2, -math.pi / 2, 0.0]
UR_JOINT_LIMIT = 2 * math.pi

# Mirrors ur_wrapper.RobotArm safety envelope so a simulated RobotArm behaves like the real one.
WRAP_MAX_JOINT_SPEED = 0.5
WRAP_MAX_JOINT_ACCEL = 0.8
WRAP_MAX_LINEAR_SPEED = 0.20
WRAP_MAX_LINEAR_ACCEL = 0.50
WRAP_Z_MIN, WRAP_Z_MAX, WRAP_R_MAX = 0.02, 0.85, 0.85

NIRYO_HOME_Q = [0.0, 0.5, -1.25, 0.0, 0.0, 0.0]
# Typical Ned joint limits (rad); exceeding them only raises a warning in the simulator.
NIRYO_LIMITS = [(-3.05, 3.05), (-1.57, 0.64), (-1.40, 1.57), (-3.05, 3.05), (-1.75, 1.92), (-2.57, 2.57)]
NIRYO_TCP_LEN = 0.0        # extra tool length beyond the flange (m)
NIRYO_MAX_JOINT_SPEED = 1.8   # rad/s at 100 % arm velocity (estimate)
NIRYO_MAX_JOINT_ACCEL = 3.0
NIRYO_MAX_LINEAR_SPEED = 0.30  # m/s at 100 %
NIRYO_MAX_LINEAR_ACCEL = 1.0


# ---------------------------------------------------------------------------
# Rotation helpers
# ---------------------------------------------------------------------------

def rotvec_to_matrix(rotvec: np.ndarray) -> np.ndarray:
    """Axis-angle vector -> 3x3 rotation matrix (Rodrigues)."""
    rotvec = np.asarray(rotvec, dtype=float)
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-9:
        return np.eye(3)
    k = rotvec / theta
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(theta) * K + (1.0 - np.cos(theta)) * (K @ K)


def matrix_to_rotvec(R: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix -> axis-angle vector with robust handling near pi."""
    trace_val = (np.trace(R) - 1.0) / 2.0
    angle = float(np.arccos(np.clip(trace_val, -1.0, 1.0)))
    if angle < 1e-7:
        return np.zeros(3)
    if angle > np.pi - 1e-3:
        # Near 180 deg: recover axis from the symmetric part
        B = (R + np.eye(3)) / 2.0
        k = int(np.argmax(np.diag(B)))
        v = B[:, k] / math.sqrt(max(B[k, k], 1e-12))
        # keep sign consistent with the antisymmetric part when available
        w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
        if np.dot(v, w) < 0:
            v = -v
        n = np.linalg.norm(v)
        return angle * (v / n if n > 1e-9 else np.array([0.0, 0.0, 1.0]))
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    return (angle / (2.0 * np.sin(angle))) * v


def rotation_error_vec(R_target: np.ndarray, R_cur: np.ndarray) -> np.ndarray:
    """Axis-angle vector (world frame) that rotates R_cur into R_target."""
    return matrix_to_rotvec(R_target @ R_cur.T)


def rotation_angle(Ra: np.ndarray, Rb: np.ndarray) -> float:
    return float(np.linalg.norm(matrix_to_rotvec(Ra.T @ Rb)))


def slerp_matrix(R0: np.ndarray, R1: np.ndarray, s: float) -> np.ndarray:
    """Geodesic interpolation between two rotations."""
    rel = matrix_to_rotvec(R0.T @ R1)
    return R0 @ rotvec_to_matrix(rel * s)


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def rpy_to_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Roll-pitch-yaw (R = Rz(yaw) Ry(pitch) Rx(roll)) -> rotation matrix."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    Rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    Ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    return Rz @ Ry @ Rx


def matrix_to_rpy(R: np.ndarray) -> List[float]:
    pitch = float(np.arcsin(-np.clip(R[2, 0], -1.0, 1.0)))
    if np.abs(np.cos(pitch)) > 1e-6:
        roll = float(np.arctan2(R[2, 1], R[2, 2]))
        yaw = float(np.arctan2(R[1, 0], R[0, 0]))
    else:
        roll = float(np.arctan2(-R[1, 2], R[1, 1]))
        yaw = 0.0
    return [roll, pitch, yaw]


# ---------------------------------------------------------------------------
# UR3 kinematics
# ---------------------------------------------------------------------------

def forward_kinematics_ur3(q: List[float]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[np.ndarray], List[np.ndarray]]:
    """
    Exact 6-DOF UR3 forward kinematics (standard DH) to the tool *flange*.
    Returns: (pos_xyz, rotvec_xyz, R_3x3, joint_origins, joint_z_axes)
    """
    T = np.eye(4)
    positions = [np.zeros(3)]
    z_axes = [np.array([0.0, 0.0, 1.0])]

    for i in range(6):
        theta = float(q[i])
        a, d, alpha = DH_UR3[i]
        ct, st = np.cos(theta), np.sin(theta)
        ca, sa = np.cos(alpha), np.sin(alpha)
        Ti = np.array([
            [ct, -st * ca,  st * sa, a * ct],
            [st,  ct * ca, -ct * sa, a * st],
            [0.0, sa,      ca,      d],
            [0.0, 0.0,     0.0,     1.0]
        ])
        T = T @ Ti
        positions.append(T[:3, 3].copy())
        z_axes.append(T[:3, 2].copy())

    pos = T[:3, 3]
    R = T[:3, :3]
    return pos, matrix_to_rotvec(R), R, positions, z_axes


def ur_tcp_pose(q: List[float], tcp: Optional[List[float]] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Returns (position, R) of the TCP (flange + optional tool offset [x,y,z,rx,ry,rz])."""
    pos, _, R, _, _ = forward_kinematics_ur3(q)
    if tcp and any(abs(v) > 1e-12 for v in tcp):
        Rt = rotvec_to_matrix(np.array(tcp[3:6], dtype=float))
        pos = pos + R @ np.array(tcp[:3], dtype=float)
        R = R @ Rt
    return pos, R


def get_tcp_pose_list(q: List[float], tcp: Optional[List[float]] = None) -> List[float]:
    """[x, y, z, rx, ry, rz] TCP pose for the given joint configuration."""
    pos, R = ur_tcp_pose(q, tcp)
    return [float(x) for x in np.concatenate([pos, matrix_to_rotvec(R)])]


def _flange_target_from_tcp(pos: np.ndarray, R: np.ndarray, tcp: Optional[List[float]]) -> Tuple[np.ndarray, np.ndarray]:
    if tcp and any(abs(v) > 1e-12 for v in tcp):
        Rt = rotvec_to_matrix(np.array(tcp[3:6], dtype=float))
        R_f = R @ Rt.T
        pos_f = pos - R_f @ np.array(tcp[:3], dtype=float)
        return pos_f, R_f
    return pos, R


def _dls_ik(fk, q_seed, target_pos, target_R, max_iters: int, pos_tol: float, rot_tol: float,
            rot_weight: float = 1.0):
    """
    Damped-least-squares IK with null-space pull toward the seed (stays in the same
    kinematic branch, so Cartesian paths never flip elbow/wrist).
    `fk(q)` must return (pos, R, joint_origins, joint_axes).
    Returns (q, pos_err_norm, rot_err_norm).
    """
    q = np.array(q_seed, dtype=float)
    seed = q.copy()
    pos_err_n = rot_err_n = 0.0
    for _ in range(max_iters):
        pos, R, origins, axes = fk(q)
        pos_err = target_pos - pos
        rot_err = rotation_error_vec(target_R, R)
        pos_err_n = float(np.linalg.norm(pos_err))
        rot_err_n = float(np.linalg.norm(rot_err))
        if pos_err_n < pos_tol and rot_err_n < rot_tol:
            break

        J = np.zeros((6, 6))
        for i in range(6):
            J[:3, i] = np.cross(axes[i], pos - origins[i])
            J[3:, i] = axes[i]

        err6 = np.concatenate([pos_err, rot_err * rot_weight])
        e = float(np.linalg.norm(err6))
        damping = 1e-4 if e < 0.02 else (1e-3 if e < 0.1 else 1e-2)
        J_pinv = J.T @ np.linalg.inv(J @ J.T + damping * np.eye(6))
        dq = J_pinv @ err6
        dq += (np.eye(6) - J_pinv @ J) @ (seed - q) * 0.05

        step = float(np.linalg.norm(dq))
        if step > 0.25:
            dq *= 0.25 / step
        q += dq

    pos, R, _, _ = fk(q)
    pos_err_n = float(np.linalg.norm(target_pos - pos))
    rot_err_n = float(np.linalg.norm(rotation_error_vec(target_R, R)))
    # unwrap relative to seed (prevents 2*pi jumps)
    for j in range(6):
        diff = q[j] - seed[j]
        q[j] = seed[j] + (diff + np.pi) % (2 * np.pi) - np.pi
    return q, pos_err_n, rot_err_n


def _ur_fk_adapter(q):
    pos, _, R, origins, axes = forward_kinematics_ur3(list(q))
    # joint i (1..6) rotates about axes[i-1] located at origins[i-1]
    return pos, R, origins[:6], axes[:6]


def inverse_kinematics_6dof(q_seed: List[float], target_pose: List[float], max_iters: int = 60,
                            tcp: Optional[List[float]] = None) -> List[float]:
    """Backward-compatible wrapper: IK for a TCP pose [x,y,z,rx,ry,rz]."""
    q, _, _ = ik_ur(q_seed, np.array(target_pose[:3], dtype=float),
                    rotvec_to_matrix(np.array(target_pose[3:6], dtype=float)), tcp=tcp, max_iters=max_iters)
    return [float(x) for x in q]


def ik_ur(q_seed, target_pos, target_R, tcp=None, max_iters: int = 60, multi_seed: bool = False):
    pos_f, R_f = _flange_target_from_tcp(np.asarray(target_pos, dtype=float), target_R, tcp)
    seeds = [list(q_seed)]
    if multi_seed:
        seeds += [UR_HOME_Q, [0.0, -1.2, 1.6, -1.9, -1.57, 0.0], [-1.5, -1.2, 1.6, -1.9, -1.57, 0.0]]
    best = None
    for s in seeds:
        q, pe, re = _dls_ik(_ur_fk_adapter, s, pos_f, R_f, max_iters, 1e-4, 2e-3)
        score = pe + 0.1 * re + (0.002 * float(np.linalg.norm(np.array(q) - np.array(q_seed))) if multi_seed else 0.0)
        if best is None or score < best[0]:
            best = (score, q, pe, re)
        if pe < 1e-3 and re < 1e-2 and not multi_seed:
            break
    return best[1], best[2], best[3]


# ---------------------------------------------------------------------------
# Niryo Ned kinematics -- the same transform chain the Three.js model draws
# ---------------------------------------------------------------------------
# Three.js frame: X right, Y up, Z forward.  Robot frame: x forward, y left, z up.
_C_THREE_TO_ROBOT = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
# Tool frame: local x = approach axis (points down for pitch = +pi/2), identity at the zero pose.
_NIRYO_TOOL_AXES = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])  # columns = x,y,z tool axes in j5 frame

# (translation in parent frame, fixed rotation, joint axis) per joint, in Three.js coordinates
NIRYO_CHAIN = [
    ((0.0, 0.000, 0.0), np.eye(3), (0.0, 1.0, 0.0)),    # j0 base pan      (rotation.y =  q0)
    ((0.0, 0.103, 0.0), np.eye(3), (-1.0, 0.0, 0.0)),   # j1 shoulder      (rotation.x = -q1)
    ((0.0, 0.210, 0.0), _rx(np.pi / 2), (-1.0, 0.0, 0.0)),  # j2 elbow     (rotation.x = -q2)
    ((0.0, 0.190, 0.0), np.eye(3), (0.0, 1.0, 0.0)),    # j3 forearm roll  (rotation.y =  q3)
    ((0.0, 0.042, 0.0), np.eye(3), (-1.0, 0.0, 0.0)),   # j4 wrist pitch   (rotation.x = -q4)
    ((0.0, 0.035, 0.0), np.eye(3), (0.0, 1.0, 0.0)),    # j5 tool roll     (rotation.y =  q5)
]


def _axis_rot(axis: Tuple[float, float, float], ang: float) -> np.ndarray:
    return rotvec_to_matrix(np.array(axis, dtype=float) * ang)


def fk_niryo(q: List[float]):
    """Returns (pos, rpy, R, joint_origins, joint_axes) in the Niryo robot frame (tool = flange + NIRYO_TCP_LEN)."""
    Rw = np.eye(3)
    pw = np.zeros(3)
    origins, axes = [], []
    for i, (trans, Rfix, axis) in enumerate(NIRYO_CHAIN):
        pw = pw + Rw @ np.array(trans)
        Rw = Rw @ Rfix
        origins.append(_C_THREE_TO_ROBOT @ pw)
        axes.append(-_C_THREE_TO_ROBOT @ (Rw @ np.array(axis)))
        Rw = Rw @ _axis_rot(axis, float(q[i]))
    pw = pw + Rw @ np.array([0.0, NIRYO_TCP_LEN, 0.0])
    pos = _C_THREE_TO_ROBOT @ pw
    R = _C_THREE_TO_ROBOT @ Rw @ _NIRYO_TOOL_AXES
    return pos, matrix_to_rpy(R), R, origins, axes


def _niryo_fk_adapter(q):
    pos, _, R, origins, axes = fk_niryo(list(q))
    return pos, R, origins, axes


def ik_niryo_full(q_seed, target_pos, target_R, max_iters: int = 80, multi_seed: bool = True):
    seeds = [list(q_seed)]
    if multi_seed:
        seeds += [NIRYO_HOME_Q, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.0, -0.6, 0.3, 0.0, -0.6, 0.0]]
    best = None
    for s in seeds:
        q, pe, re = _dls_ik(_niryo_fk_adapter, s, np.asarray(target_pos, dtype=float), target_R,
                            max_iters, 5e-4, 1e-2, rot_weight=0.7)
        score = pe + 0.1 * re + 0.002 * float(np.linalg.norm(np.array(q) - np.array(q_seed)))
        if best is None or score < best[0]:
            best = (score, q, pe, re)
        if pe < 2e-3 and re < 2e-2:
            break
    return best[1], best[2], best[3]


def ik_niryo(q_seed: List[float], target_pose: List[float], max_iters: int = 80) -> List[float]:
    """Backward-compatible wrapper (pose = [x, y, z, roll, pitch, yaw])."""
    Rt = rpy_to_matrix(*target_pose[3:6]) if len(target_pose) >= 6 else np.eye(3)
    q, _, _ = ik_niryo_full(q_seed, target_pose[:3], Rt, max_iters)
    return [float(x) for x in q]


# ---------------------------------------------------------------------------
# Velocity profiles
# ---------------------------------------------------------------------------

def trapezoid_time(dist: float, v: float, a: float) -> float:
    dist, v, a = abs(dist), max(abs(v), 1e-6), max(abs(a), 1e-6)
    if dist < 1e-9:
        return 0.0
    d_acc = v * v / (2 * a)
    if dist >= 2 * d_acc:
        return dist / v + v / a
    return 2.0 * math.sqrt(dist / a)


def trapezoid_fraction(tau: float, dist: float, v: float, a: float) -> float:
    """Fraction (0..1) of the distance completed at time tau under a trapezoidal profile."""
    dist, v, a = abs(dist), max(abs(v), 1e-6), max(abs(a), 1e-6)
    T = trapezoid_time(dist, v, a)
    if dist < 1e-9 or tau >= T:
        return 1.0
    if tau <= 0:
        return 0.0
    d_acc = v * v / (2 * a)
    if dist >= 2 * d_acc:
        t_acc = v / a
        if tau < t_acc:
            s = 0.5 * a * tau * tau
        elif tau < T - t_acc:
            s = d_acc + v * (tau - t_acc)
        else:
            tr = T - tau
            s = dist - 0.5 * a * tr * tr
    else:
        if tau < T / 2:
            s = 0.5 * a * tau * tau
        else:
            tr = T - tau
            s = dist - 0.5 * a * tr * tr
    return float(np.clip(s / dist, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Timeline (shared by every mocked robot interface)
# ---------------------------------------------------------------------------

class Timeline:
    def __init__(self, tracker: List[Dict[str, Any]], project_dir_getter=None):
        self.tracker = tracker
        self.t = 0.0
        self.commands: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.last_q: Optional[List[float]] = None
        self.last_pose: Optional[List[float]] = None
        self.model = "ur"
        self.ik_deadline = time.monotonic() + IK_TIME_BUDGET_SEC
        self.project_dir_getter = project_dir_getter or (lambda: None)

    # ----- diagnostics -----
    def warn(self, msg: str):
        if msg not in self.warnings and len(self.warnings) < 60:
            self.warnings.append(msg)

    def ik_allowed(self) -> bool:
        return time.monotonic() < self.ik_deadline

    def _caller_location(self) -> Tuple[str, int]:
        """Finds the student's source file/line that issued the current robot command."""
        proj = self.project_dir_getter()
        f = sys._getframe(2)
        while f is not None:
            fn = f.f_code.co_filename
            if fn == "<string>" or (proj and fn.startswith(proj)):
                return (os.path.basename(fn) if fn != "<string>" else "<entry>", f.f_lineno)
            f = f.f_back
        return ("", 0)

    # ----- command bookkeeping -----
    def begin_command(self, ctype: str, label: str) -> Dict[str, Any]:
        fname, line = self._caller_location()
        cmd = {
            "index": len(self.commands), "type": ctype, "label": label,
            "file": fname, "line": line, "t0": self.t, "t1": self.t,
            "distance": 0.0, "max_speed": 0.0, "robot_model": self.model,
        }
        self.commands.append(cmd)
        return cmd

    def push(self, wp: Dict[str, Any]):
        if len(self.tracker) >= MAX_WAYPOINTS or self.t > MAX_SIM_SECONDS:
            raise RuntimeError(
                "Simulation limit exceeded (too many motions / too long). Please check for infinite loops in your code.")
        wp["t"] = round(self.t, 4)
        wp.setdefault("robot_model", self.model)
        if self.commands:
            wp.setdefault("cmd", self.commands[-1]["index"])
        self.tracker.append(wp)

    def start_waypoint(self, model: str, q: List[float], pose: List[float]):
        self.model = model
        self.last_q, self.last_pose = list(q), list(pose)
        self.tracker.append({"type": "start", "robot_model": model, "q": list(q), "pose": list(pose),
                             "t": 0.0, "label": "Initial pose", "is_final": True, "phase": "start"})

    def wait(self, seconds: float, label: Optional[str] = None):
        s = max(0.0, float(seconds))
        if s < 0.01 or self.last_q is None:
            self.t += s
            return
        cmd = self.begin_command("wait", label or f"wait {s:.2f}s")
        self.t += s
        cmd["t1"] = self.t
        self.push({"type": "wait", "q": list(self.last_q), "pose": list(self.last_pose or []),
                   "label": label or f"wait {s:.2f}s", "is_final": True, "phase": "wait", "duration": s})

    def io(self, label: str, extra: Optional[Dict[str, Any]] = None):
        if self.last_q is None:
            return
        cmd = self.begin_command("io", label)
        wp = {"type": "io", "q": list(self.last_q), "pose": list(self.last_pose or []),
              "label": label, "is_final": True, "phase": "io"}
        if extra:
            wp.update(extra)
        self.push(wp)
        cmd["t1"] = self.t

    def motion(self, ctype: str, label: str, duration: float, state_fn, speed: float, accel: float,
               commit_fn=None):
        """
        Generic motion sampler.  `state_fn(tau_fraction_time)` -> (q, pose, extra) for a time `tau` in [0, duration].
        """
        cmd = self.begin_command(ctype, label)
        n = 1 if duration < 1e-6 else int(np.clip(math.ceil(duration * SAMPLE_HZ), MIN_STEPS, MAX_STEPS))
        t_start = self.t
        prev_pose = self.last_pose
        dist = 0.0
        for k in range(1, n + 1):
            tau = duration * k / n
            q, pose, extra = state_fn(tau)
            self.t = t_start + tau
            is_final = (k == n)
            wp = {"type": ctype, "q": [float(x) for x in q], "pose": [float(x) for x in pose],
                  "label": label if is_final else f"{ctype} {k}/{n}", "is_final": is_final,
                  "speed": float(speed), "accel": float(accel), "phase": "motion"}
            if extra:
                wp.update(extra)
            self.push(wp)
            if prev_pose:
                dist += float(np.linalg.norm(np.array(pose[:3]) - np.array(prev_pose[:3])))
            prev_pose = pose
            self.last_q, self.last_pose = list(q), list(pose)
        cmd["t1"] = self.t
        cmd["distance"] = dist
        cmd["duration"] = cmd["t1"] - cmd["t0"]
        cmd["max_speed"] = (dist / cmd["duration"]) if cmd["duration"] > 0 else 0.0
        if commit_fn:
            commit_fn()
        return cmd


# ---------------------------------------------------------------------------
# UR mocks
# ---------------------------------------------------------------------------

class URState:
    """Shared mutable state between the mocked RTDE control / receive / io interfaces."""
    def __init__(self):
        self.q = list(UR_HOME_Q)
        self.tcp = [0.0] * 6
        self.speed_scale = 1.0


class MockRTDEReceive:
    def __init__(self, state: URState, ip: str = "192.168.56.101"):
        self.ip = ip
        self.state = state

    @property
    def current_q(self) -> List[float]:
        return self.state.q

    @current_q.setter
    def current_q(self, v):
        self.state.q = list(v)

    def getActualTCPPose(self) -> List[float]:
        return get_tcp_pose_list(self.state.q, self.state.tcp)

    def getActualQ(self) -> List[float]:
        return list(self.state.q)

    def getTargetTCPPose(self) -> List[float]:
        return get_tcp_pose_list(self.state.q, self.state.tcp)

    def getTargetQ(self) -> List[float]:
        return list(self.state.q)

    def getActualQd(self) -> List[float]:
        return [0.0] * 6

    def getActualTCPSpeed(self) -> List[float]:
        return [0.0] * 6

    def getActualTCPForce(self) -> List[float]:
        return [0.0] * 6

    def getJointTemperatures(self) -> List[float]:
        return [32.0, 31.5, 33.0, 29.5, 30.0, 28.5]

    def getSafetyMode(self) -> int:
        return 1

    def getRobotMode(self) -> int:
        return 7

    def isProtectiveStopped(self) -> bool:
        return False

    def isEmergencyStopped(self) -> bool:
        return False

    def isConnected(self) -> bool:
        return True

    def disconnect(self):
        pass

    def reconnect(self) -> bool:
        return True

    def __getattr__(self, name: str):
        return lambda *a, **kw: 0.0


class MockRTDEControl:
    def __init__(self, timeline: Timeline, state: URState):
        self.tl = timeline
        self.state = state

    # --- helpers ---
    def _scaled(self, speed: float, accel: float) -> Tuple[float, float]:
        k = max(self.state.speed_scale, 0.01)
        return abs(float(speed)) * k, abs(float(accel)) * k * k if k < 1 else abs(float(accel))

    def _tcp_pose(self, q) -> List[float]:
        return get_tcp_pose_list(q, self.state.tcp)

    # --- joint space ---
    def moveJ(self, q: Any, speed: float = 1.05, acceleration: float = 1.4, asynchronous: bool = False):
        # Path form: [[q0..q5, v, a, blend], ...]
        if isinstance(q, (list, tuple, np.ndarray)) and len(q) > 0 and isinstance(q[0], (list, tuple, np.ndarray)):
            for wp in q:
                v = wp[6] if len(wp) > 6 else speed
                a = wp[7] if len(wp) > 7 else acceleration
                self.moveJ(list(wp[:6]), v, a)
            return True

        q_target = [float(x) for x in q]
        if len(q_target) != 6:
            raise ValueError(f"moveJ expects 6 joint angles (got {len(q_target)})")
        for j, v in enumerate(q_target):
            if abs(v) > UR_JOINT_LIMIT + 1e-3:
                self.tl.warn(f"moveJ joint {j + 1} target {v:.2f} rad exceeds +/-2*pi — the real UR3 would refuse this move.")

        q_start = list(self.state.q)
        dq = np.array(q_target) - np.array(q_start)
        dist = float(np.max(np.abs(dq)))
        v, a = self._scaled(speed, acceleration)
        T = trapezoid_time(dist, v, a)

        def state_fn(tau):
            s = trapezoid_fraction(tau, dist, v, a)
            qi = np.array(q_start) + dq * s
            return list(qi), self._tcp_pose(qi), None

        label = f"moveJ({[round(x, 2) for x in q_target]})"

        def commit():
            self.state.q = list(q_target)
        self.tl.motion("moveJ", label, T, state_fn, speed, acceleration, commit)
        self.state.q = list(q_target)
        return True

    def moveJ_IK(self, pose: Any, speed: float = 1.05, acceleration: float = 1.4, asynchronous: bool = False):
        q = self.getInverseKinematics(pose)
        return self.moveJ(q, speed, acceleration)

    # --- Cartesian ---
    def moveL(self, pose: Any, speed: float = 0.25, acceleration: float = 1.2, asynchronous: bool = False):
        if isinstance(pose, (list, tuple, np.ndarray)) and len(pose) > 0 and isinstance(pose[0], (list, tuple, np.ndarray)):
            for wp in pose:
                v = wp[6] if len(wp) > 6 else speed
                a = wp[7] if len(wp) > 7 else acceleration
                self.moveL(list(wp[:6]), v, a)
            return True

        target = [float(x) for x in pose]
        if len(target) != 6:
            raise ValueError(f"moveL expects a pose [x, y, z, rx, ry, rz] (got {len(target)} values)")

        q_start = list(self.state.q)
        p0, R0 = ur_tcp_pose(q_start, self.state.tcp)
        p1 = np.array(target[:3])
        R1 = rotvec_to_matrix(np.array(target[3:6]))
        dpos = p1 - p0
        dist = float(np.linalg.norm(dpos))
        ang = rotation_angle(R0, R1)
        v, a = self._scaled(speed, acceleration)
        T_pos = trapezoid_time(dist, v, a)
        T_rot = trapezoid_time(ang, v * 4.0, a * 4.0)
        T = max(T_pos, T_rot)
        if T_pos >= T_rot:
            prof = (dist, v, a)
        else:
            prof = (ang, v * 4.0, a * 4.0)

        prev_q = [list(q_start)]
        worst = [0.0]
        failed = [False]

        def state_fn(tau):
            s = trapezoid_fraction(tau, *prof) if T > 0 else 1.0
            pos = p0 + dpos * s
            R = slerp_matrix(R0, R1, s)
            if self.tl.ik_allowed():
                q, pe, re = ik_ur(prev_q[0], pos, R, tcp=self.state.tcp, max_iters=40 if tau > 0 else 80)
                worst[0] = max(worst[0], pe)
                if pe > 0.004 or re > 0.03:
                    failed[0] = True
                prev_q[0] = list(q)
            else:
                q = prev_q[0]
                failed[0] = True
            return list(q), self._tcp_pose(q), None

        label = f"moveL([X={target[0]:.3f}, Y={target[1]:.3f}, Z={target[2]:.3f}])"
        self.tl.motion("moveL", label, T, state_fn, speed, acceleration)
        if failed[0]:
            self.tl.warn(
                f"{label}: target is unreachable or passes through a singularity "
                f"(IK error up to {worst[0] * 1000:.1f} mm). The real UR3 would stop with a protective/safety fault.")
        self.state.q = list(prev_q[0])
        return True

    def moveL_FK(self, q: Any, speed: float = 0.25, acceleration: float = 1.2, asynchronous: bool = False):
        return self.moveL(get_tcp_pose_list([float(x) for x in q], self.state.tcp), speed, acceleration)

    # --- continuous / servo style commands: approximated as a single bounded move ---
    def servoJ(self, q, speed=0.5, acceleration=0.5, time_=0.002, lookahead_time=0.1, gain=300):
        return self.moveJ(q, max(float(speed), 0.5), max(float(acceleration), 0.5))

    def servoL(self, pose, speed=0.25, acceleration=1.2, time_=0.002, lookahead_time=0.1, gain=300):
        return self.moveL(pose, max(float(speed), 0.1), max(float(acceleration), 0.5))

    def speedJ(self, qd, acceleration=0.5, time=0.0):
        dur = float(time) if time else 0.5
        q_start = np.array(self.state.q)
        qd = np.array([float(x) for x in qd])
        a = max(float(acceleration), 0.1)

        def state_fn(tau):
            qi = q_start + qd * tau
            return list(qi), self._tcp_pose(qi), None
        self.tl.motion("speedJ", f"speedJ(max {float(np.max(np.abs(qd))):.2f} rad/s, {dur:.1f}s)", dur, state_fn, float(np.max(np.abs(qd))), a)
        self.state.q = list(q_start + qd * dur)
        return True

    def speedL(self, xd, acceleration=0.25, time=0.0):
        dur = float(time) if time else 0.5
        xd = np.array([float(x) for x in xd])
        q0 = list(self.state.q)
        p0, R0 = ur_tcp_pose(q0, self.state.tcp)
        prev = [q0]

        def state_fn(tau):
            pos = p0 + xd[:3] * tau
            R = rotvec_to_matrix(xd[3:6] * tau) @ R0
            q, _, _ = ik_ur(prev[0], pos, R, tcp=self.state.tcp, max_iters=30) if self.tl.ik_allowed() else (prev[0], 0, 0)
            prev[0] = list(q)
            return list(q), self._tcp_pose(q), None
        self.tl.motion("speedL", f"speedL(|v|={float(np.linalg.norm(xd[:3])):.2f} m/s, {dur:.1f}s)", dur, state_fn,
                       float(np.linalg.norm(xd[:3])), float(acceleration))
        self.state.q = list(prev[0])
        return True

    def stopJ(self, a=2.0):
        return True

    def stopL(self, a=10.0):
        return True

    def stopScript(self):
        self.tl.io("stopScript()", {"type": "stopScript"})
        return True

    # --- kinematics queries (return real solutions instead of the old `True` fallback) ---
    def getInverseKinematics(self, pose, qnear=None, max_position_error=1e-10, max_orientation_error=1e-10):
        target = [float(x) for x in pose]
        seed = [float(x) for x in qnear] if qnear else list(self.state.q)
        q, pe, re = ik_ur(seed, np.array(target[:3]), rotvec_to_matrix(np.array(target[3:6])),
                          tcp=self.state.tcp, multi_seed=True, max_iters=120)
        if pe > 0.003:
            self.tl.warn(f"getInverseKinematics: pose is not reachable (error {pe * 1000:.1f} mm).")
        return [float(x) for x in q]

    def getForwardKinematics(self, q=None, tcp_offset=None):
        qq = [float(x) for x in q] if q else list(self.state.q)
        return get_tcp_pose_list(qq, tcp_offset if tcp_offset else self.state.tcp)

    def isPoseWithinSafetyLimits(self, pose):
        return True

    def isJointsWithinSafetyLimits(self, q):
        return all(abs(float(v)) <= UR_JOINT_LIMIT for v in q)

    def setTcp(self, tcp_offset):
        self.state.tcp = [float(x) for x in tcp_offset]
        return True

    def getTCPOffset(self):
        return list(self.state.tcp)

    def setSpeedSlider(self, speed):
        self.state.speed_scale = float(np.clip(speed, 0.01, 1.0))
        return True

    def setStandardDigitalOut(self, pin: int, value: bool):
        self.tl.io(f"DigitalOut({pin}={bool(value)})", {"type": "digitalOut", "pin": pin, "value": bool(value)})
        return True

    def setToolDigitalOut(self, pin: int, value: bool):
        self.tl.io(f"ToolDigitalOut({pin}={bool(value)})", {"type": "digitalOut", "pin": pin, "value": bool(value)})
        return True

    def isConnected(self) -> bool:
        return True

    def isSteady(self) -> bool:
        return True

    def teachMode(self):
        return True

    def endTeachMode(self):
        return True

    def zeroFtSensor(self):
        return True

    def disconnect(self):
        pass

    def reconnect(self) -> bool:
        return True

    def __getattr__(self, name: str):
        # Any other ur_rtde control call is accepted as a no-op
        return lambda *a, **kw: True


class MockRTDEIO:
    """Mock for ur_rtde RTDEIOInterface so simulated digital I/O never touches hardware."""
    def __init__(self, timeline: Timeline, ip: str = "127.0.0.1"):
        self.tl = timeline
        self.ip = ip
        self.pins: Dict[int, bool] = {}

    def setStandardDigitalOut(self, pin: int, value: bool) -> bool:
        self.pins[pin] = bool(value)
        self.tl.io(f"DigitalOut({pin}={bool(value)})", {"type": "digitalOut", "pin": pin, "value": bool(value)})
        return True

    def setToolDigitalOut(self, pin: int, value: bool) -> bool:
        self.tl.io(f"ToolDigitalOut({pin}={bool(value)})", {"type": "digitalOut", "pin": pin, "value": bool(value)})
        return True

    def getStandardDigitalOut(self, pin: int) -> bool:
        return self.pins.get(pin, False)

    def getToolDigitalOut(self, pin: int) -> bool:
        return False

    def __getattr__(self, name: str):
        return lambda *a, **kw: True


# ---------------------------------------------------------------------------
# Niryo mocks
# ---------------------------------------------------------------------------

class MockNiryoPose:
    def __init__(self, x=0.0, y=0.0, z=0.0, roll=0.0, pitch=0.0, yaw=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.roll = float(roll)
        self.pitch = float(pitch)
        self.yaw = float(yaw)

    def to_list(self) -> List[float]:
        return [self.x, self.y, self.z, self.roll, self.pitch, self.yaw]

    def copy_with_offsets(self, x_offset=0.0, y_offset=0.0, z_offset=0.0, roll_offset=0.0, pitch_offset=0.0, yaw_offset=0.0):
        return MockNiryoPose(self.x + x_offset, self.y + y_offset, self.z + z_offset,
                             self.roll + roll_offset, self.pitch + pitch_offset, self.yaw + yaw_offset)

    def __iter__(self):
        return iter(self.to_list())

    def __getitem__(self, item):
        return self.to_list()[item]

    def __len__(self):
        return 6

    def __repr__(self):
        return (f"Pose(x={self.x:.3f}, y={self.y:.3f}, z={self.z:.3f}, "
                f"roll={self.roll:.3f}, pitch={self.pitch:.3f}, yaw={self.yaw:.3f})")


class MockRobotAxis:
    X, Y, Z, ROLL, PITCH, YAW = 0, 1, 2, 3, 4, 5


def _pose_list(args) -> List[float]:
    if len(args) == 1 and hasattr(args[0], "to_list"):
        return [float(v) for v in args[0].to_list()]
    if len(args) == 1 and isinstance(args[0], (list, tuple, np.ndarray)):
        return [float(v) for v in args[0]]
    if len(args) >= 6:
        return [float(v) for v in args[:6]]
    raise ValueError("A Niryo pose needs 6 values: x, y, z, roll, pitch, yaw")


class MockNiryoRobot:
    def __init__(self, timeline: Timeline, ip: str = "127.0.0.1"):
        self.tl = timeline
        self.ip = ip
        self.current_q = list(NIRYO_HOME_Q)
        self.is_calibrated = False
        self.velocity = 100.0          # percent (set_arm_max_velocity)
        self.gripper_open = True
        self.learning_mode = False

    # --- internals ---
    def _pose_of(self, q) -> List[float]:
        pos, rpy, _, _, _ = fk_niryo(q)
        return [float(x) for x in np.concatenate([pos, rpy])]

    def _joint_dyn(self) -> Tuple[float, float]:
        k = max(self.velocity, 1.0) / 100.0
        return NIRYO_MAX_JOINT_SPEED * k, NIRYO_MAX_JOINT_ACCEL * k

    def _lin_dyn(self) -> Tuple[float, float]:
        k = max(self.velocity, 1.0) / 100.0
        return NIRYO_MAX_LINEAR_SPEED * k, NIRYO_MAX_LINEAR_ACCEL * k

    def _check_limits(self, q_target: List[float]):
        for j, (lo, hi) in enumerate(NIRYO_LIMITS):
            if q_target[j] < lo - 0.02 or q_target[j] > hi + 0.02:
                self.tl.warn(
                    f"Niryo joint {j + 1} target {q_target[j]:.2f} rad is outside the typical Ned range "
                    f"[{lo:.2f}, {hi:.2f}] — the real robot would raise an error.")

    def _joint_move(self, q_target: List[float], label: str, ctype: str = "move_joints"):
        self._check_limits(q_target)
        q_start = list(self.current_q)
        dq = np.array(q_target) - np.array(q_start)
        dist = float(np.max(np.abs(dq)))
        v, a = self._joint_dyn()
        T = trapezoid_time(dist, v, a)

        def state_fn(tau):
            s = trapezoid_fraction(tau, dist, v, a)
            qi = np.array(q_start) + dq * s
            return list(qi), self._pose_of(qi), None
        self.tl.motion(ctype, label, T, state_fn, v, a)
        self.current_q = list(q_target)
        return True

    # --- calibration / learning mode / misc ---
    def calibrate_auto(self):
        self.is_calibrated = True
        self.tl.wait(2.0, "calibrate_auto()")
        return True

    def calibrate_manual(self):
        self.is_calibrated = True
        return True

    def need_calibration(self):
        return False

    def set_learning_mode(self, enabled):
        self.learning_mode = bool(enabled)
        return True

    def get_learning_mode(self):
        return self.learning_mode

    def set_arm_max_velocity(self, percent: int):
        p = float(percent)
        if p < 1 or p > 100:
            raise ValueError("Niryo set_arm_max_velocity expects a value between 1 and 100 (%).")
        self.velocity = p
        self.tl.io(f"set_arm_max_velocity({int(p)}%)")
        return True

    def wait(self, duration: float):
        self.tl.wait(float(duration), f"wait({float(duration):.2f}s)")
        return True

    def move_to_home_pose(self):
        return self._joint_move(list(NIRYO_HOME_Q), "move_to_home_pose()")

    def go_to_sleep(self):
        return self._joint_move(list(NIRYO_HOME_Q), "go_to_sleep()")

    # --- joint space ---
    def move_joints(self, *joints: Any):
        if len(joints) == 1 and isinstance(joints[0], (list, tuple, np.ndarray)):
            joints = tuple(joints[0])
        q_target = [float(x) for x in joints]
        if len(q_target) != 6:
            raise ValueError(f"move_joints expects 6 joint values (got {len(q_target)})")
        return self._joint_move(q_target, f"move_joints({[round(x, 2) for x in q_target]})")

    def jog_joints(self, *joints: Any):
        if len(joints) == 1 and isinstance(joints[0], (list, tuple, np.ndarray)):
            joints = tuple(joints[0])
        q_target = [self.current_q[i] + float(joints[i]) for i in range(6)]
        return self._joint_move(q_target, "jog_joints()", "jog")

    # --- Cartesian ---
    def _solve_pose(self, pose: List[float]) -> List[float]:
        Rt = rpy_to_matrix(*pose[3:6])
        q, pe, re = ik_niryo_full(self.current_q, pose[:3], Rt, multi_seed=True) if self.tl.ik_allowed() \
            else (self.current_q, 0.0, 0.0)
        if pe > 0.006 or re > 0.08:
            raise RuntimeError(
                f"Niryo move_pose: pose [{', '.join(f'{v:.3f}' for v in pose)}] is not reachable "
                f"(IK error {pe * 1000:.0f} mm / {math.degrees(re):.0f} deg).")
        return [float(x) for x in q]

    def move_pose(self, *args):
        pose = _pose_list(args)
        q_target = self._solve_pose(pose)
        return self._joint_move(q_target, f"move_pose([{', '.join(f'{v:.2f}' for v in pose[:3])}])", "move_pose")

    def move_linear_pose(self, *args):
        target = _pose_list(args)
        q_start = list(self.current_q)
        pos0, _, R0, _, _ = fk_niryo(q_start)
        p1 = np.array(target[:3])
        R1 = rpy_to_matrix(*target[3:6])
        dpos = p1 - pos0
        dist = float(np.linalg.norm(dpos))
        ang = rotation_angle(R0, R1)
        v, a = self._lin_dyn()
        T_pos = trapezoid_time(dist, v, a)
        T_rot = trapezoid_time(ang, v * 5.0, a * 5.0)
        T = max(T_pos, T_rot)
        prof = (dist, v, a) if T_pos >= T_rot else (ang, v * 5.0, a * 5.0)
        prev = [q_start]
        worst = [0.0]
        failed = [False]

        def state_fn(tau):
            s = trapezoid_fraction(tau, *prof) if T > 0 else 1.0
            pos = pos0 + dpos * s
            R = slerp_matrix(R0, R1, s)
            if self.tl.ik_allowed():
                q, pe, re = ik_niryo_full(prev[0], pos, R, max_iters=50, multi_seed=False)
                worst[0] = max(worst[0], pe)
                if pe > 0.006 or re > 0.08:
                    failed[0] = True
                prev[0] = [float(x) for x in q]
            return list(prev[0]), self._pose_of(prev[0]), None

        label = f"move_linear_pose([{', '.join(f'{x:.2f}' for x in target[:3])}])"
        self.tl.motion("move_linear_pose", label, T, state_fn, v, a)
        if failed[0]:
            self.tl.warn(f"{label}: part of the straight-line path is unreachable (IK error up to {worst[0] * 1000:.0f} mm).")
        self._check_limits(prev[0])
        self.current_q = list(prev[0])
        return True

    def shift_pose(self, axis, value):
        return self._shift(axis, value, linear=False)

    def shift_linear_pose(self, axis, value):
        return self._shift(axis, value, linear=True)

    def _shift(self, axis, value, linear: bool):
        pose = self._pose_of(self.current_q)
        idx = int(getattr(axis, "value", axis))
        if not 0 <= idx <= 5:
            raise ValueError("Unknown RobotAxis")
        pose[idx] += float(value)
        return self.move_linear_pose(pose) if linear else self.move_pose(pose)

    def pick_from_pose(self, *args):
        pose = _pose_list(args)
        above = list(pose)
        above[2] += 0.05
        self.open_gripper(500)
        self.move_pose(above)
        self.move_linear_pose(pose)
        self.close_gripper(500)
        self.move_linear_pose(above)
        return True

    def place_from_pose(self, *args):
        pose = _pose_list(args)
        above = list(pose)
        above[2] += 0.05
        self.move_pose(above)
        self.move_linear_pose(pose)
        self.open_gripper(500)
        self.move_linear_pose(above)
        return True

    def get_joints(self) -> List[float]:
        return list(self.current_q)

    def get_pose(self) -> MockNiryoPose:
        return MockNiryoPose(*self._pose_of(self.current_q))

    # --- tools ---
    def _gripper(self, state: str, speed: int):
        self.gripper_open = (state == "open")
        label = f"{'open' if self.gripper_open else 'close'}_gripper({speed})"
        self.tl.io(label, {"type": "gripper", "state": state})
        self.tl.wait(0.5, f"{label} (actuation)")
        return True

    def open_gripper(self, speed: int = 500, *a, **k):
        return self._gripper("open", speed)

    def close_gripper(self, speed: int = 500, *a, **k):
        return self._gripper("closed", speed)

    def grasp_with_tool(self):
        return self._gripper("closed", 500)

    def release_with_tool(self):
        return self._gripper("open", 500)

    def update_tool(self):
        return True

    def close_connection(self):
        pass

    def end(self):
        pass

    def __getattr__(self, name: str):
        return lambda *a, **kw: True


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def _decimate(rows: List[List[float]], limit: int = 400) -> List[List[float]]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / float(limit)
    return [rows[int(i * step)] for i in range(limit)] + [rows[-1]]


def build_analytics(waypoints: List[Dict[str, Any]], timeline: Timeline, model: str) -> Dict[str, Any]:
    """Derives speed profile, path length, joint ranges, bounds & per-command stats from the trajectory."""
    total_t = timeline.t
    motion_t = sum(c["duration"] for c in timeline.commands if c.get("duration") and c["type"] not in ("wait",))
    wait_t = sum(max(0.0, c["t1"] - c["t0"]) for c in timeline.commands if c["type"] == "wait")

    speed_profile: List[List[float]] = []
    joint_speed_profile: List[List[float]] = []
    path_len = 0.0
    max_speed = 0.0
    max_jspeed = 0.0
    prev = None
    qmin = [float("inf")] * 6
    qmax = [float("-inf")] * 6
    pmin = [float("inf")] * 3
    pmax = [float("-inf")] * 3

    for wp in waypoints:
        q = wp.get("q")
        pose = wp.get("pose")
        if q and len(q) == 6:
            for j in range(6):
                qmin[j] = min(qmin[j], q[j])
                qmax[j] = max(qmax[j], q[j])
        if pose and len(pose) >= 3:
            for j in range(3):
                pmin[j] = min(pmin[j], pose[j])
                pmax[j] = max(pmax[j], pose[j])
        if prev is not None and wp.get("t", 0) - prev.get("t", 0) > 1e-4 and pose and prev.get("pose"):
            dt = wp["t"] - prev["t"]
            d = float(np.linalg.norm(np.array(pose[:3]) - np.array(prev["pose"][:3])))
            sp = d / dt
            wp["tcp_speed"] = sp
            if q and prev.get("q"):
                js = float(np.max(np.abs(np.array(q) - np.array(prev["q"])))) / dt
                wp["joint_speed"] = js
                max_jspeed = max(max_jspeed, js)
                joint_speed_profile.append([wp["t"], js])
            path_len += d
            max_speed = max(max_speed, sp)
            speed_profile.append([wp["t"], sp])
        elif prev is not None:
            wp["tcp_speed"] = 0.0
            if wp.get("phase") in ("wait", "io"):
                speed_profile.append([wp["t"], 0.0])
        prev = wp

    avg_speed = (path_len / motion_t) if motion_t > 0 else 0.0
    counts: Dict[str, int] = {}
    for c in timeline.commands:
        counts[c["type"]] = counts.get(c["type"], 0) + 1

    has_bounds = pmin[0] != float("inf")
    return {
        "robot_model": model,
        "total_time": total_t,
        "motion_time": motion_t,
        "wait_time": wait_t,
        "path_length": path_len,
        "max_tcp_speed": max_speed,
        "avg_tcp_speed": avg_speed,
        "max_joint_speed": max_jspeed,
        "command_count": len(timeline.commands),
        "command_counts": counts,
        "waypoint_count": len(waypoints),
        "joint_min": [v if v != float("inf") else 0.0 for v in qmin],
        "joint_max": [v if v != float("-inf") else 0.0 for v in qmax],
        "bounds": {"min": pmin if has_bounds else [0, 0, 0], "max": pmax if has_bounds else [0, 0, 0]},
        "min_z": pmin[2] if has_bounds else 0.0,
        "speed_profile": _decimate(speed_profile),
        "joint_speed_profile": _decimate(joint_speed_profile),
        "commands": timeline.commands,
        "warnings": timeline.warnings,
    }


# ---------------------------------------------------------------------------
# Script simulation
# ---------------------------------------------------------------------------

def simulate_script(
    code_str: str,
    robot_model: Optional[str] = None,
    files: Optional[Dict[str, str]] = None,
    entry_point: Optional[str] = "main.py"
) -> Dict[str, Any]:
    """
    Simulates Python code in an isolated scope with accurate UR3 & Niryo kinematics and timing.
    Supports multi-file projects where entry_point imports other project modules.
    Returns time-stamped waypoints, analytics and captured output logs.
    Guarantees no physical robot I/O or network connections are triggered during simulation.
    """
    # Detect robot model from code content, project files, or explicit model hint
    all_code_text = code_str + " " + " ".join((files or {}).values())
    if "pyniryo" in all_code_text or "NiryoRobot" in all_code_text:
        detected_model = "niryo"
    elif ("rtde_control" in all_code_text or "rtde_receive" in all_code_text or "rtde_io" in all_code_text
          or "RTDEControl" in all_code_text or "RTDEIO" in all_code_text or "ur_wrapper" in all_code_text):
        detected_model = "ur"
    elif robot_model in ("ur", "niryo"):
        detected_model = robot_model
    else:
        detected_model = "ur"

    waypoints: List[Dict[str, Any]] = []
    holder = {"dir": None}
    timeline = Timeline(waypoints, project_dir_getter=lambda: holder["dir"])
    ur_state = URState()
    receiver = MockRTDEReceive(ur_state)
    controller = MockRTDEControl(timeline, ur_state)
    io_interface = MockRTDEIO(timeline)
    niryo_robot = MockNiryoRobot(timeline)
    captured_logs: List[str] = []

    if detected_model == "niryo":
        timeline.start_waypoint("niryo", NIRYO_HOME_Q, niryo_robot._pose_of(NIRYO_HOME_Q))
    else:
        timeline.start_waypoint("ur", UR_HOME_Q, get_tcp_pose_list(UR_HOME_Q))

    def mock_print(*args, **kwargs):
        captured_logs.append(" ".join(str(a) for a in args))

    def mock_printf(*args, **kwargs):
        if len(args) > 1 and isinstance(args[0], str) and ("%" in args[0]):
            try:
                captured_logs.append(args[0] % args[1:])
                return
            except Exception:
                pass
        captured_logs.append(" ".join(str(a) for a in args))

    import builtins
    import socket

    sim_builtins = dict(builtins.__dict__)
    sim_builtins["print"] = mock_print
    sim_builtins["printf"] = mock_printf

    sleep_counter = [0]

    def safe_mock_sleep(s):
        sleep_counter[0] += 1
        if sleep_counter[0] > 5000:
            raise RuntimeError("Simulation execution limit exceeded (> 5000 delays/steps). Please check for infinite while loops in your code.")
        try:
            timeline.wait(float(s))
        except (TypeError, ValueError):
            pass

    # Mock modules in sys.modules so imports resolve seamlessly without hitting real hardware
    mock_ctrl_mod = types.ModuleType("rtde_control")
    mock_ctrl_mod.RTDEControlInterface = lambda *a, **kw: controller
    mock_ctrl_mod.RTDEControl = lambda *a, **kw: controller

    mock_recv_mod = types.ModuleType("rtde_receive")
    mock_recv_mod.RTDEReceiveInterface = lambda *a, **kw: receiver
    mock_recv_mod.RTDEReceive = lambda *a, **kw: receiver

    mock_io_mod = types.ModuleType("rtde_io")
    mock_io_mod.RTDEIOInterface = lambda *a, **kw: io_interface
    mock_io_mod.RTDEIO = lambda *a, **kw: io_interface

    mock_ur_rtde_mod = types.ModuleType("ur_rtde")
    mock_ur_rtde_mod.RTDEControlInterface = lambda *a, **kw: controller
    mock_ur_rtde_mod.RTDEReceiveInterface = lambda *a, **kw: receiver
    mock_ur_rtde_mod.RTDEIOInterface = lambda *a, **kw: io_interface
    mock_ur_rtde_mod.rtde_control = mock_ctrl_mod
    mock_ur_rtde_mod.rtde_receive = mock_recv_mod
    mock_ur_rtde_mod.rtde_io = mock_io_mod

    # Mock pyniryo module
    mock_niryo_mod = types.ModuleType("pyniryo")
    mock_niryo_mod.NiryoRobot = lambda *a, **kw: niryo_robot
    mock_niryo_mod.PoseObject = MockNiryoPose
    mock_niryo_mod.RobotAxis = MockRobotAxis

    class _AnyEnum:
        """Placeholder for pyniryo enums/classes we do not model (ToolID, PinMode, ...)."""
        def __getattr__(self, name):
            return name

    def _niryo_getattr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _AnyEnum()

    mock_niryo_mod.__getattr__ = _niryo_getattr

    class SafetyViolationError(Exception):
        pass

    class MockRobotArm:
        """Mirror of ur_wrapper.RobotArm (same clamping + workspace envelope) running against the simulator."""
        def __init__(self, *a, **kw):
            self.is_simulated = True

        def movej(self, q, a=0.5, v=0.3):
            a = min(abs(a), WRAP_MAX_JOINT_ACCEL)
            v = min(abs(v), WRAP_MAX_JOINT_SPEED)
            if len(q) != 6:
                raise ValueError(f"Joint target must contain 6 angles (got {len(q)})")
            return controller.moveJ(q, v, a)

        def movel(self, pose, a=0.3, v=0.2):
            if len(pose) != 6:
                raise ValueError(f"Cartesian pose must contain 6 elements [x, y, z, rx, ry, rz] (got {len(pose)})")
            x, y, z = pose[0], pose[1], pose[2]
            r = math.sqrt(x * x + y * y)
            if z < WRAP_Z_MIN:
                raise SafetyViolationError(f"Target Z={z:.3f}m is below safe table limit (min: {WRAP_Z_MIN}m). Collision risk!")
            if z > WRAP_Z_MAX:
                raise SafetyViolationError(f"Target Z={z:.3f}m exceeds upper ceiling limit (max: {WRAP_Z_MAX}m).")
            if r > WRAP_R_MAX:
                raise SafetyViolationError(f"Target radial distance R={r:.3f}m exceeds max radius limit ({WRAP_R_MAX}m).")
            a = min(abs(a), WRAP_MAX_LINEAR_ACCEL)
            v = min(abs(v), WRAP_MAX_LINEAR_SPEED)
            return controller.moveL(pose, v, a)

        def set_digital_out(self, pin, value):
            return io_interface.setStandardDigitalOut(pin, value)

        def sleep(self, s):
            safe_mock_sleep(min(max(0.0, s), 10.0))

        def get_actual_joint_positions(self):
            return receiver.getActualQ()

        def get_actual_tcp_pose(self):
            return receiver.getActualTCPPose()

        def stop(self):
            return controller.stopScript()

    mock_wrap_mod = types.ModuleType("ur_wrapper")
    mock_wrap_mod.RobotArm = MockRobotArm
    mock_wrap_mod.printf = mock_printf
    mock_wrap_mod.SafetyViolationError = SafetyViolationError
    mock_wrap_mod.MAX_JOINT_SPEED = WRAP_MAX_JOINT_SPEED
    mock_wrap_mod.MAX_JOINT_ACCEL = WRAP_MAX_JOINT_ACCEL
    mock_wrap_mod.MAX_LINEAR_SPEED = WRAP_MAX_LINEAR_SPEED
    mock_wrap_mod.MAX_LINEAR_ACCEL = WRAP_MAX_LINEAR_ACCEL

    # Mock socket during simulation so user code cannot reach physical controllers
    class MockSimSocket:
        def __init__(self, *args, **kwargs): pass
        def connect(self, *args, **kwargs): pass
        def sendall(self, *args, **kwargs): pass
        def send(self, *args, **kwargs): return 0
        def recv(self, *args, **kwargs): return b""
        def close(self): pass
        def settimeout(self, *args, **kwargs): pass
        def setsockopt(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    old_socket_cls = getattr(socket, "socket", None)
    old_socket_create = getattr(socket, "create_connection", None)
    socket.socket = MockSimSocket
    socket.create_connection = lambda *a, **kw: MockSimSocket()

    old_ctrl = sys.modules.get("rtde_control")
    old_recv = sys.modules.get("rtde_receive")
    old_io = sys.modules.get("rtde_io")
    old_ur_rtde = sys.modules.get("ur_rtde")
    old_wrap = sys.modules.get("ur_wrapper")
    old_niryo = sys.modules.get("pyniryo")

    old_time = sys.modules.get("time")
    mock_time_mod = types.ModuleType("time")
    for attr in dir(time):
        if not attr.startswith("__"):
            try:
                setattr(mock_time_mod, attr, getattr(time, attr))
            except Exception:
                pass
    mock_time_mod.sleep = safe_mock_sleep
    sys.modules["time"] = mock_time_mod

    sys.modules["rtde_control"] = mock_ctrl_mod
    sys.modules["rtde_receive"] = mock_recv_mod
    sys.modules["rtde_io"] = mock_io_mod
    sys.modules["ur_rtde"] = mock_ur_rtde_mod
    sys.modules["ur_rtde.rtde_control"] = mock_ctrl_mod
    sys.modules["ur_rtde.rtde_receive"] = mock_recv_mod
    sys.modules["ur_rtde.rtde_io"] = mock_io_mod
    sys.modules["ur_wrapper"] = mock_wrap_mod
    sys.modules["pyniryo"] = mock_niryo_mod

    sim_globals = {
        "__builtins__": sim_builtins,
        "np": np,
        "numpy": np,
        "math": math,
        "time": mock_time_mod,
        "RTDEControl": lambda *a, **kw: controller,
        "RTDEControlInterface": lambda *a, **kw: controller,
        "RTDEReceive": lambda *a, **kw: receiver,
        "RTDEReceiveInterface": lambda *a, **kw: receiver,
        "RTDEIO": lambda *a, **kw: io_interface,
        "RTDEIOInterface": lambda *a, **kw: io_interface,
        "rtde_control": mock_ctrl_mod,
        "rtde_receive": mock_recv_mod,
        "rtde_io": mock_io_mod,
        "NiryoRobot": lambda *a, **kw: niryo_robot,
        "pyniryo": mock_niryo_mod,
        "RobotArm": MockRobotArm,
        "printf": mock_printf,
    }

    temp_sim_dir = None
    output_files: Dict[str, str] = {}
    try:
        temp_sim_dir = tempfile.mkdtemp(prefix="sim_proj_")
        holder["dir"] = temp_sim_dir
        for fname, fcontent in (files or {}).items():
            clean_name = os.path.basename(fname.strip())
            if clean_name:
                mod_name = os.path.splitext(clean_name)[0]
                sys.modules.pop(mod_name, None)
                fpath = os.path.join(temp_sim_dir, clean_name)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(fcontent or "")
        if temp_sim_dir not in sys.path:
            sys.path.insert(0, temp_sim_dir)
        if files and isinstance(files, dict) and len(files) > 0:
            entry_file = (entry_point or "main.py").strip()
            if entry_file in files and files[entry_file].strip():
                code_str = files[entry_file]
        # Confine open() of student code to the sandbox directory (worker process only)
        sandbox_io.install(temp_sim_dir, student_filenames=("<string>",))
        sim_builtins["open"] = builtins.open
    except Exception as e:
        captured_logs.append(f"[SIMULATOR WARNING] Could not mount project files: {e}")

    def _sanitize(obj):
        if isinstance(obj, (float, np.floating)):
            v = float(obj)
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return round(v, 5)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (list, tuple)):
            return [_sanitize(x) for x in obj]
        if isinstance(obj, np.ndarray):
            return [_sanitize(x) for x in obj.tolist()]
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        return obj

    def _result(success: bool, error: Optional[str] = None):
        analytics = build_analytics(waypoints, timeline, detected_model)
        res = {
            "success": success,
            "robot_model": detected_model,
            "waypoints": _sanitize(waypoints),
            "analytics": _sanitize(analytics),
            "logs": captured_logs,
            "output_files": output_files,
        }
        if error is not None:
            res["error"] = error
        return res

    try:
        exec(compile(code_str, "<string>", "exec"), sim_globals)
        if temp_sim_dir:
            output_files = sandbox_io.collect_outputs(temp_sim_dir, files or {})
        return _result(True)
    except Exception as e:
        if temp_sim_dir:
            output_files = sandbox_io.collect_outputs(temp_sim_dir, files or {})
        # report the student's line for runtime errors
        err = str(e)
        tb = e.__traceback__
        line = None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == "<string>":
                line = tb.tb_lineno
            tb = tb.tb_next
        if line:
            err = f"{err} (line {line})"
        return _result(False, err)
    finally:
        # Clean up temporary mounted project directory
        if temp_sim_dir:
            try:
                if temp_sim_dir in sys.path:
                    sys.path.remove(temp_sim_dir)
                if files:
                    for fname in files.keys():
                        mod_name = os.path.splitext(os.path.basename(fname.strip()))[0]
                        sys.modules.pop(mod_name, None)
                shutil.rmtree(temp_sim_dir, ignore_errors=True)
            except Exception:
                pass

        # Restore socket
        if old_socket_cls:
            socket.socket = old_socket_cls
        if old_socket_create:
            socket.create_connection = old_socket_create

        # Restore sys.modules
        for name, old in (("rtde_control", old_ctrl), ("rtde_receive", old_recv), ("rtde_io", old_io),
                          ("ur_rtde", old_ur_rtde), ("ur_wrapper", old_wrap), ("pyniryo", old_niryo),
                          ("time", old_time)):
            if old is not None:
                sys.modules[name] = old
            else:
                sys.modules.pop(name, None)
        sys.modules.pop("ur_rtde.rtde_control", None)
        sys.modules.pop("ur_rtde.rtde_receive", None)
        sys.modules.pop("ur_rtde.rtde_io", None)


async def run_simulation_isolated(
    code_str: str,
    robot_model: Optional[str] = None,
    files: Optional[Dict[str, str]] = None,
    entry_point: Optional[str] = "main.py"
) -> Dict[str, Any]:
    """
    Runs the 3D kinematics simulation completely isolated in a separate Python subprocess.
    Ensures 100% responsiveness of the main FastAPI server, preventing any event-loop or GIL blockage,
    and guarantees zero mutation/pollution of core server sys.modules or socket handlers.
    Includes a 15-second safety timeout to prevent infinite loops from hanging the connection.
    """
    import asyncio
    import json
    import os
    import sys

    sim_script = os.path.abspath(__file__)
    work_dir = os.path.dirname(sim_script)
    payload_json = json.dumps({
        "code": code_str,
        "robot_model": robot_model,
        "files": files,
        "entry_point": entry_point or "main.py"
    })

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", sim_script, "--worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(input=payload_json.encode("utf-8")),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {
                "success": False,
                "robot_model": robot_model or "ur",
                "error": "Simulation execution timed out (15-second limit exceeded). Please verify your code does not contain infinite loops (e.g. while True).",
                "waypoints": [],
                "logs": []
            }

        if proc.returncode != 0 and not stdout_data:
            err_msg = stderr_data.decode("utf-8", errors="replace").strip() if stderr_data else f"Worker exited with code {proc.returncode}"
            return {
                "success": False,
                "robot_model": robot_model or "ur",
                "error": f"Simulation worker error: {err_msg}",
                "waypoints": [],
                "logs": []
            }

        raw_out = stdout_data.decode("utf-8", errors="replace").strip()
        if not raw_out:
            return {
                "success": False,
                "robot_model": robot_model or "ur",
                "error": "Simulator worker returned empty output.",
                "waypoints": [],
                "logs": []
            }

        lines = [l for l in raw_out.splitlines() if l.strip().startswith("{") and l.strip().endswith("}")]
        if lines:
            return json.loads(lines[-1])
        return json.loads(raw_out)

    except Exception as e:
        return {
            "success": False,
            "robot_model": robot_model or "ur",
            "error": f"Failed to execute isolated simulation: {e}",
            "waypoints": [],
            "logs": []
        }


if __name__ == "__main__":
    import json
    import sys

    # Worker entrypoint for isolated simulation process
    try:
        raw_in = sys.stdin.read()
        if not raw_in.strip():
            print(json.dumps({"success": False, "error": "No input payload provided.", "waypoints": [], "logs": []}))
            sys.exit(0)
        data = json.loads(raw_in)
        code_input = data.get("code", "")
        model_input = data.get("robot_model", None)
        files_input = data.get("files", None)
        entry_input = data.get("entry_point", "main.py")
        res = simulate_script(code_input, robot_model=model_input, files=files_input, entry_point=entry_input)
        print(json.dumps(res))
    except Exception as err:
        print(json.dumps({
            "success": False,
            "error": str(err),
            "waypoints": [],
            "logs": []
        }))
    sys.exit(0)
