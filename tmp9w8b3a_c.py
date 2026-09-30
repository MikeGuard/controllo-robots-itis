import builtins
def printf(*args, **kwargs):
    kwargs.setdefault('flush', True)
    if len(args) > 1 and isinstance(args[0], str) and ('%' in args[0]):
        try:
            print(args[0] % args[1:], **kwargs)
            return
        except Exception:
            pass
    print(*args, **kwargs)
builtins.printf = printf
# --- End Prelude ---
# Tontini Cristiano 5 AEN
# 25/09/2026
import numpy as np
import time as t
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive
from rtde_io import RTDEIOInterface as RTDEIO
rtde_c = RTDEControl("10.0.10.60")
rtde_r = RTDEReceive("10.0.10.60")
rtde_io_ = RTDEIO("10.0.10.60")

# Move to a safe home position using moveJ (joint space)
# Arguments: joint positions [rad], speed [rad/s], acceleration [rad/s^2]
i=0
home_q = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
muovi1 = [0, -np.pi/3, -np.pi/2, -np.pi/3, np.pi/2, 0.0]
muovi2 = [0, -np.pi/2, -np.pi/3, -np.pi/2, np.pi/3, 0.0]
while i<5 :
    rtde_c.moveJ(home_q, 0.5, 0.5)
    t.sleep(0.2)
    rtde_c.moveJ(muovi1, 0.5, 0.5)
    t.sleep(0.2)
    rtde_c.moveJ(muovi2, 0.5, 0.5)
    t.sleep(0.2)
    rtde_io_.setStandardDigitalOut(1, True)
    t.sleep(0.2)
    rtde_c.moveJ(muovi1, 0.5, 0.5)
    t.sleep(0.2)
    rtde_c.moveJ(home_q, 0.5, 0.5)
    t.sleep(0.2)
    rtde_io_.setStandardDigitalOut(1, False)
    t.sleep(0.2)
    rtde_io_.setStandardDigitalOut(0, True)
    t.sleep(0.2)
    rtde_io_.setStandardDigitalOut(0, False)
    i+=1

rtde_c.stopScript()