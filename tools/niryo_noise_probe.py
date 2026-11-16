#!/usr/bin/env python3
"""
READ-ONLY probe: measures how much a motionless Niryo Ned's joint/pose readings wander.
Sends NO motion commands (only get_joints / get_pose). Do not run while a program is using the arm.

    python tools/niryo_noise_probe.py 10.0.130.49 [seconds]

Send the printed summary back; it is used to tune the telemetry noise band (telemetry.NiryoSampler).
"""
import math, statistics, sys, time

ip = sys.argv[1]
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
from pyniryo import NiryoRobot
r = NiryoRobot(ip)
qs, ps, ts = [], [], []
t_end = time.time() + secs
while time.time() < t_end:
    ts.append(time.time()); qs.append([float(v) for v in r.get_joints()]); p = r.get_pose(); ps.append(p.to_list() if hasattr(p, "to_list") else list(p))
    time.sleep(0.15)
r.close_connection()
print(f"samples={len(qs)} over {ts[-1]-ts[0]:.1f}s")
for i in range(len(qs[0])):
    col = [q[i] for q in qs]
    print(f"joint{i+1}: mean={math.degrees(statistics.mean(col)):9.3f} deg  std={math.degrees(statistics.pstdev(col)):.4f} deg  peak-to-peak={math.degrees(max(col)-min(col)):.4f} deg  distinct={len(set(col))}")
for i, n in enumerate("xyz"):
    col = [p[i] for p in ps]
    print(f"pose {n}: std={statistics.pstdev(col)*1000:.3f} mm  peak-to-peak={(max(col)-min(col))*1000:.3f} mm")
