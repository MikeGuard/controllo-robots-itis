# Robotics Execution Platform - Technical Architecture & Operation Manual

## 1. System Overview

The **Robotics Execution Platform** is a dual-arm educational and laboratory automation system designed for **Universal Robots UR3** (via `ur_rtde`) and **Niryo Ned / One** (via `pyniryo`). It provides an end-to-end workflow:
- **Students** write, simulate in 3D, validate, and submit robot programs through a web-based sandbox.
- **Instructors & Robot Managers** inspect queued submissions, simulate trajectories, dispatch physical execution, monitor real-time WebSocket telemetry, and control hardware safety.

---

## 2. Core Architectural Decoupling & Stability

### 2.1 Process-Isolated Simulation Engine (`simulator.py`)
To prevent the application from freezing or locking up when users trigger 3D simulations:
- **Subprocess Isolation**: Simulations run inside an independent external Python subprocess via `run_simulation_isolated()` with an 8-second hard timeout.
- **GIL & Memory Protection**: Trajectory calculation does not block the FastAPI asyncio event loop or hold Python's Global Interpreter Lock (GIL).
- **Socket & Hardware Shield**: In isolated simulation mode, network calls to hardware and real sockets are prohibited, preventing port pollution or interference with physical robot connections.

### 2.2 Non-Blocking Emergency Stop Watchdog (`execution_engine.py`)
- **Subprocess Abort**: When an Emergency Stop is triggered, running worker processes are terminated immediately via `SIGTERM` / `SIGKILL` without waiting.
- **Asynchronous Hardware Halt**: Hardware stop commands (`rtde_c.stopScript()`, `rtde_c.stopL()`, `robot.abort_action()`) are dispatched asynchronously via `asyncio.to_thread` with strict short timeouts (0.4s).
- **Targeted Robot Abort**: The system aborts only the active robot model (UR3 or Niryo), avoiding lengthy socket connection timeouts attempting to reach disconnected or unconfigured secondary arms.
- **Lockout Tracking**: When halted, `is_emergency_halted` prevents any new execution requests until an authorized administrator explicitly resets the emergency state via `/api/emergency-reset`.

### 2.3 Proactive Hardware Safety Guard (`ur_wrapper.py`)
- In `ur_wrapper.py`, every motion command (`movej`, `movel`) checks `check_safety_state()` before and during trajectory execution.
- If the physical robot enters Emergency Stop, Protective Stop, or Safeguard Stop, `ur_wrapper` immediately raises `SafetyViolationError` instead of allowing `ur_rtde` to block indefinitely.

---

## 3. Authentication & Access Management

### 3.1 Portal Routes
- **Student Sandbox**: `http://<server-ip>:8000/`
- **Robot Manager / Instructor Console**: `http://<server-ip>:8000/admin`
- On the standard student login page, users can click **"🛠️ Access as Robot Manager"** to switch directly to the Manager login view and access the admin console.

### 3.2 Superadmin Default Credentials
- **Username**: `mike`
- **Default Password**: `mike2088`
- **Role**: `superadmin`
- **Authority**: Permanent full system authority (`permissions: ["*"]`). Protected against deletion or accidental privilege revocation.
- *Note:* Login screens display clean placeholders without revealing credentials on screen for privacy and security.

---

## 4. Role-Based Access Control (RBAC) & Admin Delegation

From the **Robot Manager Console** (`/admin`), when logged in as **Superadmin (`mike`)**, navigate to:
> **👥 Users & Admins** &rarr; **🛡️ Admin Accounts & Permissions**

### 4.1 Granular Permissions Matrix
Superadmins can create delegated admin accounts and customize what each administrator is permitted to perform:

| Permission Key | Privilege Name | Description |
| :--- | :--- | :--- |
| `can_execute` | **Execute Submissions** | Approve and launch queued student programs on the physical robot arm. |
| `can_emergency_stop` | **Emergency Halt & Reset** | Trigger hardware emergency stop and reset emergency software lockouts. |
| `can_edit_config` | **Robot Configuration** | Switch active robot model (UR3 / Niryo) and update robot network IP addresses. |
| `can_manage_examples` | **Manage Examples** | Upload, edit, and delete shared code examples and templates for students. |
| `can_manage_students` | **Manage Students** | Approve pending student account registrations, reject, or revoke student access. |
| `can_direct_send` | **Direct Code Dispatch** | Directly execute custom Python code from the Code Studio without student queuing. |

### 4.2 Quick Assignment Presets
When creating an administrator account, superadmins can use quick presets:
- **All Permissions**: Grants all 6 privileges for co-instructors.
- **Execution Only**: Grants `can_execute` and `can_emergency_stop` for lab technicians.
- **Safety Officer**: Grants `can_emergency_stop` for safety observers.
- **Teaching Assistant**: Grants `can_manage_examples` and `can_manage_students`.

### 4.3 Live Permission Toggling
Superadmins can update any delegated administrator's privileges in real time directly from the admin table by checking or unchecking permission checkboxes. Privileges sync immediately with active sessions.

---

## 5. Quickstart Guide

### 5.1 Starting the Application (macOS & Linux)
```bash
# Using the startup script (auto-detects virtual environment and local IP)
sh run.sh

# With hot-reloading for development
sh run.sh --reload
```

### 5.2 Starting on Windows
```cmd
run.bat
```
or via PowerShell:
```powershell
.\run.ps1
```

### 5.3 Raspberry Pi Deployment
```bash
chmod +x setup_rpi.sh
./setup_rpi.sh
```

---

## 6. Key API Endpoints

### Authentication & Authorization
- `POST /api/auth/login`: Authenticates students, administrators, and superadmin (`mike`).
- `POST /api/auth/register`: Submits a new student account for instructor approval.
- `GET /api/auth/me`: Returns current user identity, role, and permission array.
- `POST /api/auth/logout`: Invalidates the caller's session token.
- `GET /api/auth/students`: Lists approved student usernames for the login dropdown.

### Superadmin Management
- `GET /api/admin/admins`: Lists all administrators and their delegated permissions (`superadmin` required).
- `POST /api/admin/create-admin`: Creates a new administrator with specific permissions (`superadmin` required).
- `PUT /api/admin/users/{user_id}/permissions`: Updates granular privileges for an administrator (`superadmin` required).
- `DELETE /api/admin/users/{user_id}`: Revokes an admin or student account (superadmin `mike` and default `admin` are protected).

### Safety & Hardware Execution
- `POST /api/submissions/{sub_id}/approve`: Validates `can_execute` and dispatches program to the active robot arm.
- `POST /api/abort`: Triggers immediate emergency halt on the active robot arm.
- `POST /api/emergency-reset`: Clears the emergency stop software lockout (`can_emergency_stop` required).
- `POST /api/config`: Updates active robot model and hardware IP addresses (`can_edit_config` required).
- `POST /api/simulate`: Runs process-isolated 3D simulation with trajectory collision detection.
- `POST /api/check-syntax`: Static code inspection verifying safety boundaries and disallowed libraries.
