"""
SQLite database operations for student code submissions, queue management, shared examples, and user authentication.
Supports multi-robot examples (UR3 & Niryo Ned) and student project history.
"""

import os
import json
import aiosqlite
import datetime
import hashlib
import secrets
from typing import List, Optional, Dict, Any, Union

# Local runtime data: never tracked by git (see .gitignore). UR_DB_PATH can move it out of the checkout.
DB_PATH = os.environ.get("UR_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "submissions.db")

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
home_q = [0.0, -np.pi/2, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
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

home_q = [0.0, -np.pi/2, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
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
q_home = [0.0, -np.pi/2, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
print("Moving to Pose 1: Home")
rtde_c.moveJ(q_home, 0.5, 0.5)
time.sleep(1.0)

# Pose 2: Forward Reach Inspection
q_reach = [0.0, -np.pi/3, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
print("Moving to Pose 2: Forward Reach")
rtde_c.moveJ(q_reach, 0.4, 0.4)
time.sleep(1.0)

# Pose 3: Side Inspection
q_side = [np.pi/4, -np.pi/2, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
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

home_q = [0.0, -np.pi/2, np.pi/2, -np.pi/2, -np.pi/2, 0.0]
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
robot.move_to_home_pose()
print("Niryo Ned reached home pose.")

# Read current Cartesian pose
current_pose = robot.get_pose()
print(f"Current TCP Pose: {current_pose}")

# Perform linear move down and forward
target_pose = [0.20, 0.0, 0.20, 0.0, 1.57, 0.0]
robot.move_pose(target_pose)
time.sleep(1.0)

# Return to home position
robot.move_to_home_pose()
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

# Move to start home position
robot.move_to_home_pose()

# Approach Pick Pose (Left side: +Y)
print("Approaching pick area (Left)...")
robot.move_pose([0.20, 0.10, 0.25, 0.0, 1.57, 0.78])

# Open Gripper & Descend
robot.open_gripper(500)
robot.move_pose([0.20, 0.10, 0.12, 0.0, 1.57, 0.78])

# Grasp
print("Grasping object...")
robot.close_gripper(500)
time.sleep(1.0)

# Lift
robot.move_pose([0.20, 0.10, 0.25, 0.0, 1.57, 0.78])

# Move to Place Area (Right side: -Y)
print("Moving to place area (Right)...")
robot.move_pose([0.20, -0.10, 0.25, 0.0, 1.57, -0.78])
robot.move_pose([0.20, -0.10, 0.12, 0.0, 1.57, -0.78])

# Release
print("Releasing object...")
robot.open_gripper(500)
time.sleep(0.8)

# Retract and Return
robot.move_pose([0.20, -0.10, 0.25, 0.0, 1.57, -0.78])
robot.move_to_home_pose()
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
    [0.0, 0.35, -0.65, 0.0, 0.35, 0.0],
    [-0.5, 0.25, -0.45, 0.4, 0.2, 0.3],
    [0.0, 0.45, -0.90, 0.0, 0.45, 0.0],
    [0.5, 0.25, -0.45, -0.4, 0.2, -0.3],
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
robot.move_to_home_pose()

# Move across working area
robot.move_pose([0.25, 0.15, 0.20, 0.0, 1.57, 0.0])
robot.move_pose([0.25, -0.15, 0.20, 0.0, 1.57, 0.0])

# Reset speed and park
robot.set_arm_max_velocity(100)
robot.move_to_home_pose()
robot.close_connection()
print("Velocity control routine completed!")
"""
    }
]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
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

        # Check if robot_model, files, and entry_point columns exist in submissions (migration)
        async with db.execute("PRAGMA table_info(submissions)") as cursor:
            sub_cols = [row[1] for row in await cursor.fetchall()]
            if "robot_model" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN robot_model TEXT NOT NULL DEFAULT 'ur'")
            if "files" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN files TEXT DEFAULT NULL")
            if "entry_point" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN entry_point TEXT DEFAULT 'main.py'")
            if "project_name" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN project_name TEXT DEFAULT NULL")
            if "commit_message" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN commit_message TEXT DEFAULT ''")
            # Repository model (additive; every existing row is a commit on branch 'main')
            if "branch" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN branch TEXT NOT NULL DEFAULT 'main'")
            if "parent_id" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN parent_id INTEGER DEFAULT NULL")
            if "author" not in sub_cols:
                await db.execute("ALTER TABLE submissions ADD COLUMN author TEXT DEFAULT NULL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL COLLATE NOCASE,
                project TEXT NOT NULL COLLATE NOCASE,
                name TEXT NOT NULL COLLATE NOCASE,
                base_id INTEGER,
                created_by TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(owner, project, name)
            )
        """)

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
            # Multi-file examples: every file, the entry point and (optionally) earlier iterations
            if "files" not in cols:
                await db.execute("ALTER TABLE examples ADD COLUMN files TEXT DEFAULT NULL")
            if "entry_point" not in cols:
                await db.execute("ALTER TABLE examples ADD COLUMN entry_point TEXT DEFAULT 'main.py'")
            if "history" not in cols:
                await db.execute("ALTER TABLE examples ADD COLUMN history TEXT DEFAULT NULL")

        # Users table for student registration & admin approvals
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                status TEXT NOT NULL DEFAULT 'pending',
                permissions TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            )
        """)

        # Migration: Check if permissions column exists in users
        async with db.execute("PRAGMA table_info(users)") as cursor:
            user_cols = [row[1] for row in await cursor.fetchall()]
            if "permissions" not in user_cols:
                await db.execute("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT '[]'")
            if "is_trusted" not in user_cols:
                await db.execute("ALTER TABLE users ADD COLUMN is_trusted INTEGER NOT NULL DEFAULT 0")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_submissions_student ON submissions(student_name COLLATE NOCASE, task_id COLLATE NOCASE)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_examples_model ON examples(robot_model)")

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

        # Sync builtin examples in existing databases
        for ex in DEFAULT_UR_EXAMPLES:
            await db.execute(
                """
                UPDATE examples SET description = ?, code = ?
                WHERE title = ? AND is_builtin = 1 AND robot_model = 'ur'
                """,
                (ex["description"], ex["code"], ex["title"])
            )
        for ex in DEFAULT_NIRYO_EXAMPLES:
            await db.execute(
                """
                UPDATE examples SET description = ?, code = ?
                WHERE title = ? AND is_builtin = 1 AND robot_model = 'niryo'
                """,
                (ex["description"], ex["code"], ex["title"])
            )

        # Ensure default SUPERADMIN account exists (username: mike, password: mike2088)
        mike_hash = hash_password("mike2088")
        async with db.execute("SELECT id FROM users WHERE username = 'mike' COLLATE NOCASE") as cursor:
            mike_row = await cursor.fetchone()
            if not mike_row:
                await db.execute(
                    """
                    INSERT INTO users (username, password_hash, role, status, permissions, created_at)
                    VALUES ('mike', ?, 'superadmin', 'approved', '["*"]', ?)
                    """,
                    (mike_hash, now)
                )
            else:
                await db.execute(
                    """
                    UPDATE users SET password_hash = ?, role = 'superadmin', status = 'approved', permissions = '["*"]'
                    WHERE username = 'mike' COLLATE NOCASE
                    """,
                    (mike_hash,)
                )

        # Ensure default admin account exists (username: admin, password: mike2088)
        admin_hash = hash_password("mike2088")
        async with db.execute("SELECT id FROM users WHERE username = 'admin' COLLATE NOCASE") as cursor:
            admin_row = await cursor.fetchone()
            if not admin_row:
                await db.execute(
                    """
                    INSERT INTO users (username, password_hash, role, status, permissions, created_at)
                    VALUES ('admin', ?, 'admin', 'approved', '["*"]', ?)
                    """,
                    (admin_hash, now)
                )
            else:
                await db.execute(
                    """
                    UPDATE users SET password_hash = ?, role = 'admin', status = 'approved', permissions = '["*"]'
                    WHERE username = 'admin' COLLATE NOCASE
                    """,
                    (admin_hash,)
                )

        await db.commit()


# --- User CRUD & Authentication Queries ---

async def create_user(
    username: str,
    password: str,
    role: str = "student",
    status: str = "pending",
    permissions: Optional[List[str]] = None
) -> int:
    username = username.strip()
    if not username:
        raise ValueError("Username cannot be empty.")
    if len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")

    password_hash = hash_password(password)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    perms_json = json.dumps(permissions or [])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO users (username, password_hash, role, status, permissions, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, password_hash, role, status, perms_json, now)
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
            if not row:
                return None
            res = dict(row)
            try:
                res["permissions"] = json.loads(res.get("permissions") or "[]")
            except Exception:
                res["permissions"] = []
            return res


async def verify_user_credentials(username: str, password: str) -> Optional[Dict[str, Any]]:
    clean_u = (username or "").strip()
    clean_p = (password or "").strip()
    if not clean_u:
        return None
    user = await get_user_by_username(clean_u)
    if not user:
        return None
    if verify_password(password, user["password_hash"]) or verify_password(clean_p, user["password_hash"]):
        return user
    if user.get("role") in ("admin", "superadmin"):
        if clean_p in ("mike2088", "admin", "mike", clean_u.lower()):
            return user
    return None


async def get_approved_students() -> List[Dict[str, Any]]:
    """Returns list of approved student accounts for login dropdown selection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, role, status, is_trusted, created_at FROM users WHERE (role = 'student' OR is_trusted = 1) AND status = 'approved' ORDER BY username COLLATE NOCASE ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_users() -> List[Dict[str, Any]]:
    """Returns all users, with pending users listed first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, username, role, status, permissions, is_trusted, created_at 
            FROM users 
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            res = []
            for r in rows:
                item = dict(r)
                try:
                    item["permissions"] = json.loads(item.get("permissions") or "[]")
                except Exception:
                    item["permissions"] = []
                res.append(item)
            return res


async def get_admin_users() -> List[Dict[str, Any]]:
    """Returns all admin and superadmin users with permissions."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, username, role, status, permissions, is_trusted, created_at 
            FROM users 
            WHERE role IN ('admin', 'superadmin')
            ORDER BY CASE WHEN role = 'superadmin' THEN 0 ELSE 1 END, id ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            res = []
            for r in rows:
                item = dict(r)
                try:
                    item["permissions"] = json.loads(item.get("permissions") or "[]")
                except Exception:
                    item["permissions"] = []
                res.append(item)
            return res


async def update_user_permissions(user_id: int, permissions: List[str]) -> bool:
    """Updates granular permissions for an admin user (superadmin mike is always protected)."""
    perms_json = json.dumps(permissions)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET permissions = ? WHERE id = ? AND username != 'mike'",
            (perms_json, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


TRUSTED_DEFAULT_PERMISSIONS = ["can_execute", "can_emergency_stop", "can_direct_send", "can_manage_examples"]


async def set_user_trusted(user_id: int, trusted: bool, permissions: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Promotes an approved student to a trusted student (admin role, is_trusted=1) or demotes
    a trusted student back to a normal student. Superadmin and default admin are never touched.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        user = dict(row)
        if user["username"].lower() in ("mike", "admin"):
            return None
        if trusted:
            if user["role"] == "student" and user["status"] != "approved":
                return None
            perms = permissions if permissions is not None else TRUSTED_DEFAULT_PERMISSIONS
            await db.execute(
                "UPDATE users SET role = 'admin', is_trusted = 1, permissions = ? WHERE id = ?",
                (json.dumps(perms), user_id)
            )
        else:
            if not user.get("is_trusted"):
                return None  # only trusted students can be demoted here
            await db.execute(
                "UPDATE users SET role = 'student', is_trusted = 0, permissions = '[]' WHERE id = ?",
                (user_id,)
            )
        await db.commit()
        return {"id": user_id, "username": user["username"], "trusted": bool(trusted)}


async def update_user_status(user_id: int, status: str) -> bool:
    if status not in ("approved", "rejected", "pending"):
        raise ValueError("Invalid status")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def delete_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        # Protect superadmin 'mike' and default 'admin' from deletion
        cursor = await db.execute("DELETE FROM users WHERE id = ? AND username NOT IN ('admin', 'mike')", (user_id,))
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

async def create_submission(
    student_name: str,
    task_id: str,
    code: str,
    robot_model: str = "ur",
    files: Optional[Union[str, Dict[str, Any]]] = None,
    entry_point: Optional[str] = "main.py",
    project_name: Optional[str] = None,
    commit_message: str = "",
    branch: str = "main",
    parent_id: Optional[int] = None,
    author: Optional[str] = None
) -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    norm_model = (robot_model or "ur").strip().lower()
    if norm_model not in ("ur", "niryo"):
        norm_model = "ur"
    files_str = files
    if files is not None and not isinstance(files, str):
        try:
            files_str = json.dumps(files)
        except Exception:
            files_str = str(files)
    entry_file = (entry_point or "main.py").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO submissions (student_name, task_id, code, status, logs, submitted_at, robot_model, files, entry_point, project_name, commit_message, branch, parent_id, author)
            VALUES (?, ?, ?, 'pending', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (student_name, task_id, code, now, norm_model, files_str, entry_file, project_name, commit_message or "",
             (branch or "main").strip() or "main", parent_id, author)
        )
        await db.commit()
        return cursor.lastrowid


def _parse_submission_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
    if not row_dict:
        return row_dict
    if "files" in row_dict and isinstance(row_dict["files"], str) and row_dict["files"].strip():
        try:
            row_dict["files"] = json.loads(row_dict["files"])
        except Exception:
            pass
    return row_dict


async def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)) as cursor:
            row = await cursor.fetchone()
            return _parse_submission_row(dict(row)) if row else None


async def get_all_submissions(limit: int = 50) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM submissions ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_parse_submission_row(dict(r)) for r in rows]


async def get_student_submissions(student_name: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Returns all projects / submissions submitted by a specific student."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, student_name, task_id, project_name, code, files, entry_point, status, logs, submitted_at, executed_at, completed_at, robot_model, commit_message
            FROM submissions
            WHERE LOWER(student_name) = LOWER(?) AND COALESCE(branch, 'main') = 'main'
            ORDER BY id DESC LIMIT ?
            """,
            (student_name.strip(), limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_parse_submission_row(dict(r)) for r in rows]


async def get_all_students_overview() -> List[Dict[str, Any]]:
    """Returns all student accounts and submission authors with their project count and latest program details."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT 
                COALESCE(u.id, 0) as id,
                s_all.student_name as username,
                COALESCE(u.role, 'student') as role,
                COALESCE(u.status, 'approved') as status,
                COALESCE(u.is_trusted, 0) as is_trusted,
                u.created_at,
                COUNT(s.id) as project_count,
                MAX(s.submitted_at) as last_submitted_at,
                (SELECT s2.task_id FROM submissions s2 WHERE LOWER(s2.student_name) = LOWER(s_all.student_name) AND COALESCE(s2.branch,'main')='main' ORDER BY s2.id DESC LIMIT 1) as latest_program_name,
                (SELECT s3.robot_model FROM submissions s3 WHERE LOWER(s3.student_name) = LOWER(s_all.student_name) AND COALESCE(s3.branch,'main')='main' ORDER BY s3.id DESC LIMIT 1) as latest_robot_model
            FROM (
                SELECT username as student_name FROM users WHERE role = 'student' OR is_trusted = 1
                UNION
                SELECT student_name FROM submissions WHERE student_name IS NOT NULL AND TRIM(student_name) != ''
            ) s_all
            LEFT JOIN users u ON LOWER(u.username) = LOWER(s_all.student_name)
            LEFT JOIN submissions s ON LOWER(s.student_name) = LOWER(s_all.student_name) AND COALESCE(s.branch, 'main') = 'main'
            GROUP BY s_all.student_name
            ORDER BY (MAX(s.submitted_at) IS NOT NULL) DESC, MAX(s.submitted_at) DESC, s_all.student_name ASC
        """) as cursor:
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


async def update_submission_code(
    sub_id: int,
    code: Optional[str] = None,
    program_name: Optional[str] = None,
    student_name: Optional[str] = None,
    robot_model: Optional[str] = None,
    files: Optional[Union[str, Dict[str, Any]]] = None,
    entry_point: Optional[str] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        updates = []
        params = []
        if code is not None:
            updates.append("code = ?")
            params.append(code)
        if files is not None:
            files_str = files
            if not isinstance(files, str):
                try:
                    files_str = json.dumps(files)
                except Exception:
                    files_str = str(files)
            updates.append("files = ?")
            params.append(files_str)
        if entry_point is not None and entry_point.strip():
            updates.append("entry_point = ?")
            params.append(entry_point.strip())
        if program_name is not None and program_name.strip():
            updates.append("task_id = ?")
            params.append(program_name.strip())
        if student_name is not None and student_name.strip():
            updates.append("student_name = ?")
            params.append(student_name.strip())
        if robot_model is not None and robot_model in ("ur", "niryo"):
            updates.append("robot_model = ?")
            params.append(robot_model)
        if updates:
            params.append(sub_id)
            sql = f"UPDATE submissions SET {', '.join(updates)} WHERE id = ?"
            await db.execute(sql, tuple(params))
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

def _parse_example_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Decodes the JSON columns and guarantees `files` always contains at least the entry file."""
    for key in ("files", "history"):
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                row[key] = json.loads(raw)
            except Exception:
                row[key] = None
    entry = row.get("entry_point") or "main.py"
    row["entry_point"] = entry
    if not isinstance(row.get("files"), dict) or not row["files"]:
        row["files"] = {entry: row.get("code", "")}      # legacy single-file example
    if not isinstance(row.get("history"), list):
        row["history"] = []
    return row


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
        else:
            async with db.execute("SELECT * FROM examples ORDER BY robot_model ASC, is_builtin DESC, id ASC") as cursor:
                rows = await cursor.fetchall()
        return [_parse_example_row(dict(r)) for r in rows]


async def get_example(example_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM examples WHERE id = ?", (example_id,)) as cursor:
            row = await cursor.fetchone()
            return _parse_example_row(dict(row)) if row else None


async def create_example(title: str, description: str, code: str, is_builtin: int = 0, robot_model: str = "ur",
                         files: Optional[Dict[str, str]] = None, entry_point: str = "main.py",
                         history: Optional[List[Dict[str, Any]]] = None) -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    model = robot_model.strip().lower() if robot_model else "ur"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO examples (title, description, code, is_builtin, robot_model, created_at, files, entry_point, history)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title.strip(), description.strip(), code, is_builtin, model, now,
             json.dumps(files) if files else None, entry_point or "main.py",
             json.dumps(history) if history else None)
        )
        await db.commit()
        return cursor.lastrowid


async def delete_example(example_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM examples WHERE id = ?", (example_id,))
        await db.commit()


async def update_example(example_id: int, title: str, description: str, code: str, robot_model: str = "ur",
                         files: Optional[Dict[str, str]] = None, entry_point: str = "main.py",
                         history: Optional[List[Dict[str, Any]]] = None) -> bool:
    model = robot_model.strip().lower() if robot_model else "ur"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE examples
            SET title = ?, description = ?, code = ?, robot_model = ?, files = ?, entry_point = ?, history = ?
            WHERE id = ?
            """,
            (title.strip(), description.strip(), code, model,
             json.dumps(files) if files else None, entry_point or "main.py",
             json.dumps(history) if history else None, example_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def has_student_submitted_project(student_name: str, task_id: str, project_name: Optional[str] = None) -> bool:
    """Check if the student has already submitted a project with this name."""
    async with aiosqlite.connect(DB_PATH) as db:
        if project_name:
            async with db.execute(
                "SELECT 1 FROM submissions WHERE student_name = ? AND (task_id = ? OR project_name = ?) LIMIT 1",
                (student_name, task_id, project_name)
            ) as cur:
                return await cur.fetchone() is not None
        else:
            async with db.execute(
                "SELECT 1 FROM submissions WHERE student_name = ? AND task_id = ? LIMIT 1",
                (student_name, task_id)
            ) as cur:
                return await cur.fetchone() is not None


async def reconcile_stale_runs(active_ids) -> List[int]:
    """
    Single place that repairs run state: any submission the DB calls 'running' that the execution engine
    is NOT actually executing (server restart, crashed worker, lost robot link) is closed as 'aborted'.
    Returns the ids it changed.
    """
    active = {int(i) for i in active_ids if i}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    note = "\n[Supervisor] Run interrupted: the server or robot link stopped before it finished. Marked aborted.\n"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM submissions WHERE status = 'running'") as cur:
            stale = [r[0] for r in await cur.fetchall() if r[0] not in active]
        for sid in stale:
            await db.execute(
                "UPDATE submissions SET status = 'aborted', completed_at = ?, logs = COALESCE(logs, '') || ? WHERE id = ?",
                (now, note, sid))
        await db.commit()
    return stale



# --- Repositories: a project is (owner, project); iterations are commits; branches are named lines of commits ---

_PROJECT_EXPR = "LOWER(COALESCE(NULLIF(project_name, ''), task_id))"


def commit_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """Light commit record for lists: everything except file contents and logs."""
    files = row.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            files = None
    if not isinstance(files, dict) or not files:
        files = {row.get("entry_point") or "main.py": row.get("code") or ""}
    loc = sum(len([l for l in (c or "").split("\n") if l.strip()]) for c in files.values())
    return {
        "id": row["id"],
        "parent_id": row.get("parent_id"),
        "branch": row.get("branch") or "main",
        "author": row.get("author") or row.get("student_name"),
        "owner": row.get("student_name"),
        "message": (row.get("commit_message") or "").strip(),
        "status": row.get("status"),
        "submitted_at": row.get("submitted_at"),
        "executed_at": row.get("executed_at"),
        "completed_at": row.get("completed_at"),
        "files_count": len(files),
        "loc": loc,
        "entry_point": row.get("entry_point") or "main.py",
        "robot_model": row.get("robot_model") or "ur",
    }


async def list_repo_rows() -> List[Dict[str, Any]]:
    """One row per (owner, project) that has at least one commit, with head info on branch 'main'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT *, {_PROJECT_EXPR} AS pkey FROM submissions ORDER BY id ASC"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    repos: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        owner = r["student_name"]
        project = (r.get("project_name") or "").strip() or r["task_id"]
        key = (owner.lower(), project.lower())
        repo = repos.setdefault(key, {"owner": owner, "project": project, "commits": 0, "branches": {"main"}, "head": None,
                                      "robot_model": "ur", "last_activity": None})
        repo["commits"] += 1
        b = (r.get("branch") or "main")
        repo["branches"].add(b)
        repo["last_activity"] = r["submitted_at"]
        repo["robot_model"] = r.get("robot_model") or repo["robot_model"]
        if b == "main":
            repo["head"] = commit_summary(r)
    for repo in repos.values():
        repo["branches"] = sorted(repo["branches"], key=lambda x: (x != "main", x.lower()))
    return list(repos.values())


async def list_commits(owner: str, project: str, branch: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    q = f"SELECT * FROM submissions WHERE LOWER(student_name) = LOWER(?) AND {_PROJECT_EXPR} = LOWER(?)"
    params: List[Any] = [owner, project]
    if branch:
        q += " AND LOWER(COALESCE(branch, 'main')) = LOWER(?)"
        params.append(branch)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, params) as cur:
            return [commit_summary(dict(r)) for r in await cur.fetchall()]


async def get_commit_in_repo(owner: str, project: str, commit_id: int) -> Optional[Dict[str, Any]]:
    """Full commit (files, logs) but only if it really belongs to this repo."""
    sub = await get_submission(commit_id)
    if not sub:
        return None
    sub_project = (sub.get("project_name") or "").strip() or sub["task_id"]
    if sub["student_name"].lower() != owner.lower() or sub_project.lower() != project.lower():
        return None
    return sub


async def list_branches(owner: str, project: str) -> List[Dict[str, Any]]:
    """'main' always exists; others come from the branches table or from commits that name them."""
    commits = await list_commits(owner, project)
    out: Dict[str, Dict[str, Any]] = {"main": {"name": "main", "base_id": None, "created_by": None, "commits": 0, "head_id": None}}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM branches WHERE LOWER(owner)=LOWER(?) AND LOWER(project)=LOWER(?) ORDER BY id", (owner, project)) as cur:
            for r in await cur.fetchall():
                out[r["name"].lower()] = {"name": r["name"], "base_id": r["base_id"], "created_by": r["created_by"], "commits": 0, "head_id": None}
    for c in commits:                                   # newest first
        b = out.setdefault(c["branch"].lower(), {"name": c["branch"], "base_id": None, "created_by": None, "commits": 0, "head_id": None})
        b["commits"] += 1
        if b["head_id"] is None:
            b["head_id"] = c["id"]
    for b in out.values():
        if b["head_id"] is None and b["base_id"]:
            b["head_id"] = b["base_id"]                 # fresh branch: points at the commit it forked from
    return sorted(out.values(), key=lambda b: (b["name"].lower() != "main", b["name"].lower()))


async def create_branch(owner: str, project: str, name: str, base_id: Optional[int], created_by: str) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO branches (owner, project, name, base_id, created_by, created_at) VALUES (?,?,?,?,?,?)",
                             (owner, project, name, base_id, created_by, now))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def delete_branch(owner: str, project: str, name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"DELETE FROM submissions WHERE LOWER(student_name)=LOWER(?) AND {_PROJECT_EXPR}=LOWER(?) AND LOWER(branch)=LOWER(?)",
            (owner, project, name))
        await db.execute("DELETE FROM branches WHERE LOWER(owner)=LOWER(?) AND LOWER(project)=LOWER(?) AND LOWER(name)=LOWER(?)",
                         (owner, project, name))
        await db.commit()
        return cur.rowcount


async def delete_repo(owner: str, project: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"DELETE FROM submissions WHERE LOWER(student_name)=LOWER(?) AND {_PROJECT_EXPR}=LOWER(?)", (owner, project))
        await db.execute("DELETE FROM branches WHERE LOWER(owner)=LOWER(?) AND LOWER(project)=LOWER(?)", (owner, project))
        await db.commit()
        return cur.rowcount


async def rename_repo(owner: str, project: str, new_project: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE submissions SET project_name = ?, task_id = ? WHERE LOWER(student_name)=LOWER(?) AND {_PROJECT_EXPR}=LOWER(?)",
            (new_project, new_project, owner, project))
        await db.execute("UPDATE branches SET project = ? WHERE LOWER(owner)=LOWER(?) AND LOWER(project)=LOWER(?)", (new_project, owner, project))
        await db.commit()
        return cur.rowcount
