import os
import json

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CONFIG_PATH = os.path.join(CONFIG_DIR, "runtime_config.json")

# Default settings
ROBOT_MODEL = "ur"           # "ur" | "niryo"
ROBOT_IP = "192.168.56.101"  # Active UR robot IP (updated at runtime)
UR_IP = "192.168.56.101"     # Persisted UR-specific IP
NIRYO_IP = "10.10.10.10"     # Persisted Niryo One IP
ROBOT_RTDE_PORT = 30004
ROBOT_SECONDARY_PORT = 30002
NIRYO_PORT = 9090            # Default pyniryo TCP port
EXECUTION_TIMEOUT_SEC = 45.0

# Load runtime override if it exists
if os.path.exists(RUNTIME_CONFIG_PATH):
    try:
        with open(RUNTIME_CONFIG_PATH, "r") as _f:
            _data = json.load(_f)
            if "robot_model" in _data and _data["robot_model"] in ("ur", "niryo"):
                ROBOT_MODEL = _data["robot_model"]
            if "ur_ip" in _data and _data["ur_ip"].strip():
                UR_IP = _data["ur_ip"].strip()
            if "niryo_ip" in _data and _data["niryo_ip"].strip():
                NIRYO_IP = _data["niryo_ip"].strip()
            # Also handle legacy single robot_ip key
            if "robot_ip" in _data and _data["robot_ip"].strip():
                _legacy_ip = _data["robot_ip"].strip()
                if "ur_ip" not in _data:
                    UR_IP = _legacy_ip
        # Set the active ROBOT_IP based on model
        ROBOT_IP = NIRYO_IP if ROBOT_MODEL == "niryo" else UR_IP
    except Exception:
        pass
