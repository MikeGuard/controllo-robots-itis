"""Bug 5: a motionless Niryo must read steadily and must not accumulate phantom path length."""
import random, time
import telemetry
from telemetry import NiryoSampler, URSampler


class FakeNed:
    """pyniryo stand-in: resting encoders with ~0.03 deg / 0.2 mm noise, optionally moving."""
    def __init__(self): self.q = [0.1, 0.3, -1.3, 0.0, 0.2, 0.0]; self.p = [0.2, 0.0, 0.3, 0, 0, 0]; random.seed(1)
    def get_joints(self): return [v + random.gauss(0, 0.0005) for v in self.q]
    def get_pose(self):
        class P(list):
            def to_list(self): return list(self)
        return P([v + random.gauss(0, 0.0002) for v in self.p])


def _sampler(ned):
    s = NiryoSampler(); s.robot = ned; s._robot_ip = s._ip(); return s


def _run(s, n, t0=1000.0, dt=0.15):
    out = None
    for i in range(n):
        q_raw = s.robot.get_joints(); p_raw = s.robot.get_pose().to_list()
        q, p = s._settle(q_raw, p_raw)
        out = (q, p, s._integrate(t0 + i * dt, p, q, None))
    return out


def test_resting_ned_reads_steady_and_adds_no_path():
    s = _sampler(FakeNed())
    _run(s, 5)
    s.reset_stats(); s._shown_q = s._shown_pose = None
    first_q, _, _ = _run(s, 1)
    qs = []
    for k in range(200):
        q, p, d = _run(s, 1, t0=2000 + k * 0.15)
        qs.append(tuple(round(v, 6) for v in q))
    assert len(set(qs)) <= 2                         # displayed joints do not flicker
    assert s.path_length < 0.002                     # was growing continuously
    assert s.max_speed < 0.01 and d["speed"] == 0.0


def test_real_motion_is_still_measured():
    ned = FakeNed(); s = _sampler(ned)
    _run(s, 3)
    s.reset_stats(); s._shown_q = s._shown_pose = None
    t = 5000.0
    for i in range(40):                              # move 0.2 m over 6 s
        ned.p[0] += 0.005; ned.q[0] += 0.01
        _run(s, 1, t0=t + i * 0.15)
    assert 0.17 < s.path_length < 0.23
    assert s.max_speed > 0.02


def test_ur_integration_unchanged_by_deadband():
    s = URSampler(); assert s.POSE_DEADBAND_M == 0.0
    s.reset_stats()
    for i in range(5):
        s._integrate(10 + i * 0.2, [0.001 * i, 0, 0, 0, 0, 0], [0.0] * 6, 0.005)
    assert abs(s.path_length - 0.004) < 1e-9
