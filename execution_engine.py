"""
Execution Engine: Subprocess supervisor, timeout watchdog, emergency abort,
and live output streaming.
"""

import asyncio
import os
import signal
import socket
import sys
import tempfile
from typing import Callable, Optional

# Ensure ur_platform directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import update_status, append_logs
import config

DEFAULT_TIMEOUT_SEC = config.EXECUTION_TIMEOUT_SEC
ROBOT_IP = config.ROBOT_IP
ROBOT_PORT = config.ROBOT_SECONDARY_PORT


def _send_ur_emergency_halt(ip: str, port: int):
    """Sends stopj(5.0) and resets digital outs via secondary socket with short timeout."""
    if not ip:
        return
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect((ip, port))
        s.sendall(b"stopj(5.0)\n")
        s.sendall(b"set_standard_digital_out(0, False)\n")
        s.sendall(b"set_standard_digital_out(1, False)\n")
        s.sendall(b"set_tool_digital_out(0, False)\n")
        s.sendall(b"set_tool_digital_out(1, False)\n")
        s.close()
        print(f"[EMERGENCY STOP] Sent stopj(5.0) and reset digital outputs to UR at {ip}:{port}")
    except Exception as e:
        print(f"[EMERGENCY STOP] UR halt notice ({ip}): {e}")


def _send_niryo_emergency_halt(ip: str):
    """Sends stop_move to Niryo arm if reachable, with short probe to avoid long blocking."""
    if not ip:
        return
    try:
        # Pre-probe socket with 0.3s timeout so pyniryo does not freeze for 20s
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect((ip, getattr(config, "NIRYO_PORT", 9090)))
        s.close()

        import pyniryo
        niryo = pyniryo.NiryoRobot(ip)
        niryo.stop_move()
        niryo.end()
        print(f"[EMERGENCY STOP] Sent stop_move() to Niryo at {ip}")
    except Exception as e:
        print(f"[EMERGENCY STOP] Niryo halt notice ({ip}): {e}")


class ExecutionEngine:
    def __init__(self):
        self.current_process: Optional[asyncio.subprocess.Process] = None
        self.current_submission_id: Optional[int] = None
        self.is_executing = False
        self.is_aborted = False
        self.is_emergency_halted = False
        self.emergency_halt_reason = ""
        self._lock = asyncio.Lock()

    def reset_emergency(self):
        self.is_emergency_halted = False
        self.emergency_halt_reason = ""

    async def execute_submission(
        self,
        submission_id: int,
        code_str: str,
        log_callback: Optional[Callable[[str], None]] = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        robot_model: Optional[str] = None
    ) -> bool:
        """
        Executes a student Python script in an isolated subprocess.
        Returns True if completed successfully, False otherwise.
        """
        if robot_model in ("ur", "niryo"):
            config.ROBOT_MODEL = robot_model
            config.ROBOT_IP = config.NIRYO_IP if robot_model == "niryo" else config.UR_IP
        self.current_robot_model = getattr(config, "ROBOT_MODEL", "ur")

        async with self._lock:
            if self.is_executing:
                raise RuntimeError("Another script is currently executing on the robot.")
            self.is_executing = True
            self.is_aborted = False
            self.current_submission_id = submission_id

        await update_status(submission_id, "running")

        # Create temporary script file in ur_platform dir so local imports (ur_wrapper) resolve
        work_dir = os.path.dirname(os.path.abspath(__file__))
        runtime_prelude = (
            "import builtins\n"
            "def printf(*args, **kwargs):\n"
            "    kwargs.setdefault('flush', True)\n"
            "    if len(args) > 1 and isinstance(args[0], str) and ('%' in args[0]):\n"
            "        try:\n"
            "            print(args[0] % args[1:], **kwargs)\n"
            "            return\n"
            "        except Exception:\n"
            "            pass\n"
            "    print(*args, **kwargs)\n"
            "builtins.printf = printf\n"
            "# --- End Prelude ---\n"
        )
        full_code = runtime_prelude + code_str
        with tempfile.NamedTemporaryFile("w", suffix=".py", dir=work_dir, delete=False) as f:
            temp_path = f.name
            f.write(full_code)

        success = False
        try:
            # Use current Python interpreter (from virtualenv)
            # unbuffered (-u) to guarantee real-time log output
            extra_kwargs = {}
            if sys.platform != "win32":
                extra_kwargs["preexec_fn"] = os.setsid  # Put in new process group on POSIX
            else:
                import subprocess
                extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
                **extra_kwargs
            )
            self.current_process = proc

            async def dispatch_log(text: str):
                if log_callback:
                    res = log_callback(text)
                    if asyncio.iscoroutine(res):
                        await res

            async def stream_output():
                while True:
                    try:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace")
                        await append_logs(submission_id, decoded)
                        await dispatch_log(decoded)
                    except Exception:
                        break

            # Run with watchdog timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stream_output()),
                    timeout=timeout_sec
                )

                if self.is_aborted:
                    msg = f"\n[Supervisor] Script was aborted ({self.emergency_halt_reason or 'Emergency Stop'}).\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "aborted")
                elif proc.returncode == 0:
                    success = True
                    msg = "\n[Supervisor] Execution completed successfully with returncode 0.\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "completed")
                else:
                    msg = f"\n[Supervisor] Script terminated with error code {proc.returncode}.\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "failed")

            except asyncio.TimeoutError:
                msg = f"\n[Supervisor WATCHDOG] Execution timed out (> {timeout_sec}s). Force killing process.\n"
                await append_logs(submission_id, msg)
                await dispatch_log(msg)
                await self.abort_current(reason="Timeout")
                await update_status(submission_id, "failed")

        except Exception as e:
            msg = f"\n[Supervisor ERROR] Execution failed to launch: {e}\n"
            await append_logs(submission_id, msg)
            if log_callback:
                res = log_callback(msg)
                if asyncio.iscoroutine(res):
                    await res
            await update_status(submission_id, "failed")

        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if not success and getattr(self, "current_robot_model", "ur") == "ur" and getattr(config, "UR_IP", None):
                # Non-blocking background reset
                asyncio.create_task(asyncio.to_thread(
                    _send_ur_emergency_halt, config.UR_IP, config.ROBOT_SECONDARY_PORT
                ))
            self.current_process = None
            self.current_submission_id = None
            self.is_executing = False

        return success

    async def abort_current(self, reason: str = "Instructor Emergency Stop"):
        """Emergency Stop: Kills student process group immediately and commands active robot to halt without blocking."""
        print(f"[EMERGENCY STOP] Triggered: {reason}")
        self.is_aborted = True
        self.is_emergency_halted = True
        self.emergency_halt_reason = reason

        # 1. Kill the subprocess group immediately
        proc = self.current_process
        if proc and proc.pid:
            try:
                if sys.platform == "win32":
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        import subprocess
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False
                        )
                    except Exception:
                        pass
                else:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[EMERGENCY STOP] Error killing process group: {e}")

        # 2. Hardware Emergency Halt — dispatched in worker threads (zero blocking on event loop)
        active_model = getattr(self, "current_robot_model", None) or getattr(config, "ROBOT_MODEL", "ur")
        if active_model == "ur" and getattr(config, "UR_IP", None):
            asyncio.create_task(asyncio.to_thread(
                _send_ur_emergency_halt, config.UR_IP, getattr(config, "ROBOT_SECONDARY_PORT", 30002)
            ))
        elif active_model == "niryo" and getattr(config, "NIRYO_IP", None):
            asyncio.create_task(asyncio.to_thread(
                _send_niryo_emergency_halt, config.NIRYO_IP
            ))

        sub_id = self.current_submission_id
        if sub_id:
            await append_logs(
                sub_id,
                f"\n[EMERGENCY STOP] Execution aborted immediately: {reason}\n"
            )
            await update_status(sub_id, "aborted")


# Singleton engine instance
engine = ExecutionEngine()

