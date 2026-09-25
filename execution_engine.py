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


class ExecutionEngine:
    def __init__(self):
        self.current_process: Optional[asyncio.subprocess.Process] = None
        self.current_submission_id: Optional[int] = None
        self.is_executing = False
        self._lock = asyncio.Lock()

    async def execute_submission(
        self,
        submission_id: int,
        code_str: str,
        log_callback: Optional[Callable[[str], None]] = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC
    ) -> bool:
        """
        Executes a student Python script in an isolated subprocess.
        Returns True if completed successfully, False otherwise.
        """
        async with self._lock:
            if self.is_executing:
                raise RuntimeError("Another script is currently executing on the robot.")
            self.is_executing = True
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
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
                preexec_fn=os.setsid  # Put in new process group for clean abort/kill
            )
            self.current_process = proc

            async def dispatch_log(text: str):
                if log_callback:
                    res = log_callback(text)
                    if asyncio.iscoroutine(res):
                        await res

            async def stream_output():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    await append_logs(submission_id, decoded)
                    await dispatch_log(decoded)

            # Run with watchdog timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stream_output()),
                    timeout=timeout_sec
                )
                if proc.returncode == 0:
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
            self.current_process = None
            self.current_submission_id = None
            self.is_executing = False

        return success

    async def abort_current(self, reason: str = "Instructor Emergency Stop"):
        """Emergency Stop: Kills student process group and commands the active robot to halt."""
        print(f"[EMERGENCY STOP] Triggered: {reason}")

        # 1. Kill the subprocess group immediately
        if self.current_process and self.current_process.pid:
            try:
                pgid = os.getpgid(self.current_process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[EMERGENCY STOP] Error killing process group: {e}")

        # 2. Hardware Emergency Halt — depends on active robot model
        robot_model = getattr(config, "ROBOT_MODEL", "ur")
        active_ip = config.ROBOT_IP

        if robot_model == "niryo":
            # Niryo One: send stop_move via pyniryo
            try:
                import pyniryo
                niryo = pyniryo.NiryoRobot(active_ip)
                niryo.stop_move()
                niryo.end()
                print("[EMERGENCY STOP] Sent stop_move() to Niryo One.")
            except ImportError:
                print("[EMERGENCY STOP] pyniryo not installed — cannot send hardware stop to Niryo One.")
            except Exception as e:
                print(f"[EMERGENCY STOP] Niryo halt attempt failed: {e}")
        else:
            # UR: send stopj via Secondary Socket
            robot_port = config.ROBOT_SECONDARY_PORT
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect((active_ip, robot_port))
                # stopj with emergency deceleration (5.0 rad/s^2)
                s.sendall(b"stopj(5.0)\n")
                s.close()
                print("[EMERGENCY STOP] Sent stopj(5.0) to UR controller.")
            except Exception as e:
                print(f"[EMERGENCY STOP] Secondary socket UR halt attempt: {e}")

        if self.current_submission_id:
            await append_logs(
                self.current_submission_id,
                f"\n[EMERGENCY STOP] Execution aborted immediately: {reason}\n"
            )
            await update_status(self.current_submission_id, "aborted")



# Singleton engine instance
engine = ExecutionEngine()
