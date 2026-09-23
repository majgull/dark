"""dark/power.py: a meter that integrates real counters and says null when
it cannot read one, never a number."""

import io
import unittest

from dark import power


class FakeProc:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(lines))
        self.killed = False

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        self.t += 1.0
        return self.t


def meter(cpu_reads, gpu_lines, cpu="root@px", gpu="operator@vm"):
    reads = list(cpu_reads)

    def ssh_out(host, cmd, timeout=15):
        return reads.pop(0) if reads else (255, "")

    def ssh_stream(host, cmd):
        return FakeProc(gpu_lines)
    return power.Meter(cpu, gpu, ssh_out=ssh_out, ssh_stream=ssh_stream, clock=Clock())


class Integrate(unittest.TestCase):
    def test_trapezoid(self):
        self.assertAlmostEqual(power.integrate([(0, 100.0), (3600, 100.0)]), 100.0)
        self.assertAlmostEqual(power.integrate([(0, 0.0), (3600, 200.0)]), 100.0)
        self.assertEqual(power.integrate([(0, 50.0)]), 0.0)


class MeterRecords(unittest.TestCase):
    def test_both_sensors(self):
        # 3.6e9 uJ = 1 Wh on the package; two GPUs at 100 W and 50 W for the
        # sampled span (the fake clock ticks one second per reading)
        m = meter([(0, "1000 1000000000000\n"), (0, "3600001000 1000000000000\n")],
                  ["0, 100.0\n", "1, 50.0\n", "0, 100.0\n", "1, 50.0\n", "0, 100.0\n", "1, 50.0\n"])
        rec = m.start().stop()
        self.assertAlmostEqual(rec["wh_cpu"], 1.0, places=3)
        # gpu0: 100 W over the two-second gaps between its three samples (t=103,105,107): 4 s
        # gpu1: 50 W over 4 s; total (400 + 200) Ws / 3600
        self.assertAlmostEqual(rec["wh_gpu"], (400 + 200) / 3600.0, places=3)
        self.assertAlmostEqual(rec["wh"], rec["wh_cpu"] + rec["wh_gpu"], places=3)
        self.assertIsNone(rec["wh_overhead"])
        self.assertEqual((rec["power"]["gpu_samples"], rec["power"]["gpu_devices"]), (6, 2))
        self.assertNotIn("errors", rec["power"])

    def test_counter_wrap(self):
        m = meter([(0, "3240000000 3600000000\n"), (0, "360000000 3600000000\n")], [], gpu="")
        rec = m.start().stop()
        self.assertAlmostEqual(rec["wh_cpu"], 0.2, places=3)
        self.assertIsNone(rec["wh_gpu"])
        self.assertEqual(rec["wh"], rec["wh_cpu"])

    def test_unreachable_sensors_are_null(self):
        m = meter([(255, ""), (255, "")], ["garbage\n"])
        rec = m.start().stop()
        self.assertEqual((rec["wh_cpu"], rec["wh_gpu"], rec["wh"]), (None, None, None))
        self.assertTrue(any(e.startswith("cpu:") for e in rec["power"]["errors"]))
        self.assertTrue(any(e.startswith("gpu:") for e in rec["power"]["errors"]))

    def test_sensors_off(self):
        rec = power.Meter("", "").start().stop()
        self.assertEqual((rec["wh"], rec["power"]["cpu_host"], rec["power"]["gpu_host"]), (None, None, None))


if __name__ == "__main__":
    unittest.main()
