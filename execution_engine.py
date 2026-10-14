"""
Execution Engine: Multi-robot concurrent subprocess supervisor, watchdog,
emergency abort, and live output streaming for UR3 and Niryo Ned.
"""

import asyncio
import os
import signal
import socket
import sys
import tempfile
import shutil
from typing import Callable, Optional, Dict, Any

# Ensure ur_platform directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import update_status, append_logs
import config
import sandbox_io

DEFAULT_TIMEOUT_SEC = getattr(config, "EXECUTION_TIMEOUT_SEC", 45.0)


def _send_ur_emergency_halt(ip: str, port: int = 30002):
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


class RobotSlot:
    """Tracks state and process for an individual robot arm."""
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.process: Optional[asyncio.subprocess.Process] = None
        self.submission_id: Optional[int] = None
        self.is_executing: bool = False
        self.is_aborted: bool = False
        self.is_emergency_halted: bool = False
        self.emergency_halt_reason: str = ""
        self.lock = asyncio.Lock()


class ExecutionEngine:
    """
    Manages concurrent, independent execution across multiple robots (UR3 and Niryo Ned).
    Scripts for UR3 and Niryo can execute simultaneously without blocking each other.
    """
    def __init__(self):
        self.slots: Dict[str, RobotSlot] = {
            "ur": RobotSlot("ur"),
            "niryo": RobotSlot("niryo")
        }

    def _normalize_model(self, robot_model: Optional[str]) -> str:
        if robot_model and "niryo" in robot_model.lower():
            return "niryo"
        return "ur"

    def get_slot(self, robot_model: Optional[str]) -> RobotSlot:
        model = self._normalize_model(robot_model)
        if model not in self.slots:
            self.slots[model] = RobotSlot(model)
        return self.slots[model]

    # --- Status Inspection Helpers ---

    def is_model_executing(self, robot_model: str) -> bool:
        slot = self.get_slot(robot_model)
        return slot.is_executing

    def is_any_executing(self) -> bool:
        return any(s.is_executing for s in self.slots.values())

    def active_submission_id(self, robot_model: Optional[str] = None) -> Optional[int]:
        if robot_model:
            return self.get_slot(robot_model).submission_id
        for s in self.slots.values():
            if s.submission_id:
                return s.submission_id
        return None

    def is_emergency_halted(self, robot_model: Optional[str] = None) -> bool:
        if robot_model:
            return self.get_slot(robot_model).is_emergency_halted
        return any(s.is_emergency_halted for s in self.slots.values())

    @property
    def emergency_halt_reason(self) -> str:
        for s in self.slots.values():
            if s.emergency_halt_reason:
                return s.emergency_halt_reason
        return ""

    @property
    def is_executing(self) -> bool:
        return self.is_any_executing()

    @property
    def current_process(self) -> Optional[asyncio.subprocess.Process]:
        for s in self.slots.values():
            if s.process:
                return s.process
        return None

    @property
    def current_submission_id(self) -> Optional[int]:
        return self.active_submission_id()

    def reset_emergency(self, robot_model: Optional[str] = None):
        if robot_model:
            slot = self.get_slot(robot_model)
            slot.is_emergency_halted = False
            slot.emergency_halt_reason = ""
        else:
            for slot in self.slots.values():
                slot.is_emergency_halted = False
                slot.emergency_halt_reason = ""

    # --- Execution Logic ---

    async def execute_submission(
        self,
        submission_id: int,
        code_str: str,
        log_callback: Optional[Callable[[str], None]] = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        robot_model: Optional[str] = None,
        files: Optional[Dict[str, str]] = None,
        entry_point: Optional[str] = "main.py",
        on_outputs: Optional[Callable[[Dict[str, str]], Any]] = None
    ) -> bool:
        """
        Executes a Python script or multi-file project on the designated robot arm in an isolated subprocess.
        Does not block other robot arms from executing concurrently.
        """
        model = self._normalize_model(robot_model)
        slot = self.get_slot(model)

        async with slot.lock:
            if slot.is_executing:
                arm_label = "Niryo Ned" if model == "niryo" else "UR3 Arm"
                raise RuntimeError(f"Another script is currently executing on {arm_label}.")
            slot.is_executing = True
            slot.is_aborted = False
            slot.submission_id = submission_id

        await update_status(submission_id, "running")

        work_dir = os.path.dirname(os.path.abspath(__file__))
        target_ip = getattr(config, "NIRYO_IP", "10.0.10.61") if model == "niryo" else getattr(config, "UR_IP", "10.0.10.60")

        # Build runtime prelude with safety bindings and model-specific constants
        runtime_prelude = (
            f"# --- Multi-Robot Execution Prelude ({model.upper()}) ---\n"
            f"import builtins, os, sys\n"
            f"ROBOT_MODEL = '{model}'\n"
            f"ROBOT_IP = '{target_ip}'\n"
            f"UR_IP = '{getattr(config, 'UR_IP', '10.0.10.60')}'\n"
            f"NIRYO_IP = '{getattr(config, 'NIRYO_IP', '10.0.10.61')}'\n"
            f"os.environ['ROBOT_MODEL'] = '{model}'\n"
            f"os.environ['ROBOT_IP'] = '{target_ip}'\n"
            f"os.environ['UR_ROBOT_IP'] = '{getattr(config, 'UR_IP', '10.0.10.60')}'\n"
            f"os.environ['NIRYO_ROBOT_IP'] = '{getattr(config, 'NIRYO_IP', '10.0.10.61')}'\n"
            "try:\n"
            "    import ur_rtde\n"
            "    if 'rtde_control' not in sys.modules:\n"
            "        import ur_rtde.rtde_control; sys.modules['rtde_control'] = ur_rtde.rtde_control\n"
            "    if 'rtde_receive' not in sys.modules:\n"
            "        import ur_rtde.rtde_receive; sys.modules['rtde_receive'] = ur_rtde.rtde_receive\n"
            "    if 'rtde_io' not in sys.modules:\n"
            "        import ur_rtde.rtde_io; sys.modules['rtde_io'] = ur_rtde.rtde_io\n"
            "except Exception:\n"
            "    pass\n"
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
            "try:\n"
            "    import importlib.util as _ilu\n"
            f"    _spec = _ilu.spec_from_file_location('_sandbox_io', {os.path.join(work_dir, 'sandbox_io.py')!r})\n"
            "    _sbx = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_sbx)\n"
            "    _sbx.install(os.getcwd())\n"
            "except Exception as _e:\n"
            "    print('[Supervisor] File sandbox unavailable:', _e)\n"
            "# --- End Prelude ---\n\n"
        )

        # Create isolated execution directory in OS temp to prevent uvicorn dev-reload from triggering
        run_dir = tempfile.mkdtemp(prefix=f"robot_run_{submission_id}_{model}_")
        entry_file_name = (entry_point or "main.py").strip()

        if files and isinstance(files, dict) and len(files) > 0:
            for fname, fcontent in files.items():
                clean_name = os.path.basename(fname.strip())
                if clean_name:
                    dst_path = os.path.join(run_dir, clean_name)
                    if clean_name == entry_file_name:
                        with open(dst_path, "w", encoding="utf-8") as f:
                            f.write(runtime_prelude + (fcontent or code_str))
                    else:
                        with open(dst_path, "w", encoding="utf-8") as f:
                            f.write(fcontent or "")
            temp_path = os.path.join(run_dir, entry_file_name)
            if not os.path.exists(temp_path):
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(runtime_prelude + code_str)
        else:
            temp_path = os.path.join(run_dir, f"main_{model}.py")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(runtime_prelude + code_str)

        success = False
        try:
            extra_kwargs = {}
            if sys.platform != "win32":
                extra_kwargs["preexec_fn"] = os.setsid
            else:
                import subprocess
                extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            proc_env = os.environ.copy()
            proc_env["ROBOT_MODEL"] = model
            proc_env["ROBOT_IP"] = target_ip
            proc_env["UR_ROBOT_IP"] = getattr(config, "UR_IP", "10.0.10.60")
            proc_env["NIRYO_ROBOT_IP"] = getattr(config, "NIRYO_IP", "10.0.10.61")
            # Ensure isolated project run directory and repo are both discoverable
            curr_py_path = proc_env.get("PYTHONPATH", "")
            proc_env["PYTHONPATH"] = f"{run_dir}{os.pathsep}{work_dir}{os.pathsep}{curr_py_path}".strip(os.pathsep)

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=run_dir,
                env=proc_env,
                **extra_kwargs
            )
            slot.process = proc

            async def dispatch_log(text: str):
                if log_callback:
                    res = log_callback(text)
                    if asyncio.iscoroutine(res):
                        await res

            log_buffer = []
            last_flush = [0.0]

            async def flush_logs():
                if log_buffer:
                    chunk = "".join(log_buffer)
                    log_buffer.clear()
                    await append_logs(submission_id, chunk)
                last_flush[0] = asyncio.get_running_loop().time()

            async def stream_output():
                try:
                    while True:
                        try:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            decoded = line.decode("utf-8", errors="replace")
                            log_buffer.append(decoded)
                            await dispatch_log(decoded)
                            if asyncio.get_running_loop().time() - last_flush[0] > 0.3:
                                await flush_logs()
                        except Exception:
                            break
                finally:
                    await flush_logs()

            try:
                await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stream_output()),
                    timeout=timeout_sec
                )

                if slot.is_aborted:
                    msg = f"\n[Supervisor] Script on {model.upper()} was aborted ({slot.emergency_halt_reason or 'Emergency Stop'}).\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "aborted")
                elif proc.returncode == 0:
                    success = True
                    msg = f"\n[Supervisor] Execution on {model.upper()} completed successfully with returncode 0.\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "completed")
                else:
                    msg = f"\n[Supervisor] Script on {model.upper()} terminated with error code {proc.returncode}.\n"
                    await append_logs(submission_id, msg)
                    await dispatch_log(msg)
                    await update_status(submission_id, "failed")

            except asyncio.TimeoutError:
                await flush_logs()
                msg = f"\n[Supervisor WATCHDOG] Execution on {model.upper()} timed out (> {timeout_sec}s). Force killing process.\n"
                await append_logs(submission_id, msg)
                await dispatch_log(msg)
                await self.abort_model(model, reason="Execution Timeout Watchdog")
                await update_status(submission_id, "failed")

        except Exception as e:
            msg = f"\n[Supervisor ERROR] Execution on {model.upper()} failed to launch: {e}\n"
            await append_logs(submission_id, msg)
            if log_callback:
                res = log_callback(msg)
                if asyncio.iscoroutine(res):
                    await res
            await update_status(submission_id, "failed")

        finally:
            if run_dir and os.path.exists(run_dir):
                if on_outputs:
                    try:
                        outputs = sandbox_io.collect_outputs(
                            run_dir, files or {}, skip={entry_file_name, os.path.basename(temp_path)}
                        )
                        if outputs:
                            res = on_outputs(outputs)
                            if asyncio.iscoroutine(res):
                                await res
                            note = f"\n[Supervisor] Saved {len(outputs)} generated file(s) to project: {', '.join(outputs.keys())}\n"
                            await append_logs(submission_id, note)
                            if log_callback:
                                r2 = log_callback(note)
                                if asyncio.iscoroutine(r2):
                                    await r2
                    except Exception as e:
                        print(f"[Supervisor] Could not save generated files: {e}")
                try:
                    shutil.rmtree(run_dir, ignore_errors=True)
                except Exception:
                    pass
            elif temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

            if not success and model == "ur" and getattr(config, "UR_IP", None):
                asyncio.create_task(asyncio.to_thread(
                    _send_ur_emergency_halt, config.UR_IP, getattr(config, "ROBOT_SECONDARY_PORT", 30002)
                ))

            slot.process = None
            slot.submission_id = None
            slot.is_executing = False

        return success

    # --- Emergency Abort ---

    async def abort_model(self, robot_model: str, reason: str = "Emergency Stop"):
        """Emergency Stop for a specific robot model arm."""
        model = self._normalize_model(robot_model)
        slot = self.get_slot(model)
        slot.is_aborted = True
        slot.is_emergency_halted = True
        slot.emergency_halt_reason = reason
        print(f"[EMERGENCY STOP] Triggered on {model.upper()}: {reason}")

        proc = slot.process
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
                print(f"[EMERGENCY STOP] Error killing {model} process group: {e}")

        # Send hardware emergency halt in background thread
        if model == "ur" and getattr(config, "UR_IP", None):
            asyncio.create_task(asyncio.to_thread(
                _send_ur_emergency_halt, config.UR_IP, getattr(config, "ROBOT_SECONDARY_PORT", 30002)
            ))
        elif model == "niryo" and getattr(config, "NIRYO_IP", None):
            asyncio.create_task(asyncio.to_thread(
                _send_niryo_emergency_halt, config.NIRYO_IP
            ))

        sub_id = slot.submission_id
        if sub_id:
            await append_logs(
                sub_id,
                f"\n[EMERGENCY STOP] Execution on {model.upper()} aborted immediately: {reason}\n"
            )
            await update_status(sub_id, "aborted")

    async def abort_all(self, reason: str = "Instructor pressed Emergency Abort"):
        """Emergency stops all robot models."""
        for model in list(self.slots.keys()):
            await self.abort_model(model, reason=reason)

    async def abort_current(self, reason: str = "Instructor Emergency Stop"):
        """Backwards compatible alias for abort_all."""
        await self.abort_all(reason)


# Global singleton engine instance
engine = ExecutionEngine()
