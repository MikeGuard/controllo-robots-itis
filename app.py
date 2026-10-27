"""
FastAPI Server for Universal Robots Student Lab Platform.
Provides Student Web Portal, Instructor Control Dashboard, WebSocket log streaming, User Authentication,
multi-robot examples (UR3 vs Niryo Ned), and student saved projects history.
"""

import asyncio
import os
import sys
import secrets
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set, Any

# Ensure ur_platform directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json

import database
import config
from execution_engine import engine
from safety import validate_python_code
from simulator import simulate_script, run_simulation_isolated
from telemetry import hub as telemetry_hub
import workspace


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# In-memory session store: token -> {"id": int, "username": str, "role": str}
active_sessions: Dict[str, Dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database
    await database.init_db()
    yield


app = FastAPI(title="UR Robotics Lab Platform", lifespan=lifespan)

# Allow CORS for student and instructor browser dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Connected WebSockets for log broadcasting
active_log_sockets: Dict[int, Set[WebSocket]] = {}
admin_sockets: Set[WebSocket] = set()


# --- Pydantic Request Models ---

class SyntaxCheckRequest(BaseModel):
    code: str
    robot_model: Optional[str] = None
    files: Optional[Dict[str, str]] = None
    entry_point: Optional[str] = "main.py"


class SubmissionRequest(BaseModel):
    program_name: Optional[str] = None
    student_name: Optional[str] = None
    task_id: Optional[str] = ""
    code: str
    robot_model: Optional[str] = "ur"
    files: Optional[Dict[str, str]] = None
    entry_point: Optional[str] = "main.py"
    project_name: Optional[str] = None


class CreateExampleRequest(BaseModel):
    title: str
    description: Optional[str] = ""
    code: str
    robot_model: Optional[str] = "ur"


class AdminSendCodeRequest(BaseModel):
    program_name: Optional[str] = "Admin Routine"
    code: str
    execute_now: Optional[bool] = False
    task_id: Optional[str] = "Admin"
    files: Optional[Dict[str, str]] = None
    entry_point: Optional[str] = "main.py"
    project_name: Optional[str] = None
    robot_model: Optional[str] = None


class WorkspaceCreateRequest(BaseModel):
    name: str
    files: Optional[Dict[str, str]] = None
    entry_point: Optional[str] = "main.py"
    robot_model: Optional[str] = None
    template: Optional[str] = None


class WorkspaceSaveRequest(BaseModel):
    files: Dict[str, str]
    entry_point: Optional[str] = None
    robot_model: Optional[str] = None


class WorkspaceRenameRequest(BaseModel):
    new_name: str



class TrustedUpdateRequest(BaseModel):
    trusted: bool
    permissions: Optional[List[str]] = None


class UpdateSubmissionRequest(BaseModel):
    code: Optional[str] = None
    program_name: Optional[str] = None
    files: Optional[Dict[str, str]] = None
    entry_point: Optional[str] = None
    robot_model: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    robot_ip: Optional[str] = None          # legacy / UR IP
    ur_ip: Optional[str] = None
    niryo_ip: Optional[str] = None
    robot_model: Optional[str] = None       # "ur" | "niryo"


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class UserStatusUpdateRequest(BaseModel):
    status: str


class CreateAdminUserRequest(BaseModel):
    username: str
    password: str
    permissions: List[str] = []


class UpdatePermissionsRequest(BaseModel):
    permissions: List[str]


# --- Web Page Views ---

@app.get("/")
async def get_student_portal():
    return FileResponse(
        os.path.join(STATIC_DIR, "student.html"),
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}
    )


@app.get("/admin")
async def get_instructor_dashboard():
    # Never cache: the console must always (re)authenticate and load fresh code.
    return FileResponse(
        os.path.join(STATIC_DIR, "instructor.html"),
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}
    )


# --- Helper Functions ---

# Short-lived cache and locks so multiple dashboard polls don't spawn redundant pings
_reachability_cache: Dict[tuple, tuple] = {}
_reachability_locks: Dict[tuple, asyncio.Lock] = {}
_REACHABILITY_TTL_SEC = 2.5


async def check_robot_reachability(ip: Optional[str] = None, model: Optional[str] = None, use_cache: bool = True):
    target_ip = (ip or "").strip()
    target_model = (model or "").strip().lower()

    if not target_ip:
        if target_model == "niryo":
            target_ip = config.NIRYO_IP
        elif target_model == "ur":
            target_ip = config.UR_IP
        else:
            target_ip = config.ROBOT_IP

    if not target_model:
        target_model = "niryo" if target_ip == config.NIRYO_IP else "ur"

    cache_key = (target_ip, target_model)
    loop = asyncio.get_running_loop()
    now_ts = loop.time()
    cached = _reachability_cache.get(cache_key)
    if use_cache and cached and (now_ts - cached[0] < _REACHABILITY_TTL_SEC):
        return cached[1]

    # In-flight lock to deduplicate concurrent requests for identical target
    if cache_key not in _reachability_locks:
        _reachability_locks[cache_key] = asyncio.Lock()
    lock = _reachability_locks[cache_key]

    async with lock:
        now_ts = loop.time()
        cached = _reachability_cache.get(cache_key)
        if use_cache and cached and (now_ts - cached[0] < _REACHABILITY_TTL_SEC):
            return cached[1]

        ping_ok = False
        try:
            # -n disables reverse DNS lookup which otherwise causes 3-5s delays on local IPs
            if sys.platform == "darwin":
                cmd = ["ping", "-n", "-c", "1", "-W", "800", target_ip]
            elif sys.platform == "win32":
                cmd = ["ping", "-n", "1", "-w", "800", target_ip]
            else:
                cmd = ["ping", "-n", "-c", "1", "-W", "1", target_ip]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
                ping_ok = (proc.returncode == 0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                ping_ok = False
        except Exception:
            ping_ok = False

        # Fast TCP port verification fallback (Niryo 9090 vs UR 30004/30002)
        port_ok = False
        if not ping_ok:
            ports = [config.NIRYO_PORT] if target_model == "niryo" else [config.ROBOT_RTDE_PORT, config.ROBOT_SECONDARY_PORT]
            for port in ports:
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(target_ip, port), timeout=0.35)
                    writer.close()
                    await writer.wait_closed()
                    port_ok = True
                    break
                except Exception:
                    continue

        is_ready = ping_ok or port_ok
        result = (is_ready, ping_ok, port_ok)
        _reachability_cache[cache_key] = (now_ts, result)
        return result


# --- Authentication Endpoints ---

@app.post("/api/auth/register")
async def register_user(payload: RegisterRequest):
    """Registers a new student account (sets status to 'pending' awaiting admin approval)."""
    username = payload.username.strip()
    password = payload.password.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")

    existing = await database.get_user_by_username(username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists. Please choose a different name.")

    try:
        user_id = await database.create_user(username, password, role="student", status="pending")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create account: {e}")

    await broadcast_to_admins({"type": "new_user_registered", "username": username, "id": user_id})
    return {
        "status": "pending",
        "user_id": user_id,
        "username": username,
        "message": "Account created successfully. Awaiting instructor approval before login."
    }


@app.get("/api/auth/students")
async def get_approved_students_list():
    """Returns list of approved students for dropdown selection on the login page."""
    students = await database.get_approved_students()
    return {"students": students}


def verify_admin_permission(authorization: Optional[str], required_perm: str) -> Dict[str, Any]:
    """Helper to verify caller has specific permission or is superadmin."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    role = session.get("role")
    if role == "superadmin":
        return session
    perms = session.get("permissions") or []
    if "*" in perms or required_perm in perms:
        return session
    raise HTTPException(status_code=403, detail=f"Permission denied: Missing '{required_perm}' privilege.")


def verify_any_admin(authorization: Optional[str]) -> Dict[str, Any]:
    """Requires a valid admin or superadmin session (no specific permission)."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    if session.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Instructor access required.")
    return session


def verify_authenticated_user(authorization: Optional[str]) -> Dict[str, Any]:
    """Requires any valid authenticated session (student, admin, superadmin)."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    return session


@app.post("/api/auth/login")
async def login_user(payload: LoginRequest):
    """Authenticates a student, admin, or superadmin (mike) user."""
    username = payload.username.strip()
    password = payload.password.strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    user = await database.verify_user_credentials(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    if user["status"] == "pending":
        raise HTTPException(
            status_code=403,
            detail="Your account is currently pending instructor approval. Please ask your instructor to approve your registration."
        )
    elif user["status"] == "rejected":
        raise HTTPException(
            status_code=403,
            detail="Your account registration has been rejected by the instructor."
        )

    # Determine user permissions
    role = user.get("role", "student")
    if role == "superadmin":
        perms = ["*"]
    elif role == "admin":
        perms = user.get("permissions") or []
        if not perms and not user.get("is_trusted"):
            perms = ["can_execute", "can_emergency_stop", "can_edit_config", "can_manage_examples", "can_manage_students", "can_direct_send"]
    else:
        perms = []

    # Generate session token
    token = secrets.token_hex(24)
    active_sessions[token] = {
        "id": user["id"],
        "username": user["username"],
        "role": role,
        "permissions": perms,
        "is_trusted": bool(user.get("is_trusted"))
    }

    return {
        "status": "success",
        "token": token,
        "role": role,
        "username": user["username"],
        "permissions": perms,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": role,
            "permissions": perms,
            "is_trusted": bool(user.get("is_trusted"))
        }
    }


@app.get("/api/auth/me")
async def get_current_user(authorization: Optional[str] = Header(default=None)):
    """Validates session token and returns active user info."""
    if not authorization or not isinstance(authorization, str):
        return {"authenticated": False}

    token = authorization.replace("Bearer ", "").strip()
    if token in active_sessions:
        session = active_sessions[token]
        return {
            "authenticated": True,
            "role": session.get("role"),
            "username": session.get("username"),
            "permissions": session.get("permissions", []),
            "user": session
        }
    return {"authenticated": False}


@app.post("/api/auth/logout")
async def logout_user(authorization: Optional[str] = Header(default=None)):
    """Clears active session token."""
    if authorization and isinstance(authorization, str):
        token = authorization.replace("Bearer ", "").strip()
        active_sessions.pop(token, None)
    return {"status": "logged_out"}


# --- Admin & Superadmin User Management Endpoints ---

@app.get("/api/admin/users")
async def get_all_users_admin(authorization: Optional[str] = Header(default=None)):
    """Lists all users (pending, approved, rejected) for the instructor."""
    verify_any_admin(authorization)
    users = await database.get_all_users()
    return {"users": users}


@app.post("/api/admin/users/{user_id}/status")
async def update_user_status_endpoint(
    user_id: int,
    payload: UserStatusUpdateRequest,
    authorization: Optional[str] = Header(default=None)
):
    """Allows instructor to approve or reject a student account."""
    verify_admin_permission(authorization, "can_manage_students")
    status = payload.status.lower().strip()
    if status not in ("approved", "rejected", "pending"):
        raise HTTPException(status_code=400, detail="Status must be 'approved', 'rejected', or 'pending'.")

    success = await database.update_user_status(user_id, status)
    if not success:
        raise HTTPException(status_code=404, detail="User not found.")

    await broadcast_to_admins({"type": "user_status_changed", "id": user_id, "status": status})
    return {"status": "updated", "id": user_id, "new_status": status}


@app.post("/api/admin/users/{user_id}/approve")
async def approve_user_endpoint(user_id: int, authorization: Optional[str] = Header(default=None)):
    """Convenience endpoint allowing instructor to approve a student account."""
    verify_admin_permission(authorization, "can_manage_students")
    success = await database.update_user_status(user_id, "approved")
    if not success:
        raise HTTPException(status_code=404, detail="User not found.")
    await broadcast_to_admins({"type": "user_status_changed", "id": user_id, "status": "approved"})
    return {"status": "approved", "id": user_id}


@app.post("/api/admin/users/{user_id}/reject")
async def reject_user_endpoint(user_id: int, authorization: Optional[str] = Header(default=None)):
    """Convenience endpoint allowing instructor to reject a student registration."""
    verify_admin_permission(authorization, "can_manage_students")
    success = await database.update_user_status(user_id, "rejected")
    if not success:
        raise HTTPException(status_code=404, detail="User not found.")
    await broadcast_to_admins({"type": "user_status_changed", "id": user_id, "status": "rejected"})
    return {"status": "rejected", "id": user_id}


@app.delete("/api/admin/users/{user_id}")
async def delete_user_endpoint(user_id: int, authorization: Optional[str] = Header(default=None)):
    """Allows instructor to remove a user account."""
    verify_admin_permission(authorization, "can_manage_students")
    success = await database.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot delete superadmin/admin user or user not found.")

    await broadcast_to_admins({"type": "user_deleted", "id": user_id})
    return {"status": "deleted", "id": user_id}


@app.get("/api/admin/admins")
async def get_admins_endpoint(authorization: Optional[str] = Header(default=None)):
    """Lists all admin users and their permissions (superadmin only)."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session or session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required.")

    admins = await database.get_admin_users()
    return {"admins": admins}


@app.post("/api/admin/create-admin")
async def create_admin_endpoint(
    payload: CreateAdminUserRequest,
    authorization: Optional[str] = Header(default=None)
):
    """Allows superadmin (mike) to create new admin users with granular permissions."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session or session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Only Superadmin can create new admin accounts.")

    username = payload.username.strip()
    password = payload.password.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")

    existing = await database.get_user_by_username(username)
    if existing:
        raise HTTPException(status_code=400, detail=f"User '{username}' already exists.")

    try:
        user_id = await database.create_user(
            username=username,
            password=password,
            role="admin",
            status="approved",
            permissions=payload.permissions
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create admin: {e}")

    await broadcast_to_admins({"type": "admin_created", "id": user_id, "username": username})
    return {"status": "created", "id": user_id, "username": username, "permissions": payload.permissions}


@app.put("/api/admin/users/{user_id}/permissions")
async def update_permissions_endpoint(
    user_id: int,
    payload: UpdatePermissionsRequest,
    authorization: Optional[str] = Header(default=None)
):
    """Allows superadmin (mike) to modify granular permissions of an admin."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session or session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Only Superadmin can modify admin permissions.")

    success = await database.update_user_permissions(user_id, payload.permissions)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot modify permissions for this user (or user not found).")

    # Sync live session if admin is currently connected
    for tok, sess in list(active_sessions.items()):
        if sess.get("id") == user_id:
            sess["permissions"] = payload.permissions

    await broadcast_to_admins({"type": "admin_permissions_updated", "id": user_id})
    return {"status": "updated", "id": user_id, "permissions": payload.permissions}


# --- Trusted Students (students promoted to limited admins) ---

@app.post("/api/admin/users/{user_id}/trusted")
async def set_trusted_student_endpoint(
    user_id: int,
    payload: TrustedUpdateRequest,
    authorization: Optional[str] = Header(default=None)
):
    """Superadmin promotes an approved student to a trusted student (admin) or demotes them back."""
    session = verify_any_admin(authorization)
    if session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Only Superadmin can manage trusted students.")

    result = await database.set_user_trusted(user_id, payload.trusted, payload.permissions)
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Cannot change trust for this user (must be an approved student, or an existing trusted student to demote)."
        )

    # Sync live sessions so the change applies immediately
    for tok, sess in list(active_sessions.items()):
        if sess.get("id") == user_id:
            if payload.trusted:
                sess["role"] = "admin"
                sess["is_trusted"] = True
                sess["permissions"] = payload.permissions if payload.permissions is not None else database.TRUSTED_DEFAULT_PERMISSIONS
            else:
                active_sessions.pop(tok, None)

    await broadcast_to_admins({"type": "admin_created" if payload.trusted else "user_status_changed", "id": user_id})
    return {"status": "updated", **result}


# --- Project Workspaces (one folder per project, per user) ---

def _workspace_owner(authorization: Optional[str], owner: Optional[str] = None) -> str:
    """Resolves whose workspace is being accessed. Only admins may access another user's workspace."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = authorization.replace("Bearer ", "").strip()
    session = active_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    if owner and owner.strip() and owner.strip().lower() != session["username"].lower():
        if session.get("role") not in ("admin", "superadmin"):
            raise HTTPException(status_code=403, detail="You can only access your own projects.")
        return owner.strip()
    return session["username"]


def _ws_error(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


@app.get("/api/workspace/projects")
async def list_workspace_projects(owner: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    user = _workspace_owner(authorization, owner)
    try:
        return {"owner": user, "projects": workspace.list_projects(user)}
    except workspace.WorkspaceError as e:
        raise _ws_error(e)


@app.post("/api/workspace/projects")
async def create_workspace_project(
    payload: WorkspaceCreateRequest,
    owner: Optional[str] = None,
    authorization: Optional[str] = Header(default=None)
):
    user = _workspace_owner(authorization, owner)
    try:
        return workspace.create_project(
            user,
            payload.name,
            payload.files,
            payload.entry_point or "main.py",
            payload.robot_model,
            payload.template
        )
    except workspace.WorkspaceError as e:
        raise _ws_error(e)


@app.get("/api/workspace/projects/{name}")
async def get_workspace_project(name: str, owner: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    user = _workspace_owner(authorization, owner)
    try:
        return workspace.load_project(user, name)
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/api/workspace/projects/{name}")
async def save_workspace_project(
    name: str,
    payload: WorkspaceSaveRequest,
    owner: Optional[str] = None,
    authorization: Optional[str] = Header(default=None)
):
    user = _workspace_owner(authorization, owner)
    try:
        return workspace.save_project(user, name, payload.files, payload.entry_point, payload.robot_model)
    except workspace.WorkspaceError as e:
        raise _ws_error(e)


@app.delete("/api/workspace/projects/{name}")
async def delete_workspace_project(name: str, owner: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    user = _workspace_owner(authorization, owner)
    try:
        if not workspace.delete_project(user, name):
            raise HTTPException(status_code=404, detail="Project not found.")
    except workspace.WorkspaceError as e:
        raise _ws_error(e)
    return {"status": "deleted", "name": name}


@app.post("/api/workspace/projects/{name}/rename")
async def rename_workspace_project(
    name: str,
    payload: WorkspaceRenameRequest,
    owner: Optional[str] = None,
    authorization: Optional[str] = Header(default=None)
):
    user = _workspace_owner(authorization, owner)
    try:
        return workspace.rename_project(user, name, payload.new_name)
    except workspace.WorkspaceError as e:
        raise _ws_error(e)



# --- Robot Configuration & Status ---

@app.post("/api/config")
async def update_config(payload: ConfigUpdateRequest, authorization: Optional[str] = Header(default=None)):
    """Allows the instructor to change the robot model/IP from the admin dashboard."""
    verify_admin_permission(authorization, "can_edit_config")
    import json

    # Determine new model
    new_model = (payload.robot_model or "").strip().lower()
    if new_model and new_model not in ("ur", "niryo"):
        raise HTTPException(status_code=400, detail="robot_model must be 'ur' or 'niryo'.")
    if not new_model:
        new_model = config.ROBOT_MODEL  # keep current

    # Determine per-robot IPs (fall back to existing values)
    new_ur_ip = (payload.ur_ip or payload.robot_ip or "").strip() or config.UR_IP
    new_niryo_ip = (payload.niryo_ip or "").strip() or config.NIRYO_IP

    if not new_ur_ip:
        raise HTTPException(status_code=400, detail="UR IP cannot be empty.")
    if not new_niryo_ip:
        raise HTTPException(status_code=400, detail="Niryo IP cannot be empty.")

    # Apply to live config
    config.ROBOT_MODEL = new_model
    config.UR_IP = new_ur_ip
    config.NIRYO_IP = new_niryo_ip
    config.ROBOT_IP = new_niryo_ip if new_model == "niryo" else new_ur_ip

    # Persist to runtime_config.json
    try:
        with open(config.RUNTIME_CONFIG_PATH, "w") as f:
            json.dump({
                "robot_model": new_model,
                "ur_ip": new_ur_ip,
                "niryo_ip": new_niryo_ip,
                "robot_ip": config.ROBOT_IP   # legacy compat
            }, f, indent=2)
    except Exception as e:
        print(f"Could not persist runtime_config.json: {e}")

    await broadcast_to_admins({
        "type": "config_updated",
        "robot_model": new_model,
        "robot_ip": config.ROBOT_IP,
        "ur_ip": new_ur_ip,
        "niryo_ip": new_niryo_ip,
    })
    return {
        "status": "updated",
        "robot_model": new_model,
        "robot_ip": config.ROBOT_IP,
        "ur_ip": new_ur_ip,
        "niryo_ip": new_niryo_ip,
    }


@app.get("/api/config")
async def get_config():
    """Returns the central robot configuration defined in config.py."""
    return {
        "robot_model": config.ROBOT_MODEL,
        "robot_ip": config.ROBOT_IP,
        "ur_ip": config.UR_IP,
        "niryo_ip": config.NIRYO_IP,
        "rtde_port": config.ROBOT_RTDE_PORT,
        "secondary_port": config.ROBOT_SECONDARY_PORT,
        "niryo_port": config.NIRYO_PORT,
        "timeout_sec": config.EXECUTION_TIMEOUT_SEC
    }


@app.get("/api/robot-status")
async def get_robot_status(ip: Optional[str] = None, model: Optional[str] = None):
    """
    Pings robot IPs and checks connectivity concurrently for both arms.
    Returns status for the target model as well as concurrent status for both UR3 and Niryo.
    """
    ur_task = check_robot_reachability(config.UR_IP, model="ur")
    niryo_task = check_robot_reachability(config.NIRYO_IP, model="niryo")
    (ur_ready, ur_ping, ur_port), (niryo_ready, niryo_ping, niryo_port) = await asyncio.gather(ur_task, niryo_task)

    target_model = (model or "").strip().lower()
    if target_model not in ("ur", "niryo"):
        target_model = config.ROBOT_MODEL

    if ip:
        target_ip = ip.strip()
        target_model = target_model or ("niryo" if target_ip == config.NIRYO_IP else "ur")
    elif target_model == "niryo":
        target_ip = config.NIRYO_IP
    else:
        target_ip = config.UR_IP

    is_ready = niryo_ready if target_model == "niryo" else ur_ready
    ping_ok = niryo_ping if target_model == "niryo" else ur_ping
    port_ok = niryo_port if target_model == "niryo" else ur_port

    target_halted = engine.is_emergency_halted(target_model)

    return {
        "ip": target_ip,
        "robot_model": target_model,
        "is_ready": is_ready and not target_halted,
        "ping_ok": ping_ok,
        "port_ok": port_ok,
        "is_emergency_halted": target_halted,
        "is_executing": engine.is_model_executing(target_model),
        "status_text": "EMERGENCY STOPPED" if target_halted else ("READY" if is_ready else "NOT READY"),
        "message": (
            f"EMERGENCY STOP ACTIVE on {target_model.upper()}" if target_halted
            else (f"Robot ({target_model.upper()}) at {target_ip} is online and ready." if is_ready else f"Robot ({target_model.upper()}) at {target_ip} did not respond to ping. Not ready.")
        ),
        "robots": {
            "ur": {
                "model": "ur",
                "label": "UR3 Arm",
                "ip": config.UR_IP,
                "is_ready": ur_ready and not engine.is_emergency_halted("ur"),
                "ping_ok": ur_ping,
                "port_ok": ur_port,
                "is_executing": engine.is_model_executing("ur"),
                "active_submission_id": engine.active_submission_id("ur"),
                "is_emergency_halted": engine.is_emergency_halted("ur")
            },
            "niryo": {
                "model": "niryo",
                "label": "Niryo Ned",
                "ip": config.NIRYO_IP,
                "is_ready": niryo_ready and not engine.is_emergency_halted("niryo"),
                "ping_ok": niryo_ping,
                "port_ok": niryo_port,
                "is_executing": engine.is_model_executing("niryo"),
                "active_submission_id": engine.active_submission_id("niryo"),
                "is_emergency_halted": engine.is_emergency_halted("niryo")
            }
        }
    }


@app.post("/api/emergency-reset")
async def reset_emergency_stop(model: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    """Allows instructor to clear the emergency halt latch and return robot status to normal."""
    verify_admin_permission(authorization, "can_emergency_stop")
    engine.reset_emergency(model)
    await broadcast_to_admins({"type": "emergency_reset", "robot_model": model or "all"})
    return {"status": "ok", "message": "Emergency stop cleared."}


# --- Code Verification & Simulation ---

@app.post("/api/check-syntax")
async def check_syntax(payload: SyntaxCheckRequest):
    files = payload.files or {}
    project_modules = {os.path.splitext(f)[0] for f in files.keys() if f.endswith(".py")}

    valid = True
    errors = []

    if files:
        for fname, fcontent in files.items():
            if fname.endswith(".py"):
                f_valid, f_errors = validate_python_code(fcontent or "", project_modules=project_modules)
                if not f_valid:
                    valid = False
                    for err in f_errors:
                        errors.append(f"[{fname}] {err}")
    else:
        valid, errors = validate_python_code(payload.code or "", project_modules=project_modules)

    return {"valid": valid, "errors": errors}


@app.post("/api/simulate")
async def simulate_code(payload: SyntaxCheckRequest):
    """
    Simulates code execution in a mock environment and returns trajectory waypoints
    for smooth 3D browser visualization. Supports multi-file project simulation.
    """
    files = payload.files or {}
    project_modules = {os.path.splitext(f)[0] for f in files.keys() if f.endswith(".py")}
    entry_point = (payload.entry_point or "main.py").strip()

    valid = True
    errors = []

    if files:
        for fname, fcontent in files.items():
            if fname.endswith(".py"):
                f_valid, f_errors = validate_python_code(fcontent or "", project_modules=project_modules)
                if not f_valid:
                    valid = False
                    for err in f_errors:
                        errors.append(f"[{fname}] {err}")
    else:
        valid, errors = validate_python_code(payload.code or "", project_modules=project_modules)

    if not valid:
        return JSONResponse(
            status_code=422,
            content={"success": False, "detail": "Safety or syntax check failed.", "errors": errors}
        )

    code_to_run = payload.code
    if files and entry_point in files and files[entry_point].strip():
        code_to_run = files[entry_point]

    res = await run_simulation_isolated(
        code_to_run,
        robot_model=payload.robot_model,
        files=payload.files,
        entry_point=entry_point
    )
    return res


# --- Submissions Management & Execution ---

@app.post("/api/submit")
async def submit_code(payload: SubmissionRequest):
    """
    Submits student code or multi-file project for execution.
    Requires student_name and program_name, and enforces program name uniqueness per student.
    """
    student_name = (payload.student_name or "").strip()
    program_name = (payload.program_name or payload.task_id or "").strip()

    if not student_name:
        raise HTTPException(status_code=400, detail="Student Name is required. Please log in first.")
    if not program_name:
        raise HTTPException(status_code=400, detail="Program Name is required.")

    # Check for duplicate program name for this student
    is_duplicate = await database.is_program_name_duplicate(student_name, program_name)
    if is_duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"You have already submitted a program named '{program_name}'. Please choose a new or versioned program name."
        )

    files = payload.files or {}
    project_modules = {os.path.splitext(f)[0] for f in files.keys() if f.endswith(".py")}
    entry_point = (payload.entry_point or "main.py").strip()

    valid = True
    errors = []
    if files:
        for fname, fcontent in files.items():
            if fname.endswith(".py"):
                f_valid, f_errors = validate_python_code(fcontent or "", project_modules=project_modules)
                if not f_valid:
                    valid = False
                    for err in f_errors:
                        errors.append(f"[{fname}] {err}")
    else:
        valid, errors = validate_python_code(payload.code or "", project_modules=project_modules)

    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": "Safety or syntax check failed.", "errors": errors}
        )

    code_to_save = payload.code
    if files and entry_point in files and files[entry_point].strip():
        code_to_save = files[entry_point]

    # Determine robot model
    all_text = code_to_save + " " + " ".join(files.values())
    model = (payload.robot_model or "").strip().lower()
    if model not in ("ur", "niryo"):
        model = "niryo" if ("pyniryo" in all_text or "NiryoRobot" in all_text) else "ur"

    sub_id = await database.create_submission(
        student_name=student_name,
        task_id=program_name,
        code=code_to_save,
        robot_model=model,
        files=payload.files,
        entry_point=payload.entry_point or "main.py",
        project_name=(payload.project_name or None)
    )

    # Notify instructor dashboard of new submission
    await broadcast_to_admins({
        "type": "new_submission",
        "id": sub_id,
        "student": student_name,
        "program": program_name,
        "robot_model": model
    })
    return {
        "submission_id": sub_id,
        "status": "pending",
        "student_name": student_name,
        "program_name": program_name,
        "robot_model": model,
        "message": f"Program '{program_name}' ({model.upper()}) sent for execution."
    }


@app.get("/api/submissions")
async def list_submissions(request: Request, authorization: Optional[str] = Header(default=None)):
    verify_any_admin(authorization)
    rows = await database.get_all_submissions(500)
    payload = {
        "submissions": rows,
        "is_executing": engine.is_any_executing(),
        "is_executing_ur": engine.is_model_executing("ur"),
        "is_executing_niryo": engine.is_model_executing("niryo"),
        "active_submission_id": engine.active_submission_id("ur") or engine.active_submission_id("niryo"),
        "active_submission_ur": engine.active_submission_id("ur"),
        "active_submission_niryo": engine.active_submission_id("niryo")
    }
    # ETag lets the dashboard poll revalidate cheaply instead of re-downloading all code/logs
    import hashlib, json as _json
    body = _json.dumps(payload, separators=(",", ":"), default=str)
    etag = '"' + hashlib.md5(body.encode("utf-8")).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return Response(content=body, media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/submissions/{sub_id}")
async def get_submission_details(sub_id: int, authorization: Optional[str] = Header(default=None)):
    verify_any_admin(authorization)
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    return sub


@app.get("/api/student/projects")
async def get_student_saved_projects(
    student_name: Optional[str] = None,
    authorization: Optional[str] = Header(default=None)
):
    """
    Returns all previous submissions / saved projects for the requesting student.
    Admins can view any student's projects by providing student_name.
    """
    target_student = None
    if authorization and isinstance(authorization, str):
        token = authorization.replace("Bearer ", "").strip()
        if token in active_sessions:
            session_data = active_sessions[token]
            if session_data.get("role") in ("admin", "superadmin"):
                if student_name and student_name.strip():
                    target_student = student_name.strip()
            else:
                target_student = session_data["username"]
    if not target_student and student_name:
        target_student = student_name.strip()

    if not target_student:
        raise HTTPException(status_code=400, detail="Student name or authentication token is required.")

    projects = await database.get_student_submissions(target_student)
    return {"student": target_student, "projects": projects}


@app.get("/api/admin/students/overview")
async def get_admin_students_overview(authorization: Optional[str] = Header(default=None)):
    """
    Returns overview of all registered students with their project count and last active timestamp.
    """
    verify_any_admin(authorization)
    overview = await database.get_all_students_overview()
    return {"students": overview}


@app.get("/api/admin/students/{username}/projects")
async def get_admin_student_projects(username: str, authorization: Optional[str] = Header(default=None)):
    """
    Returns all saved code / submissions for a specific student for instructor review.
    """
    verify_any_admin(authorization)
    clean_username = username.strip()
    projects = await database.get_student_submissions(clean_username)
    return {"student": clean_username, "projects": projects}


async def launch_submission_execution(
    sub_id: int,
    code_str: str,
    robot_model: Optional[str] = None,
    files: Optional[Dict[str, str]] = None,
    entry_point: Optional[str] = "main.py",
    workspace_owner: Optional[str] = None,
    workspace_project: Optional[str] = None
):
    active_model = robot_model or getattr(config, "ROBOT_MODEL", "ur")
    engine.reset_emergency(active_model)

    async def log_broadcaster(msg: str):
        # Broadcast to any student or instructor listening to this submission
        if sub_id in active_log_sockets:
            for ws in list(active_log_sockets[sub_id]):
                try:
                    await ws.send_text(msg)
                except Exception:
                    active_log_sockets[sub_id].discard(ws)

    async def persist_outputs(outputs: Dict[str, str]):
        if not (workspace_owner and workspace_project):
            return
        saved = await asyncio.to_thread(workspace.merge_files, workspace_owner, workspace_project, outputs)
        if saved:
            await broadcast_to_admins({
                "type": "workspace_files_updated",
                "owner": workspace_owner,
                "project": workspace_project,
                "files": list(saved.keys())
            })

    # Launch execution in background task
    async def run_task():
        await asyncio.to_thread(telemetry_hub.reset_stats, active_model)
        telemetry_hub.log(active_model, "info", f"Execution of submission #{sub_id} started on {active_model.upper()}", "supervisor")
        await broadcast_to_admins({"type": "execution_started", "id": sub_id, "robot_model": active_model})
        try:
            await engine.execute_submission(
                submission_id=sub_id,
                code_str=code_str,
                log_callback=log_broadcaster,
                robot_model=active_model,
                files=files,
                entry_point=entry_point or "main.py",
                on_outputs=persist_outputs if (workspace_owner and workspace_project) else None
            )
        except Exception as e:
            print(f"[Execution Task Exception] sub #{sub_id}: {e}")
            await database.update_status(sub_id, "failed", f"\n[Supervisor ERROR] Execution failed: {e}\n")
        finally:
            telemetry_hub.log(active_model, "info", f"Execution of submission #{sub_id} finished", "supervisor")
            await broadcast_to_admins({"type": "execution_finished", "id": sub_id, "robot_model": active_model})

    asyncio.create_task(run_task())


@app.post("/api/submissions/{sub_id}/approve")
async def approve_submission(sub_id: int, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_execute")
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    sub_files = sub.get("files")
    if isinstance(sub_files, str) and sub_files.strip():
        try:
            sub_files = json.loads(sub_files)
        except Exception:
            sub_files = None

    all_code = sub.get("code", "") + " " + " ".join((sub_files or {}).values())
    sub_model = sub.get("robot_model") or ("niryo" if ("pyniryo" in all_code or "NiryoRobot" in all_code) else "ur")
    model_name = "Niryo Ned" if sub_model == "niryo" else "UR3"

    if engine.is_model_executing(sub_model):
        raise HTTPException(status_code=409, detail=f"A script is already running on {model_name}.")

    target_ip = config.NIRYO_IP if sub_model == "niryo" else config.UR_IP
    is_ready, _, _ = await check_robot_reachability(target_ip, model=sub_model, use_cache=False)
    if not is_ready:
        raise HTTPException(
            status_code=503,
            detail=f"{model_name} at {target_ip} is offline (no ping). Cannot execute."
        )

    ws_project = sub.get("project_name")
    ws_owner = sub.get("student_name") if ws_project else None
    if ws_owner and ws_owner.startswith("Admin ("):
        ws_owner = None

    await launch_submission_execution(
        sub_id,
        sub["code"],
        robot_model=sub_model,
        files=sub_files,
        entry_point=sub.get("entry_point") or "main.py",
        workspace_owner=ws_owner,
        workspace_project=ws_project
    )
    return {"status": "started", "message": f"Execution launched for submission #{sub_id} on {sub_model.upper()}"}


@app.post("/api/admin/send-code")
async def admin_send_code(payload: AdminSendCodeRequest, authorization: Optional[str] = Header(default=None)):
    """
    Allows the instructor to directly send/execute custom code or multi-file project from the admin dashboard.
    """
    if payload.execute_now:
        admin_session = verify_admin_permission(authorization, "can_direct_send")
    else:
        admin_session = verify_any_admin(authorization)
    prog_name = (payload.program_name or "Admin Routine").strip()

    files = payload.files or {}
    project_modules = {os.path.splitext(f)[0] for f in files.keys() if f.endswith(".py")}

    valid, errors = validate_python_code(payload.code, project_modules=project_modules)
    if files:
        for fname, fcontent in files.items():
            if fname.endswith(".py"):
                f_valid, f_errors = validate_python_code(fcontent, project_modules=project_modules)
                if not f_valid:
                    valid = False
                    for err in f_errors:
                        errors.append(f"[{fname}] {err}")

    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": "Safety check failed on custom code.", "errors": errors}
        )

    # Detect robot model from code
    all_text = payload.code + " " + " ".join(files.values())
    detected_model = "niryo" if ("pyniryo" in all_text or "NiryoRobot" in all_text) else "ur"
    if (payload.robot_model or "").strip().lower() in ("ur", "niryo") and not ("pyniryo" in all_text or "NiryoRobot" in all_text or "rtde" in all_text):
        detected_model = payload.robot_model.strip().lower()
    target_ip = config.NIRYO_IP if detected_model == "niryo" else config.UR_IP
    ws_project = (payload.project_name or "").strip() or None

    sub_id = await database.create_submission(
        student_name=f"Admin ({prog_name})",
        task_id="Admin",
        code=payload.code,
        robot_model=detected_model,
        files=payload.files,
        entry_point=payload.entry_point or "main.py",
        project_name=ws_project
    )

    if payload.execute_now:
        model_name = "Niryo Ned" if detected_model == "niryo" else "UR3"
        is_ready, _, _ = await check_robot_reachability(target_ip, model=detected_model, use_cache=False)
        if not is_ready:
            raise HTTPException(
                status_code=503,
                detail=f"{model_name} at {target_ip} is offline. Script added to queue as #{sub_id} but not started."
            )
        if engine.is_model_executing(detected_model):
            raise HTTPException(
                status_code=409,
                detail=f"Another script is currently executing on {model_name}. Added as #{sub_id}."
            )

        await launch_submission_execution(
            sub_id,
            payload.code,
            robot_model=detected_model,
            files=payload.files,
            entry_point=payload.entry_point or "main.py",
            workspace_owner=admin_session["username"] if ws_project else None,
            workspace_project=ws_project
        )
        return {
            "status": "started",
            "submission_id": sub_id,
            "robot_model": detected_model,
            "executed": True,
            "message": f"Execution started for #{sub_id} on {detected_model.upper()}"
        }

    await broadcast_to_admins({"type": "new_submission", "id": sub_id, "robot_model": detected_model})
    return {
        "status": "pending",
        "submission_id": sub_id,
        "robot_model": detected_model,
        "executed": False,
        "message": f"Custom code saved to queue as #{sub_id}"
    }


@app.put("/api/submissions/{sub_id}")
async def update_submission_endpoint(sub_id: int, payload: UpdateSubmissionRequest, authorization: Optional[str] = Header(default=None)):
    verify_any_admin(authorization)
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    files = payload.files or {}
    project_modules = {os.path.splitext(f)[0] for f in files.keys() if f.endswith(".py")}

    if payload.code is not None:
        valid, errors = validate_python_code(payload.code, project_modules=project_modules)
        if not valid:
            return JSONResponse(
                status_code=422,
                content={"detail": "Syntax check failed on edited code.", "errors": errors}
            )

    if files:
        for fname, fcontent in files.items():
            if fname.endswith(".py"):
                f_valid, f_errors = validate_python_code(fcontent, project_modules=project_modules)
                if not f_valid:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": f"Syntax check failed in [{fname}].", "errors": f_errors}
                    )

    detected_model = payload.robot_model
    if not detected_model:
        all_text = (payload.code or "") + " " + " ".join(files.values())
        if payload.code is not None or files:
            detected_model = "niryo" if ("pyniryo" in all_text or "NiryoRobot" in all_text) else "ur"

    await database.update_submission_code(
        sub_id=sub_id,
        code=payload.code,
        program_name=payload.program_name,
        robot_model=detected_model,
        files=payload.files,
        entry_point=payload.entry_point
    )
    await broadcast_to_admins({"type": "submission_updated", "id": sub_id})
    return {"status": "updated", "id": sub_id}


@app.post("/api/submissions/{sub_id}/reject")
async def reject_submission(sub_id: int, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_execute")
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    await database.update_status(sub_id, "rejected")
    await broadcast_to_admins({"type": "submission_rejected", "id": sub_id})
    return {"status": "rejected", "id": sub_id}


@app.delete("/api/submissions/{sub_id}")
async def delete_submission_endpoint(sub_id: int, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_execute")
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    await database.delete_submission(sub_id)
    await broadcast_to_admins({"type": "submission_deleted", "id": sub_id})
    return {"status": "deleted", "id": sub_id}


@app.delete("/api/submissions")
async def clear_all_submissions_endpoint(authorization: Optional[str] = Header(default=None)):
    """Deletes all submissions from the queue database."""
    verify_admin_permission(authorization, "can_execute")
    await database.clear_all_submissions()
    await broadcast_to_admins({"type": "all_submissions_cleared"})
    return {"status": "cleared", "message": "All submissions deleted."}


@app.post("/api/abort")
async def abort_robot(model: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_emergency_stop")
    if model in ("ur", "niryo"):
        await engine.abort_model(model, "Instructor pressed Emergency Abort button.")
    else:
        await engine.abort_all("Instructor pressed Emergency Abort button.")
    await broadcast_to_admins({"type": "aborted", "robot_model": model or "all"})
    return {"status": "aborted", "message": "Emergency Stop sent to execution engine and robot."}


@app.post("/api/clear-queue")
async def clear_queue(authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_execute")
    await database.clear_pending_queue()
    await broadcast_to_admins({"type": "queue_cleared"})
    return {"status": "cleared", "message": "All pending submissions marked rejected."}


# --- Examples Endpoints ---

@app.get("/api/examples")
async def list_examples(model: Optional[str] = None):
    """
    Returns lab examples.
    If ?model=ur -> UR examples
    If ?model=niryo -> Niryo Ned examples
    If ?model=all -> all examples
    If not specified -> defaults to active config.ROBOT_MODEL
    """
    if model and model.lower() in ("ur", "niryo"):
        target_model = model.lower()
        examples = await database.get_all_examples(target_model)
        return {"examples": examples, "robot_model": target_model}
    elif model and model.lower() == "all":
        examples = await database.get_all_examples(None)
        return {"examples": examples, "robot_model": "all"}
    else:
        target_model = config.ROBOT_MODEL
        examples = await database.get_all_examples(target_model)
        return {"examples": examples, "robot_model": target_model}


@app.get("/api/examples/{example_id}")
async def get_example_endpoint(example_id: int):
    ex = await database.get_example(example_id)
    if not ex:
        raise HTTPException(status_code=404, detail="Example not found")
    return ex


@app.post("/api/examples")
async def create_example_endpoint(payload: CreateExampleRequest, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_manage_examples")
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Title is required.")
    if not payload.code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty.")

    valid, errors = validate_python_code(payload.code)
    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": "Syntax check failed on example code.", "errors": errors}
        )

    model = (payload.robot_model or "ur").strip().lower()
    if model not in ("ur", "niryo"):
        model = "ur"

    example_id = await database.create_example(
        title=payload.title,
        description=payload.description or "",
        code=payload.code,
        is_builtin=0,
        robot_model=model
    )

    await broadcast_to_admins({"type": "examples_updated", "id": example_id, "robot_model": model})
    return {"status": "created", "example_id": example_id, "title": payload.title, "robot_model": model}


@app.post("/api/examples/upload")
async def upload_example_file(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(""),
    robot_model: Optional[str] = Form("ur"),
    authorization: Optional[str] = Header(default=None)
):
    verify_admin_permission(authorization, "can_manage_examples")
    if not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only Python (.py) files are supported.")

    content = await file.read()
    code = content.decode("utf-8")

    valid, errors = validate_python_code(code)
    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": f"Syntax error in uploaded file {file.filename}.", "errors": errors}
        )

    ex_title = title.strip() if (title and title.strip()) else file.filename.replace(".py", "")
    model = (robot_model or "ur").strip().lower()
    if model not in ("ur", "niryo"):
        model = "ur"

    example_id = await database.create_example(
        title=ex_title,
        description=description or f"Uploaded file: {file.filename}",
        code=code,
        is_builtin=0,
        robot_model=model
    )

    await broadcast_to_admins({"type": "examples_updated", "id": example_id, "robot_model": model})
    return {"status": "created", "example_id": example_id, "title": ex_title, "robot_model": model}


@app.delete("/api/examples/{example_id}")
async def delete_example_endpoint(example_id: int, authorization: Optional[str] = Header(default=None)):
    verify_admin_permission(authorization, "can_manage_examples")
    ex = await database.get_example(example_id)
    if not ex:
        raise HTTPException(status_code=404, detail="Example not found")
    await database.delete_example(example_id)
    await broadcast_to_admins({"type": "examples_updated", "id": example_id})
    return {"status": "deleted", "id": example_id}


# --- Live Robot Telemetry & Debug (read-only) ---

class DebugCommandRequest(BaseModel):
    command: str


def _engine_state(model: str) -> Dict[str, Any]:
    return {
        "executing": engine.is_model_executing(model),
        "active_submission_id": engine.active_submission_id(model),
        "emergency_halted": engine.is_emergency_halted(model),
    }


@app.get("/api/telemetry/{model}")
async def get_telemetry_snapshot(model: str, authorization: Optional[str] = Header(default=None)):
    verify_authenticated_user(authorization)
    snap = await telemetry_hub.get(model)
    return {**snap, "engine": _engine_state(telemetry_hub.normalize(model))}


@app.get("/api/telemetry/{model}/history")
async def get_telemetry_history(model: str, limit: int = 600, authorization: Optional[str] = Header(default=None)):
    verify_authenticated_user(authorization)
    m = telemetry_hub.normalize(model)
    return {
        "model": m,
        "history": telemetry_hub.history(m, max(10, min(limit, 1200))),
        "debug": telemetry_hub.debug_since(m, 0),
        "debug_seq": telemetry_hub.debug_seq(m),
    }


@app.post("/api/telemetry/{model}/reset-stats")
async def reset_telemetry_stats(model: str, authorization: Optional[str] = Header(default=None)):
    verify_any_admin(authorization)
    await asyncio.to_thread(telemetry_hub.reset_stats, model)
    return {"status": "ok"}


@app.post("/api/robot-debug/{model}/command")
@app.post("/api/telemetry/{model}/dashboard")
async def robot_debug_command(model: str, payload: DebugCommandRequest, authorization: Optional[str] = Header(default=None)):
    """Read-only controller queries (UR dashboard allow-list). Never moves the robot."""
    verify_admin_permission(authorization, "can_execute")
    try:
        out = await telemetry_hub.dashboard(model, payload.command)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller did not answer: {e}")
    return {"command": payload.command, "response": out}


# --- WebSockets ---

@app.websocket("/ws/telemetry/{model}")
async def ws_telemetry(websocket: WebSocket, model: str, token: str = ""):
    """Streams ~5 Hz telemetry frames (plus new debug lines) to an authenticated user (student or admin)."""
    session = active_sessions.get((token or "").strip())
    if not session:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    m = telemetry_hub.normalize(model)
    last_seq = telemetry_hub.debug_seq(m)
    try:
        while True:
            snap = await telemetry_hub.get(m)
            new_debug = telemetry_hub.debug_since(m, last_seq)
            if new_debug:
                last_seq = new_debug[-1]["seq"]
            await websocket.send_json({**snap, "engine": _engine_state(m), "debug": new_debug})
            await asyncio.sleep(0.2 if snap.get("connected") else 1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/logs/{sub_id}")
async def ws_logs(websocket: WebSocket, sub_id: int):
    await websocket.accept()
    if sub_id not in active_log_sockets:
        active_log_sockets[sub_id] = set()
    active_log_sockets[sub_id].add(websocket)

    # Send existing logs if any
    sub = await database.get_submission(sub_id)
    if sub and sub.get("logs"):
        await websocket.send_text(sub["logs"])

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if sub_id in active_log_sockets:
            active_log_sockets[sub_id].discard(websocket)
            if not active_log_sockets[sub_id]:
                del active_log_sockets[sub_id]


@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await websocket.accept()
    admin_sockets.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        admin_sockets.discard(websocket)
    except Exception:
        admin_sockets.discard(websocket)


async def broadcast_to_admins(data: dict):
    for ws in list(admin_sockets):
        try:
            await ws.send_json(data)
        except Exception:
            admin_sockets.discard(ws)


if __name__ == "__main__":
    import uvicorn
    # Bind to 0.0.0.0 so students on the local Wi-Fi / LAN can connect
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
