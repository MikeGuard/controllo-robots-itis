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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database
import config
from execution_engine import engine
from safety import validate_python_code
from simulator import simulate_script


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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Connected WebSockets for log broadcasting
active_log_sockets: Dict[int, Set[WebSocket]] = {}
admin_sockets: Set[WebSocket] = set()


# --- Pydantic Request Models ---

class SyntaxCheckRequest(BaseModel):
    code: str


class SubmissionRequest(BaseModel):
    program_name: Optional[str] = None
    student_name: Optional[str] = None
    task_id: Optional[str] = ""
    code: str


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


class UpdateSubmissionRequest(BaseModel):
    code: Optional[str] = None
    program_name: Optional[str] = None


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


# --- Web Page Views ---

@app.get("/")
async def get_student_portal():
    return FileResponse(os.path.join(STATIC_DIR, "student.html"))


@app.get("/admin")
async def get_instructor_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "instructor.html"))


# --- Helper Functions ---

async def check_robot_reachability(ip: Optional[str] = None):
    target_ip = ip or config.ROBOT_IP
    ping_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", target_ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        ping_ok = (proc.returncode == 0)
    except Exception:
        ping_ok = False

    # Also check UR ports (30004 RTDE, 30002 secondary)
    port_ok = False
    if not ping_ok:
        for port in (config.ROBOT_RTDE_PORT, config.ROBOT_SECONDARY_PORT):
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(target_ip, port), timeout=0.5)
                writer.close()
                await writer.wait_closed()
                port_ok = True
                break
            except Exception:
                continue

    is_ready = ping_ok or port_ok
    return is_ready, ping_ok, port_ok


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


@app.post("/api/auth/login")
async def login_user(payload: LoginRequest):
    """Authenticates a student or admin user."""
    username = payload.username.strip()
    password = payload.password.strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    user = await database.get_user_by_username(username)
    if not user or not database.verify_password(password, user["password_hash"]):
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

    # Generate session token
    token = secrets.token_hex(24)
    active_sessions[token] = {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"]
    }

    return {
        "status": "success",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"]
        }
    }


@app.get("/api/auth/me")
async def get_current_user(authorization: Optional[str] = Header(default=None)):
    """Validates session token and returns active user info."""
    if not authorization or not isinstance(authorization, str):
        return {"authenticated": False}

    token = authorization.replace("Bearer ", "").strip()
    if token in active_sessions:
        return {"authenticated": True, "user": active_sessions[token]}
    return {"authenticated": False}


@app.post("/api/auth/logout")
async def logout_user(authorization: Optional[str] = Header(default=None)):
    """Clears active session token."""
    if authorization and isinstance(authorization, str):
        token = authorization.replace("Bearer ", "").strip()
        active_sessions.pop(token, None)
    return {"status": "logged_out"}


# --- Admin User Management Endpoints ---

@app.get("/api/admin/users")
async def get_all_users_admin():
    """Lists all users (pending, approved, rejected) for the instructor."""
    users = await database.get_all_users()
    return {"users": users}


@app.post("/api/admin/users/{user_id}/status")
async def update_user_status_endpoint(user_id: int, payload: UserStatusUpdateRequest):
    """Allows instructor to approve or reject a student account."""
    status = payload.status.lower().strip()
    if status not in ("approved", "rejected", "pending"):
        raise HTTPException(status_code=400, detail="Status must be 'approved', 'rejected', or 'pending'.")

    success = await database.update_user_status(user_id, status)
    if not success:
        raise HTTPException(status_code=404, detail="User not found.")

    await broadcast_to_admins({"type": "user_status_changed", "id": user_id, "status": status})
    return {"status": "updated", "id": user_id, "new_status": status}


@app.delete("/api/admin/users/{user_id}")
async def delete_user_endpoint(user_id: int):
    """Allows instructor to remove a user account."""
    success = await database.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot delete admin user or user not found.")

    await broadcast_to_admins({"type": "user_deleted", "id": user_id})
    return {"status": "deleted", "id": user_id}


# --- Robot Configuration & Status ---

@app.post("/api/config")
async def update_config(payload: ConfigUpdateRequest):
    """Allows the instructor to change the robot model/IP from the admin dashboard."""
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
async def get_robot_status(ip: Optional[str] = None):
    """
    Pings the active robot IP and checks connectivity.
    If ping or port probe fails, robot is NOT ready.
    """
    target_ip = ip or config.ROBOT_IP
    is_ready, ping_ok, port_ok = await check_robot_reachability(target_ip)
    return {
        "ip": target_ip,
        "robot_model": config.ROBOT_MODEL,
        "is_ready": is_ready,
        "ping_ok": ping_ok,
        "port_ok": port_ok,
        "status_text": "READY" if is_ready else "NOT READY",
        "message": f"Robot at {target_ip} is online and ready." if is_ready else f"Robot at {target_ip} did not respond to ping. Not ready."
    }


# --- Code Verification & Simulation ---

@app.post("/api/check-syntax")
async def check_syntax(payload: SyntaxCheckRequest):
    valid, errors = validate_python_code(payload.code)
    return {"valid": valid, "errors": errors}


@app.post("/api/simulate")
async def simulate_code(payload: SyntaxCheckRequest):
    """
    Simulates code execution in a mock environment and returns trajectory waypoints
    for smooth 3D browser visualization.
    """
    valid, errors = validate_python_code(payload.code)
    if not valid:
        return JSONResponse(
            status_code=422,
            content={"success": False, "detail": "Safety check failed.", "errors": errors}
        )

    res = simulate_script(payload.code)
    return res


# --- Submissions Management & Execution ---

@app.post("/api/submit")
async def submit_code(payload: SubmissionRequest):
    """
    Submits student code for execution.
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

    valid, errors = validate_python_code(payload.code)
    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": "Safety or syntax check failed.", "errors": errors}
        )

    sub_id = await database.create_submission(
        student_name=student_name,
        task_id=program_name,
        code=payload.code
    )

    # Notify instructor dashboard of new submission
    await broadcast_to_admins({"type": "new_submission", "id": sub_id, "student": student_name, "program": program_name})
    return {
        "submission_id": sub_id,
        "status": "pending",
        "student_name": student_name,
        "program_name": program_name,
        "message": f"Program '{program_name}' sent for execution."
    }


@app.get("/api/submissions")
async def list_submissions():
    rows = await database.get_all_submissions()
    return {
        "submissions": rows,
        "is_executing": engine.is_executing,
        "active_submission_id": engine.current_submission_id
    }


@app.get("/api/submissions/{sub_id}")
async def get_submission_details(sub_id: int):
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
    """
    target_student = None
    if authorization and isinstance(authorization, str):
        token = authorization.replace("Bearer ", "").strip()
        if token in active_sessions:
            target_student = active_sessions[token]["username"]
    if not target_student and student_name:
        target_student = student_name.strip()

    if not target_student:
        raise HTTPException(status_code=400, detail="Student name or authentication token is required.")

    projects = await database.get_student_submissions(target_student)
    return {"student": target_student, "projects": projects}


async def launch_submission_execution(sub_id: int, code_str: str):
    async def log_broadcaster(msg: str):
        # Broadcast to any student or instructor listening to this submission
        if sub_id in active_log_sockets:
            for ws in list(active_log_sockets[sub_id]):
                try:
                    await ws.send_text(msg)
                except Exception:
                    active_log_sockets[sub_id].discard(ws)

    # Launch execution in background task
    async def run_task():
        await broadcast_to_admins({"type": "execution_started", "id": sub_id})
        try:
            await engine.execute_submission(
                submission_id=sub_id,
                code_str=code_str,
                log_callback=log_broadcaster
            )
        except Exception as e:
            print(f"[Execution Task Exception] sub #{sub_id}: {e}")
            await database.update_status(sub_id, "failed", f"\n[Supervisor ERROR] Execution failed: {e}\n")
        finally:
            await broadcast_to_admins({"type": "execution_finished", "id": sub_id})

    asyncio.create_task(run_task())


@app.post("/api/submissions/{sub_id}/approve")
async def approve_submission(sub_id: int):
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    if engine.is_executing:
        raise HTTPException(status_code=409, detail="A script is already running on the robot.")

    is_ready, _, _ = await check_robot_reachability()
    if not is_ready:
        raise HTTPException(
            status_code=503,
            detail=f"Robot at {config.ROBOT_IP} is offline (no ping). Cannot execute."
        )

    await launch_submission_execution(sub_id, sub["code"])
    return {"status": "started", "message": f"Execution launched for submission #{sub_id}"}


@app.post("/api/admin/send-code")
async def admin_send_code(payload: AdminSendCodeRequest):
    """
    Allows the instructor to directly send/execute custom code from the admin dashboard.
    """
    prog_name = (payload.program_name or "Admin Routine").strip()
    valid, errors = validate_python_code(payload.code)
    if not valid:
        return JSONResponse(
            status_code=422,
            content={"detail": "Safety check failed on custom code.", "errors": errors}
        )

    sub_id = await database.create_submission(
        student_name=f"Admin ({prog_name})",
        task_id="Admin",
        code=payload.code
    )

    if payload.execute_now:
        is_ready, _, _ = await check_robot_reachability()
        if not is_ready:
            raise HTTPException(
                status_code=503,
                detail=f"Robot at {config.ROBOT_IP} is offline. Script added to queue as #{sub_id} but not started."
            )
        if engine.is_executing:
            raise HTTPException(
                status_code=409,
                detail=f"Another script is currently executing. Added as #{sub_id}."
            )
        await launch_submission_execution(sub_id, payload.code)
        return {
            "status": "started",
            "submission_id": sub_id,
            "message": f"Execution started for #{sub_id}"
        }

    await broadcast_to_admins({"type": "new_submission", "id": sub_id})
    return {
        "status": "pending",
        "submission_id": sub_id,
        "message": f"Custom code saved to queue as #{sub_id}"
    }


@app.put("/api/submissions/{sub_id}")
async def update_submission_endpoint(sub_id: int, payload: UpdateSubmissionRequest):
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    if payload.code is not None:
        valid, errors = validate_python_code(payload.code)
        if not valid:
            return JSONResponse(
                status_code=422,
                content={"detail": "Syntax check failed on edited code.", "errors": errors}
            )

    await database.update_submission_code(
        sub_id=sub_id,
        code=payload.code,
        student_name=payload.program_name
    )
    await broadcast_to_admins({"type": "submission_updated", "id": sub_id})
    return {"status": "updated", "id": sub_id}


@app.post("/api/submissions/{sub_id}/reject")
async def reject_submission(sub_id: int):
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    await database.update_status(sub_id, "rejected")
    await broadcast_to_admins({"type": "submission_rejected", "id": sub_id})
    return {"status": "rejected", "id": sub_id}


@app.delete("/api/submissions/{sub_id}")
async def delete_submission_endpoint(sub_id: int):
    sub = await database.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    await database.delete_submission(sub_id)
    await broadcast_to_admins({"type": "submission_deleted", "id": sub_id})
    return {"status": "deleted", "id": sub_id}


@app.delete("/api/submissions")
async def clear_all_submissions_endpoint():
    """Deletes all submissions from the queue database."""
    await database.clear_all_submissions()
    await broadcast_to_admins({"type": "all_submissions_cleared"})
    return {"status": "cleared", "message": "All submissions deleted."}


@app.post("/api/abort")
async def abort_robot():
    await engine.abort_current("Instructor pressed Emergency Abort button.")
    await broadcast_to_admins({"type": "aborted"})
    return {"status": "aborted", "message": "Emergency Stop sent to execution engine and robot."}


@app.post("/api/clear-queue")
async def clear_queue():
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
async def create_example_endpoint(payload: CreateExampleRequest):
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
    robot_model: Optional[str] = Form("ur")
):
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
async def delete_example_endpoint(example_id: int):
    ex = await database.get_example(example_id)
    if not ex:
        raise HTTPException(status_code=404, detail="Example not found")
    await database.delete_example(example_id)
    await broadcast_to_admins({"type": "examples_updated", "id": example_id})
    return {"status": "deleted", "id": example_id}


# --- WebSockets ---

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
        admin_sockets.remove(websocket)


async def broadcast_to_admins(data: dict):
    for ws in list(admin_sockets):
        try:
            await ws.send_json(data)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    # Bind to 0.0.0.0 so students on the local Wi-Fi / LAN can connect
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
