"""
SQLite database operations for student code submissions, queue management, shared examples, and user authentication.
Supports multi-robot examples (UR3 & Niryo Ned) and student project history.
"""

import os
import aiosqlite
import datetime
import hashlib
import secrets
from typing import List, Optional, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submissions.db")

# --- Password Hashing Utilities ---

def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Hashes a password with a secure salt using SHA-256."""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${hashed}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verifies a password against a stored salt$hash string."""
    try:
        salt, hashed = stored_hash.split("$", 1)
        expected = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return secrets.compare_digest(hashed, expected)
    except Exception:
        return False


# --- Default Presets for Universal Robots UR3 ---

DEFAULT_UR_EXAMPLES = [
    {
        "title": "1. Cartesian Probe (moveJ + moveL)",
        "description": "Moves to home, descends 15cm in Z, shifts in X, returns home.",
        "robot_model": "ur",
        "code": """import numpy as np
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive
import config

ROBOT_IP = config.ROBOT_IP

rtde_c = RTDEControl(ROBOT_IP)
rtde_r = RTDEReceive(ROBOT_IP)

# Move to safe home position using moveJ (joint space)
# Arguments: joint angles [rad], speed [rad/s], accel [rad/s^2]
home_q = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
print(f"Moving to home joint configuration: {home_q}")
rtde_c.moveJ(home_q, 0.5, 0.5)

# Read current TCP Cartesian pose at home
tcp_pose = rtde_r.getActualTCPPose()
print(f"TCP pose at home: {[round(x, 3) for x in tcp_pose[:3]]}")

# Move 15 cm down in Z using moveL (Cartesian linear move)
pose_down = tcp_pose[:]
pose_down[2] -= 0.15
rtde_c.moveL(pose_down, 0.25, 0.5)

# Move 10 cm in X
pose_side = pose_down[:]
pose_side[0] += 0.10
rtde_c.moveL(pose_side, 0.25, 0.5)

# Return to initial home TCP pose
rtde_c.moveL(tcp_pose, 0.25, 0.5)

# Return to home joint configuration
rtde_c.moveJ(home_q, 0.5, 0.5)

rtde_c.stopScript()
print("Cartesian sequence completed successfully!")
"""
    },
    {
        "title": "2. Square Contour Path (moveL Box)",
        "description": "Traces a 10cm × 10cm horizontal box with linear Cartesian moves.",
        "robot_model": "ur",
        "code": """import numpy as np
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive
import config

ROBOT_IP = config.ROBOT_IP

rtde_c = RTDEControl(ROBOT_IP)
rtde_r = RTDEReceive(ROBOT_IP)

home_q = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
rtde_c.moveJ(home_q, 0.5, 0.5)

start_pose = rtde_r.getActualTCPPose()
print(f"Starting 10cm square contour at: {[round(x, 3) for x in start_pose[:3]]}")

box_size = 0.10  # 10 cm box

# Corner 1: +X
p1 = start_pose[:]
p1[0] += box_size
rtde_c.moveL(p1, 0.20, 0.4)

# Corner 2: +X, -Y
p2 = p1[:]
p2[1] -= box_size
rtde_c.moveL(p2, 0.20, 0.4)

# Corner 3: original X, -Y
p3 = p2[:]
p3[0] -= box_size
rtde_c.moveL(p3, 0.20, 0.4)

# Corner 4: Return to start TCP pose
rtde_c.moveL(start_pose, 0.20, 0.4)

# Return home
rtde_c.moveJ(home_q, 0.5, 0.5)
rtde_c.stopScript()
print("Square contour path completed successfully!")
"""
    },
    {
        "title": "3. Joint-Space Articulation (moveJ)",
        "description": "Articulates 6 axes through multiple safe inspection configurations.",
        "robot_model": "ur",
        "code": """import numpy as np
import time
from rtde_control import RTDEControlInterface as RTDEControl
import config

ROBOT_IP = config.ROBOT_IP
rtde_c = RTDEControl(ROBOT_IP)

# Pose 1: Standard Home
q_home = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
print("Moving to Pose 1: Home")
rtde_c.moveJ(q_home, 0.5, 0.5)
time.sleep(1.0)

# Pose 2: Forward Reach Inspection
q_reach = [-np.pi/2, -np.pi/3, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
print("Moving to Pose 2: Forward Reach")
rtde_c.moveJ(q_reach, 0.4, 0.4)
time.sleep(1.0)

# Pose 3: Side Inspection
q_side = [-np.pi/3, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
print("Moving to Pose 3: Side Inspection")
rtde_c.moveJ(q_side, 0.4, 0.4)
time.sleep(1.0)

# Return to Home
rtde_c.moveJ(q_home, 0.5, 0.5)
rtde_c.stopScript()
print("Joint exploration routine finished!")
"""
    },
    {
        "title": "4. Gripper / Digital Output (I/O Pin 0)",
        "description": "Commands digital output pin 0 to simulate gripper actuation.",
        "robot_model": "ur",
        "code": """import numpy as np
import time
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive
import config

ROBOT_IP = config.ROBOT_IP

rtde_c = RTDEControl(ROBOT_IP)
rtde_r = RTDEReceive(ROBOT_IP)

home_q = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
rtde_c.moveJ(home_q, 0.5, 0.5)

tcp = rtde_r.getActualTCPPose()
pick_pose = tcp[:]
pick_pose[2] -= 0.12
rtde_c.moveL(pick_pose, 0.20, 0.4)

# Close Gripper (Pin 0 HIGH)
print("Actuating Gripper: PIN 0 -> HIGH (Grip)")
rtde_c.setStandardDigitalOut(0, True)
time.sleep(1.5)

# Lift up
rtde_c.moveL(tcp, 0.20, 0.4)

# Open Gripper (Pin 0 LOW)
print("Releasing Gripper: PIN 0 -> LOW (Release)")
rtde_c.setStandardDigitalOut(0, False)
time.sleep(1.0)

rtde_c.moveJ(home_q, 0.5, 0.5)
rtde_c.stopScript()
print("Gripper pick and place sequence finished!")
"""
    }
]

# --- Default Presets for Niryo Ned / One ---

DEFAULT_NIRYO_EXAMPLES = [
    {
        "title": "1. Niryo Ned: Home & Cartesian Move",
        "description": "Calibrates Niryo Ned, reaches ready pose, performs linear Cartesian move, and parks.",
        "robot_model": "niryo",
        "code": """import time
from pyniryo import NiryoRobot
import config

ROBOT_IP = config.ROBOT_IP
robot = NiryoRobot(ROBOT_IP)

# Auto-calibrate and move to ready pose
robot.calibrate_auto()
robot.move_joints([0.0, 0.5, -1.25, 0.0, 0.0, 0.0])
print("Niryo Ned reached ready joint pose.")

# Read current Cartesian pose
current_pose = robot.get_pose()
print(f"Current TCP Pose: {current_pose}")

# Perform linear move down and forward
target_pose = [0.25, 0.0, 0.15, 0.0, 1.57, 0.0]
robot.move_pose(target_pose)
time.sleep(1.0)

# Return to sleep / park position
robot.move_joints([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
robot.close_connection()
print("Niryo Ned Cartesian routine completed successfully!")
"""
    },
    {
        "title": "2. Niryo Ned: Pick & Place with Gripper",
        "description": "Opens gripper, descends to pick position, grasps object, moves across, and releases.",
        "robot_model": "niryo",
        "code": """import time
from pyniryo import NiryoRobot
import config

ROBOT_IP = config.ROBOT_IP
robot = NiryoRobot(ROBOT_IP)
robot.calibrate_auto()

# Approach Pick Pose
print("Approaching pick area...")
robot.move_pose([0.20, -0.10, 0.25, 0.0, 1.57, -0.78])

# Open Gripper & Descend
robot.open_gripper(500)
robot.move_pose([0.20, -0.10, 0.12, 0.0, 1.57, -0.78])

# Grasp
print("Grasping object...")
robot.close_gripper(500)
time.sleep(1.0)

# Lift
robot.move_pose([0.20, -0.10, 0.25, 0.0, 1.57, -0.78])

# Move to Place Area
print("Moving to place area...")
robot.move_pose([0.20, 0.10, 0.25, 0.0, 1.57, 0.78])
robot.move_pose([0.20, 0.10, 0.12, 0.0, 1.57, 0.78])

# Release
print("Releasing object...")
robot.open_gripper(500)
time.sleep(0.8)

# Retract and Return
robot.move_pose([0.20, 0.10, 0.25, 0.0, 1.57, 0.78])
robot.move_joints([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
robot.close_connection()
print("Niryo Ned Pick & Place routine completed!")
"""
    },
    {
        "title": "3. Niryo Ned: Multi-Axis Joint Trajectory",
        "description": "Smoothly steps all 6 joints through a coordinated spatial trajectory.",
        "robot_model": "niryo",
        "code": """import time
from pyniryo import NiryoRobot
import config

ROBOT_IP = config.ROBOT_IP
robot = NiryoRobot(ROBOT_IP)
robot.calibrate_auto()

waypoints = [
    [0.0, 0.4, -1.0, 0.0, 0.0, 0.0],
    [-0.6, 0.3, -0.8, 0.4, -0.2, 0.3],
    [0.0, 0.6, -1.3, 0.0, 0.3, 0.0],
    [0.6, 0.3, -0.8, -0.4, 0.2, -0.3],
    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
]

for idx, q in enumerate(waypoints):
    print(f"Moving Niryo Ned to waypoint {idx+1}/{len(waypoints)}...")
    robot.move_joints(q)
    time.sleep(0.4)

robot.close_connection()
print("Multi-axis joint sequence completed!")
"""
    },
    {
        "title": "4. Niryo Ned: Velocity & Trajectory Control",
        "description": "Adjusts arm speed percentage and commands smooth spatial motion.",
        "robot_model": "niryo",
        "code": """import time
from pyniryo import NiryoRobot
import config

ROBOT_IP = config.ROBOT_IP
robot = NiryoRobot(ROBOT_IP)
robot.calibrate_auto()

# Set arm speed percentage (100 is max, 50 is normal)
robot.set_arm_max_velocity(60)

print("Moving with controlled 60% velocity...")
robot.move_joints([0.0, 0.5, -1.25, 0.0, 0.0, 0.0])

# Move across working area
robot.move_pose([0.25, -0.15, 0.20, 0.0, 1.57, 0.0])
robot.move_pose([0.25, 0.15, 0.20, 0.0, 1.57, 0.0])

# Reset speed and park
robot.set_arm_max_velocity(100)
robot.move_joints([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
robot.close_connection()
print("Velocity control routine completed!")
"""
    }
]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Submissions table with robot_model support
        await db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                task_id TEXT NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                logs TEXT DEFAULT '',
                submitted_at TEXT NOT NULL,
                executed_at TEXT,
                completed_at TEXT,
                robot_model TEXT NOT NULL DEFAULT 'ur'
            )
        """)

        # Check if robot_model column exists in submissions (migration)
        async with db.execute("PRAGMA table_info(submissions)") as cursor:
            sub_cols = [row[1] for row in await cursor.fetchall()]
            if "robot_model" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN robot_model TEXT NOT NULL DEFAULT 'ur'")

        # Examples table with robot_model support
        await db.execute("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                code TEXT NOT NULL,
                is_builtin INTEGER DEFAULT 0,
                robot_model TEXT NOT NULL DEFAULT 'ur',
                created_at TEXT NOT NULL
            )
        """)

        # Check if robot_model column exists in examples (for backward compatibility migration)
        async with db.execute("PRAGMA table_info(examples)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            if "robot_model" not in cols:
                await db.execute("ALTER TABLE examples ADD COLUMN robot_model TEXT NOT NULL DEFAULT 'ur'")

        # Users table for student registration & admin approvals
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Seed UR3 examples if not present
        async with db.execute("SELECT COUNT(*) FROM examples WHERE robot_model = 'ur'") as cursor:
            count = (await cursor.fetchone())[0]
            if count == 0:
                for ex in DEFAULT_UR_EXAMPLES:
                    await db.execute(
                        """
                        INSERT INTO examples (title, description, code, is_builtin, robot_model, created_at)
                        VALUES (?, ?, ?, 1, 'ur', ?)
                        """,
                        (ex["title"], ex["description"], ex["code"], now)
                    )

        # Seed Niryo Ned examples if not present
        async with db.execute("SELECT COUNT(*) FROM examples WHERE robot_model = 'niryo'") as cursor:
            count = (await cursor.fetchone())[0]
            if count == 0:
                for ex in DEFAULT_NIRYO_EXAMPLES:
                    await db.execute(
                        """
                        INSERT INTO examples (title, description, code, is_builtin, robot_model, created_at)
                        VALUES (?, ?, ?, 1, 'niryo', ?)
                        """,
                        (ex["title"], ex["description"], ex["code"], now)
                    )

        # Ensure default admin account exists (username: admin, password: mike2088)
        async with db.execute("SELECT id FROM users WHERE username = 'admin'") as cursor:
            admin_row = await cursor.fetchone()
            if not admin_row:
                admin_hash = hash_password("mike2088")
                await db.execute(
                    """
                    INSERT INTO users (username, password_hash, role, status, created_at)
                    VALUES ('admin', ?, 'admin', 'approved', ?)
                    """,
                    (admin_hash, now)
                )

        await db.commit()


# --- User CRUD & Authentication Queries ---

async def create_user(username: str, password: str, role: str = "student", status: str = "pending") -> int:
    username = username.strip()
    if not username:
        raise ValueError("Username cannot be empty.")
    if len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")

    password_hash = hash_password(password)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO users (username, password_hash, role, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, password_hash, role, status, now)
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def verify_user_credentials(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = await get_user_by_username(username)
    if not user:
        return None
    if verify_password(password, user["password_hash"]):
        return user
    return None


async def get_approved_students() -> List[Dict[str, Any]]:
    """Returns list of approved student accounts for login dropdown selection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, role, status, created_at FROM users WHERE role = 'student' AND status = 'approved' ORDER BY username COLLATE NOCASE ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_users() -> List[Dict[str, Any]]:
    """Returns all users, with pending users listed first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, username, role, status, created_at 
            FROM users 
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_user_status(user_id: int, status: str) -> bool:
    if status not in ("approved", "rejected", "pending"):
        raise ValueError("Invalid status")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def delete_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        # Protect default admin from deletion
        cursor = await db.execute("DELETE FROM users WHERE id = ? AND username != 'admin'", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def is_program_name_duplicate(student_name: str, program_name: str) -> bool:
    """Checks if a student has already submitted a program with this name."""
    s_name = student_name.strip().lower()
    p_name = program_name.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM submissions WHERE LOWER(student_name) = ? AND LOWER(task_id) = ?",
            (s_name, p_name)
        ) as cursor:
            count = (await cursor.fetchone())[0]
            return count > 0


# --- Submissions CRUD & Project History ---

async def create_submission(student_name: str, task_id: str, code: str, robot_model: str = "ur") -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    norm_model = (robot_model or "ur").strip().lower()
    if norm_model not in ("ur", "niryo"):
        norm_model = "ur"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO submissions (student_name, task_id, code, status, logs, submitted_at, robot_model)
            VALUES (?, ?, ?, 'pending', '', ?, ?)
            """,
            (student_name, task_id, code, now, norm_model)
        )
        await db.commit()
        return cursor.lastrowid


async def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_submissions(limit: int = 50) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM submissions ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_student_submissions(student_name: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Returns all projects / submissions submitted by a specific student."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, student_name, task_id, code, status, logs, submitted_at, executed_at, completed_at, robot_model
            FROM submissions 
            WHERE LOWER(student_name) = LOWER(?) 
            ORDER BY id DESC LIMIT ?
            """,
            (student_name.strip(), limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_status(sub_id: int, status: str, logs: Optional[str] = None):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if status == 'running':
            await db.execute(
                "UPDATE submissions SET status = ?, executed_at = ?, completed_at = NULL, logs = '' WHERE id = ?",
                (status, now, sub_id)
            )
        elif status in ('completed', 'failed', 'aborted', 'rejected'):
            if logs is not None:
                await db.execute(
                    "UPDATE submissions SET status = ?, completed_at = ?, logs = ? WHERE id = ?",
                    (status, now, logs, sub_id)
                )
            else:
                await db.execute(
                    "UPDATE submissions SET status = ?, completed_at = ? WHERE id = ?",
                    (status, now, sub_id)
                )
        else:
            await db.execute(
                "UPDATE submissions SET status = ? WHERE id = ?",
                (status, sub_id)
            )
        await db.commit()


async def update_submission_code(sub_id: int, code: Optional[str] = None, student_name: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if code is not None and student_name is not None:
            await db.execute(
                "UPDATE submissions SET code = ?, student_name = ? WHERE id = ?",
                (code, student_name, sub_id)
            )
        elif code is not None:
            await db.execute(
                "UPDATE submissions SET code = ? WHERE id = ?",
                (code, sub_id)
            )
        elif student_name is not None:
            await db.execute(
                "UPDATE submissions SET student_name = ? WHERE id = ?",
                (student_name, sub_id)
            )
        await db.commit()


async def append_logs(sub_id: int, log_chunk: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET logs = COALESCE(logs, '') || ? WHERE id = ?",
            (log_chunk, sub_id)
        )
        await db.commit()


async def delete_submission(sub_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
        await db.commit()


async def clear_all_submissions():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM submissions")
        await db.commit()


async def clear_pending_queue():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET status = 'rejected' WHERE status = 'pending'"
        )
        await db.commit()


# --- Examples CRUD with Robot Model Filtering ---

async def get_all_examples(robot_model: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns examples, optionally filtered by robot_model ('ur' | 'niryo')."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if robot_model:
            async with db.execute(
                "SELECT * FROM examples WHERE LOWER(robot_model) = LOWER(?) ORDER BY is_builtin DESC, id ASC",
                (robot_model.strip(),)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        else:
            async with db.execute("SELECT * FROM examples ORDER BY robot_model ASC, is_builtin DESC, id ASC") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]


async def get_example(example_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM examples WHERE id = ?", (example_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_example(title: str, description: str, code: str, is_builtin: int = 0, robot_model: str = "ur") -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    model = robot_model.strip().lower() if robot_model else "ur"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO examples (title, description, code, is_builtin, robot_model, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title.strip(), description.strip(), code, is_builtin, model, now)
        )
        await db.commit()
        return cursor.lastrowid


async def delete_example(example_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM examples WHERE id = ?", (example_id,))
        await db.commit()
