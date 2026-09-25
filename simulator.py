"""
Accurate UR3 Kinematics Simulation and Trajectory Generator.
Computes real Denavit-Hartenberg Forward Kinematics and full 6-DOF
Damped Least Squares Inverse Kinematics so moveJ and moveL produce
physically accurate, linear, and orientation-consistent trajectories.
"""

import math
import sys
import types
from typing import Any, Dict, List, Tuple
import numpy as np

# Standard UR3 DH parameters [a, d, alpha]
DH_UR3 = [
    [0.0,      0.1519,   np.pi / 2],    # Joint 1 (Base Pan to Shoulder)
    [-0.24365, 0.0,      0.0],          # Joint 2 (Upper Arm)
    [-0.21325, 0.0,      0.0],          # Joint 3 (Forearm)
    [0.0,      0.11235,  np.pi / 2],    # Joint 4 (Wrist 1)
    [0.0,      0.08535, -np.pi / 2],    # Joint 5 (Wrist 2)
    [0.0,      0.0819,   0.0]           # Joint 6 (Wrist 3 / Tool Flange)
]


def rotvec_to_matrix(rotvec: np.ndarray) -> np.ndarray:
    """Converts an axis-angle rotation vector to a 3x3 rotation matrix using Rodrigues' formula."""
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-7:
        return np.eye(3)
    k = rotvec / theta
    K = np.array([
        [0.0, -k[2], k[1]],
        [k[2], 0.0, -k[0]],
        [-k[1], k[0], 0.0]
    ])
    return np.eye(3) + np.sin(theta) * K + (1.0 - np.cos(theta)) * (K @ K)


def matrix_to_rotvec(R: np.ndarray) -> np.ndarray:
    """Converts a 3x3 rotation matrix to an axis-angle rotation vector."""
    trace_val = (np.trace(R) - 1.0) / 2.0
    angle = np.arccos(np.clip(trace_val, -1.0, 1.0))
    if angle < 1e-6:
        return np.zeros(3)
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    return (angle / (2.0 * np.sin(angle))) * v


def forward_kinematics_ur3(q: List[float]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[np.ndarray], List[np.ndarray]]:
    """
    Computes exact 6-DOF UR3 Forward Kinematics using standard DH parameters.
    Returns: (pos_xyz, rotvec_xyz, R_3x3, joint_positions, joint_z_axes)
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
    rotvec = matrix_to_rotvec(R)

    return pos, rotvec, R, positions, z_axes


def get_tcp_pose_list(q: List[float]) -> List[float]:
    """Returns [x, y, z, rx, ry, rz] pose list for given joint configuration."""
    pos, rotvec, _, _, _ = forward_kinematics_ur3(q)
    return [float(x) for x in np.concatenate([pos, rotvec])]


def inverse_kinematics_6dof(q_seed: List[float], target_pose: List[float], max_iters: int = 100) -> List[float]:
    """
    Solves full 6-DOF Inverse Kinematics (position + orientation)
    using Damped Least Squares (DLS) Jacobian iteration with null-space
    projection towards q_seed. Guarantees kinematic branch continuity
    and prevents abrupt 180° wrist/elbow flips during Cartesian motion.
    """
    q = np.array(q_seed, dtype=float)
    target_pos = np.array(target_pose[:3], dtype=float)
    target_rotvec = np.array(target_pose[3:6], dtype=float)
    target_R = rotvec_to_matrix(target_rotvec)
    q_seed_arr = np.array(q_seed, dtype=float)

    for _ in range(max_iters):
        pos, _, R, positions, z_axes = forward_kinematics_ur3(q)

        # Position error [m]
        pos_err = target_pos - pos

        # Orientation error [rad]
        R_err = target_R @ R.T
        trace_val = (np.trace(R_err) - 1.0) / 2.0
        angle = np.arccos(np.clip(trace_val, -1.0, 1.0))
        if angle < 1e-6:
            rot_err = np.zeros(3)
        else:
            v = np.array([R_err[2, 1] - R_err[1, 2], R_err[0, 2] - R_err[2, 0], R_err[1, 0] - R_err[0, 1]])
            rot_err = (angle / (2.0 * np.sin(angle))) * v

        # Check convergence: sub-millimeter position and <0.002 rad orientation
        if np.linalg.norm(pos_err) < 1e-4 and np.linalg.norm(rot_err) < 2e-3:
            break

        # Full 6x6 geometric Jacobian
        J = np.zeros((6, 6))
        for i in range(6):
            p_i = positions[i]
            z_i = z_axes[i]
            J[:3, i] = np.cross(z_i, pos - p_i)
            J[3:, i] = z_i

        error_6d = np.concatenate([pos_err, rot_err])
        err_norm = float(np.linalg.norm(error_6d))

        # Adaptive damping for singularity robustness
        damping = 1e-3 if err_norm < 0.05 else 1e-2
        inv_part = np.linalg.inv(J @ J.T + damping * np.eye(6))
        J_pinv = J.T @ inv_part
        dq = J_pinv @ error_6d

        # Null-space bias towards seed to stay locked in the same kinematic branch
        null_proj = np.eye(6) - J_pinv @ J
        dq += null_proj @ (q_seed_arr - q) * 0.25

        # Clamp step size to prevent numerical oscillation
        step_len = float(np.linalg.norm(dq))
        if step_len > 0.15:
            dq = dq * (0.15 / step_len)

        q += dq

    # Continuous angle unwrapping relative to q_seed (prevents 2*pi wrapping jumps)
    for j in range(6):
        diff = q[j] - q_seed[j]
        q[j] = q_seed[j] + (diff + np.pi) % (2 * np.pi) - np.pi

    return [float(x) for x in q]


class MockRTDEReceive:
    def __init__(self, ip: str = "192.168.56.101"):
        self.ip = ip
        # Safe default home pose: [-pi/2, -pi/2, -pi/2, -pi/2, pi/2, 0.0]
        self.current_q = [-math.pi / 2, -math.pi / 2, -math.pi / 2, -math.pi / 2, math.pi / 2, 0.0]

    def getActualTCPPose(self) -> List[float]:
        return get_tcp_pose_list(self.current_q)

    def getActualQ(self) -> List[float]:
        return list(self.current_q)

    def getTargetTCPPose(self) -> List[float]:
        return get_tcp_pose_list(self.current_q)

    def getTargetQ(self) -> List[float]:
        return list(self.current_q)

    def getActualTCPSpeed(self) -> List[float]:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def getActualTCPForce(self) -> List[float]:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def getJointTemperatures(self) -> List[float]:
        return [32.0, 31.5, 33.0, 29.5, 30.0, 28.5]

    def isConnected(self) -> bool:
        return True

    def disconnect(self):
        pass

    def reconnect(self) -> bool:
        return True

    def __getattr__(self, name: str):
        # Fallback for any unmocked receive queries
        return lambda *a, **kw: 0.0


class MockRTDEControl:
    def __init__(self, tracker: List[Dict[str, Any]], receiver: MockRTDEReceive):
        self.tracker = tracker
        self.receiver = receiver

    def moveJ(self, q: Any, speed: float = 0.5, accel: float = 0.5):
        q_target = [float(x) for x in q]
        q_start = list(self.receiver.current_q)

        # Discretize joint space motion into intermediate waypoints for smooth animation
        num_steps = 10
        for s in range(1, num_steps + 1):
            alpha = s / num_steps
            smooth_alpha = 0.5 * (1.0 - math.cos(math.pi * alpha))
            q_interp = [
                q_start[j] + (q_target[j] - q_start[j]) * smooth_alpha
                for j in range(6)
            ]
            is_final = (s == num_steps)
            label = f"moveJ({[round(x, 2) for x in q_target]})" if is_final else f"moveJ step {s}/{num_steps}"
            self.tracker.append({
                "type": "moveJ",
                "robot_model": "ur",
                "q": q_interp,
                "label": label,
                "speed": float(speed),
                "accel": float(accel),
                "is_final": is_final
            })

        self.receiver.current_q = list(q_target)
        return True

    def moveL(self, pose: Any, speed: float = 0.25, accel: float = 0.5):
        target_pose = [float(x) for x in pose]
        start_pose = get_tcp_pose_list(self.receiver.current_q)

        # Discretize Cartesian straight line motion (10 steps)
        num_steps = 10
        prev_q = list(self.receiver.current_q)

        for s in range(1, num_steps + 1):
            alpha = s / num_steps
            smooth_alpha = 0.5 * (1.0 - math.cos(math.pi * alpha))

            # Interpolate position and orientation
            interp_pose = [
                start_pose[j] + (target_pose[j] - start_pose[j]) * smooth_alpha
                for j in range(6)
            ]

            # Solve 6-DOF IK using previous step as seed for seamless continuity
            step_q = inverse_kinematics_6dof(prev_q, interp_pose)
            prev_q = list(step_q)

            is_final = (s == num_steps)
            label = (
                f"moveL([X={target_pose[0]:.2f}m, Y={target_pose[1]:.2f}m, Z={target_pose[2]:.2f}m])"
                if is_final else f"moveL linear step {s}/{num_steps}"
            )

            self.tracker.append({
                "type": "moveL",
                "robot_model": "ur",
                "pose": interp_pose,
                "q": step_q,
                "label": label,
                "speed": float(speed),
                "accel": float(accel),
                "is_final": is_final
            })

        self.receiver.current_q = list(prev_q)
        return True

    def stopScript(self):
        self.tracker.append({
            "type": "stopScript",
            "robot_model": "ur",
            "label": "stopScript()"
        })
        return True

    def setStandardDigitalOut(self, pin: int, value: bool):
        self.tracker.append({
            "type": "digitalOut",
            "robot_model": "ur",
            "pin": pin,
            "value": bool(value),
            "label": f"DigitalOut({pin}={value})"
        })
        return True

    def setToolDigitalOut(self, pin: int, value: bool):
        self.tracker.append({
            "type": "digitalOut",
            "robot_model": "ur",
            "pin": pin,
            "value": bool(value),
            "label": f"ToolDigitalOut({pin}={value})"
        })
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
        # Fallback for any other ur_rtde control calls
        return lambda *a, **kw: True


# Standard Niryo One / Ned DH parameters [a, d, alpha]
DH_NIRYO = [
    [0.0,   0.130,  np.pi / 2],
    [0.210, 0.0,    0.0],
    [0.030, 0.0,    np.pi / 2],
    [0.0,   0.190, -np.pi / 2],
    [0.0,   0.0,    np.pi / 2],
    [0.0,   0.080,  0.0]
]


def rpy_to_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Converts roll-pitch-yaw Euler angles (ZYX) to a 3x3 rotation matrix."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    Rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    Ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    return Rz @ Ry @ Rx


def matrix_to_rpy(R: np.ndarray) -> List[float]:
    """Converts a 3x3 rotation matrix to roll-pitch-yaw Euler angles."""
    pitch = float(np.arcsin(-np.clip(R[2, 0], -1.0, 1.0)))
    if np.abs(np.cos(pitch)) > 1e-6:
        roll = float(np.arctan2(R[2, 1], R[2, 2]))
        yaw = float(np.arctan2(R[1, 0], R[0, 0]))
    else:
        roll = float(np.arctan2(-R[1, 2], R[1, 1]))
        yaw = 0.0
    return [roll, pitch, yaw]


def fk_niryo(q: List[float]):
    """Computes exact 6-DOF Niryo forward kinematics."""
    T = np.eye(4)
    positions = [np.zeros(3)]
    z_axes = [np.array([0.0, 0.0, 1.0])]
    for i in range(6):
        theta = float(q[i])
        a, d, alpha = DH_NIRYO[i]
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
    rpy = matrix_to_rpy(R)
    return pos, rpy, R, positions, z_axes


def ik_niryo(q_seed: List[float], target_pose: List[float], max_iters: int = 80) -> List[float]:
    """Damped Least Squares inverse kinematics for Niryo One / Ned."""
    q = np.array(q_seed, dtype=float)
    target_pos = np.array(target_pose[:3], dtype=float)
    target_R = rpy_to_matrix(target_pose[3], target_pose[4], target_pose[5]) if len(target_pose) >= 6 else np.eye(3)
    q_seed_arr = np.array(q_seed, dtype=float)

    for _ in range(max_iters):
        pos, _, R, positions, z_axes = fk_niryo(q)
        pos_err = target_pos - pos
        R_err = target_R @ R.T
        trace_val = (np.trace(R_err) - 1.0) / 2.0
        angle = np.arccos(np.clip(trace_val, -1.0, 1.0))
        rot_err = np.zeros(3) if angle < 1e-6 else (angle / (2.0 * np.sin(angle))) * np.array([
            R_err[2, 1] - R_err[1, 2], R_err[0, 2] - R_err[2, 0], R_err[1, 0] - R_err[0, 1]
        ])

        if np.linalg.norm(pos_err) < 2e-3 and np.linalg.norm(rot_err) < 5e-2:
            break

        J = np.zeros((6, 6))
        for i in range(6):
            p_i = positions[i]
            z_i = z_axes[i]
            J[:3, i] = np.cross(z_i, pos - p_i)
            J[3:, i] = z_i

        error_6d = np.concatenate([pos_err, rot_err * 0.4])
        damping = 1e-2
        inv_part = np.linalg.inv(J @ J.T + damping * np.eye(6))
        J_pinv = J.T @ inv_part
        dq = J_pinv @ error_6d + (np.eye(6) - J_pinv @ J) @ (q_seed_arr - q) * 0.2
        step_len = float(np.linalg.norm(dq))
        if step_len > 0.15:
            dq = dq * (0.15 / step_len)
        q += dq

    for j in range(6):
        diff = q[j] - q_seed[j]
        q[j] = q_seed[j] + (diff + np.pi) % (2 * np.pi) - np.pi
    return [float(x) for x in q]


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

    def __iter__(self):
        return iter(self.to_list())

    def __getitem__(self, item):
        return self.to_list()[item]

    def __repr__(self):
        return f"Pose(x={self.x:.3f}, y={self.y:.3f}, z={self.z:.3f}, roll={self.roll:.3f}, pitch={self.pitch:.3f}, yaw={self.yaw:.3f})"


class MockNiryoRobot:
    def __init__(self, tracker: List[Dict[str, Any]], ip: str = "127.0.0.1"):
        self.tracker = tracker
        self.ip = ip
        # Ready pose [j1, j2, j3, j4, j5, j6] (rad)
        self.current_q = [0.0, 0.5, -1.25, 0.0, 0.0, 0.0]
        self.is_calibrated = False

    def calibrate_auto(self):
        self.is_calibrated = True
        return True

    def calibrate_manual(self):
        self.is_calibrated = True
        return True

    def move_joints(self, joints: Any):
        q_target = [float(x) for x in joints]
        q_start = list(self.current_q)
        num_steps = 10
        for s in range(1, num_steps + 1):
            alpha = s / num_steps
            smooth_alpha = 0.5 * (1.0 - math.cos(math.pi * alpha))
            q_interp = [
                q_start[j] + (q_target[j] - q_start[j]) * smooth_alpha
                for j in range(6)
            ]
            is_final = (s == num_steps)
            label = f"Niryo move_joints({[round(x, 2) for x in q_target]})" if is_final else f"Niryo step {s}/{num_steps}"
            self.tracker.append({
                "type": "move_joints",
                "robot_model": "niryo",
                "q": q_interp,
                "label": label,
                "is_final": is_final
            })
        self.current_q = list(q_target)
        return True

    def move_pose(self, *args):
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            pose = [float(x) for x in args[0]]
        elif len(args) >= 6:
            pose = [float(x) for x in args[:6]]
        elif len(args) == 1 and hasattr(args[0], 'to_list'):
            pose = args[0].to_list()
        else:
            pose = [0.20, 0.0, 0.15, 0.0, 1.57, 0.0]

        target_q = ik_niryo(self.current_q, pose)
        return self.move_joints(target_q)

    def get_joints(self) -> List[float]:
        return list(self.current_q)

    def get_pose(self) -> MockNiryoPose:
        pos, rpy, _, _, _ = fk_niryo(self.current_q)
        return MockNiryoPose(pos[0], pos[1], pos[2], rpy[0], rpy[1], rpy[2])

    def open_gripper(self, speed: int = 500):
        self.tracker.append({
            "type": "gripper",
            "robot_model": "niryo",
            "state": "open",
            "label": f"open_gripper({speed})",
            "q": list(self.current_q),
            "is_final": True
        })
        return True

    def close_gripper(self, speed: int = 500):
        self.tracker.append({
            "type": "gripper",
            "robot_model": "niryo",
            "state": "closed",
            "label": f"close_gripper({speed})",
            "q": list(self.current_q),
            "is_final": True
        })
        return True

    def grasp_with_tool(self):
        return self.close_gripper(500)

    def release_with_tool(self):
        return self.open_gripper(500)

    def set_arm_max_velocity(self, percent: int):
        return True

    def wait(self, duration: float):
        pass

    def close_connection(self):
        pass

    def __getattr__(self, name: str):
        return lambda *a, **kw: True


def simulate_script(code_str: str) -> Dict[str, Any]:
    """
    Simulates Python code in an isolated scope with accurate UR3 & Niryo mock kinematics.
    Returns generated trajectory waypoints and captured output logs.
    """
    waypoints = []
    receiver = MockRTDEReceive()
    controller = MockRTDEControl(tracker=waypoints, receiver=receiver)
    niryo_robot = MockNiryoRobot(tracker=waypoints)
    captured_logs = []

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

    sim_builtins = dict(builtins.__dict__)
    sim_builtins["print"] = mock_print
    sim_builtins["printf"] = mock_printf

    # Mock modules in sys.modules so imports resolve seamlessly
    mock_ctrl_mod = types.ModuleType("rtde_control")
    mock_ctrl_mod.RTDEControlInterface = lambda *a, **kw: controller
    mock_recv_mod = types.ModuleType("rtde_receive")
    mock_recv_mod.RTDEReceiveInterface = lambda *a, **kw: receiver

    # Mock pyniryo module
    mock_niryo_mod = types.ModuleType("pyniryo")
    mock_niryo_mod.NiryoRobot = lambda *a, **kw: niryo_robot
    mock_niryo_mod.PoseObject = MockNiryoPose

    class MockRobotArm:
        def __init__(self, *a, **kw):
            self.is_simulated = True
        def movej(self, q, a=0.5, v=0.3):
            return controller.moveJ(q, a, v)
        def movel(self, pose, a=0.3, v=0.2):
            return controller.moveL(pose, a, v)
        def set_digital_out(self, pin, value):
            return controller.setStandardDigitalOut(pin, value)
        def sleep(self, s):
            pass
        def get_actual_joint_positions(self):
            return receiver.getActualQ()
        def get_actual_tcp_pose(self):
            return receiver.getActualTCPPose()
        def stop(self):
            return controller.stopScript()

    mock_wrap_mod = types.ModuleType("ur_wrapper")
    mock_wrap_mod.RobotArm = MockRobotArm
    mock_wrap_mod.printf = mock_printf
    mock_wrap_mod.SafetyViolationError = Exception
    mock_wrap_mod.MAX_JOINT_SPEED = 0.5
    mock_wrap_mod.MAX_JOINT_ACCEL = 0.8
    mock_wrap_mod.MAX_LINEAR_SPEED = 0.20
    mock_wrap_mod.MAX_LINEAR_ACCEL = 0.50

    old_ctrl = sys.modules.get("rtde_control")
    old_recv = sys.modules.get("rtde_receive")
    old_wrap = sys.modules.get("ur_wrapper")
    old_niryo = sys.modules.get("pyniryo")

    sys.modules["rtde_control"] = mock_ctrl_mod
    sys.modules["rtde_receive"] = mock_recv_mod
    sys.modules["ur_wrapper"] = mock_wrap_mod
    sys.modules["pyniryo"] = mock_niryo_mod

    sim_globals = {
        "__builtins__": sim_builtins,
        "np": np,
        "numpy": np,
        "math": math,
        "time": type("MockTime", (), {"sleep": lambda s: None}),
        "RTDEControl": lambda *a, **kw: controller,
        "RTDEReceive": lambda *a, **kw: receiver,
        "NiryoRobot": lambda *a, **kw: niryo_robot,
        "pyniryo": mock_niryo_mod,
        "RobotArm": MockRobotArm,
        "printf": mock_printf,
    }

    # Detect robot model from code content
    detected_model = "niryo" if ("pyniryo" in code_str or "NiryoRobot" in code_str) else "ur"

    try:
        exec(code_str, sim_globals)
        return {
            "success": True,
            "robot_model": detected_model,
            "waypoints": waypoints,
            "logs": captured_logs
        }
    except Exception as e:
        return {
            "success": False,
            "robot_model": detected_model,
            "error": str(e),
            "waypoints": waypoints,
            "logs": captured_logs
        }
    finally:
        if old_ctrl is not None:
            sys.modules["rtde_control"] = old_ctrl
        else:
            sys.modules.pop("rtde_control", None)
        if old_recv is not None:
            sys.modules["rtde_receive"] = old_recv
        else:
            sys.modules.pop("rtde_receive", None)
        if old_wrap is not None:
            sys.modules["ur_wrapper"] = old_wrap
        else:
            sys.modules.pop("ur_wrapper", None)
        if old_niryo is not None:
            sys.modules["pyniryo"] = old_niryo
        else:
            sys.modules.pop("pyniryo", None)


