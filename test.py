from rtde_receive import RTDEReceiveInterface as RTDEReceive

rtde_r = RTDEReceive("10.0.10.60")

# Get the current joint positions [rad]
joint_q = rtde_r.getActualQ()
print("Joint positions (rad):", joint_q)

# Get the current TCP pose [x, y, z, rx, ry, rz]
tcp_pose = rtde_r.getActualTCPPose()
print("TCP position (m):            ", tcp_pose[:3])
print("TCP orientation (axis-angle):", tcp_pose[3:])