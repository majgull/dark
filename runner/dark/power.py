"""dark/power.py — measured energy over a run window.

Two sensors, both real counters, neither a placeholder:

- CPU: the RAPL package energy counter on the Proxmox host
  (/sys/class/powercap/intel-rapl:0/energy_uj), read once at the start and
  once at the end of the window. The difference is the package energy
  over the window, exact to the counter's resolution; a wrap is handled
  with max_energy_range_uj.
- GPU: `nvidia-smi` inside the model VM (the GPUs are passed through, the
  host cannot see them), streamed at 1 Hz for the window and integrated
  per device by the trapezoid rule over receipt times.

What neither sees: board, RAM, disks, fans, PSU loss. That remainder is
NOT MEASURED and the record says so (`wh_overhead` is null); a wall
meter is the only way to close it. A sensor that cannot be read yields
null, never a number, and the run goes on: measurement must not fail a
run. Everything is over ssh from the runner host, bounded, BatchMode.
"""

import subprocess
import threading
import time

RAPL = "/sys/class/powercap/intel-rapl:0"
# one nvidia-smi per second, not `-lms`: over a pipe nvidia-smi block-buffers
# its loop output and the runner sees no lines until it is killed. Each
# invocation exits and flushes; the outer timeout ends the loop if the ssh
# ever leaks.
GPU_CMD = ("timeout 14400 bash -c 'while :; do nvidia-smi --query-gpu=index,power.draw "
           "--format=csv,noheader,nounits || exit 1; sleep 1; done'")


def _ssh_out(host, cmd, timeout=15):
    """(rc, stdout) of one bounded ssh call; rc 255 on a transport error."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 255, ""


def _ssh_stream(host, cmd):
    """A Popen streaming stdout lines from `cmd` on `host` (the GPU sampler)."""
    return subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)


def integrate(samples):
    """Wh of one device from [(t, W), ...] by the trapezoid rule; 0.0 for
    fewer than two samples (no span to integrate over)."""
    wh = 0.0
    for (t0, w0), (t1, w1) in zip(samples, samples[1:]):
        wh += (w0 + w1) / 2.0 * (t1 - t0) / 3600.0
    return wh


class Meter:
    """start() then stop() -> the run.end power fields. Either host may be
    "" (that sensor is off) or unreachable (that sensor is null)."""

    def __init__(self, cpu_host, gpu_host, ssh_out=_ssh_out, ssh_stream=_ssh_stream, clock=time.monotonic):
        self.cpu_host, self.gpu_host = cpu_host, gpu_host
        self.ssh_out, self.ssh_stream, self.clock = ssh_out, ssh_stream, clock
        self.errors = []
        self.e0 = self.t0 = self.rng = None
        self.proc = None
        self.samples = {}     # gpu index -> [(t, W)]
        self._thread = None

    # --- CPU: two counter reads -------------------------------------------
    def _rapl(self):
        rc, out = self.ssh_out(self.cpu_host, f"cat {RAPL}/energy_uj {RAPL}/max_energy_range_uj")
        parts = out.split()
        if rc != 0 or len(parts) < 1:
            return None, None
        try:
            return int(parts[0]), (int(parts[1]) if len(parts) > 1 else None)
        except ValueError:
            return None, None

    # --- GPU: one streamed nvidia-smi ---------------------------------------
    def _read(self):
        for line in self.proc.stdout:
            try:
                idx, w = line.split(",")
                self.samples.setdefault(int(idx), []).append((self.clock(), float(w)))
            except ValueError:
                continue

    def start(self):
        if self.cpu_host:
            self.e0, self.rng = self._rapl()
            self.t0 = self.clock()
            if self.e0 is None:
                self.errors.append(f"cpu: {self.cpu_host}: no RAPL reading")
        if self.gpu_host:
            try:
                self.proc = self.ssh_stream(self.gpu_host, GPU_CMD)
                self._thread = threading.Thread(target=self._read, daemon=True)
                self._thread.start()
            except OSError as e:
                self.proc = None
                self.errors.append(f"gpu: {self.gpu_host}: {e}")
        return self

    def stop(self):
        rec = {"wh_cpu": None, "wh_gpu": None, "wh": None, "wh_overhead": None}
        power = {"cpu_host": self.cpu_host or None, "gpu_host": self.gpu_host or None,
                 "cpu_seconds": None, "gpu_samples": 0, "gpu_span_s": None, "gpu_devices": 0}
        if self.cpu_host and self.e0 is not None:
            e1, _ = self._rapl()
            t1 = self.clock()
            if e1 is None:
                self.errors.append(f"cpu: {self.cpu_host}: no RAPL reading at stop")
            else:
                d = e1 - self.e0
                if d < 0 and self.rng:
                    d += self.rng
                if d >= 0:
                    rec["wh_cpu"] = round(d / 3.6e9, 3)
                    power["cpu_seconds"] = round(t1 - self.t0, 1)
                else:
                    self.errors.append("cpu: counter went backwards without a known range")
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            if self._thread:
                self._thread.join(timeout=5)
            n = sum(len(v) for v in self.samples.values())
            power["gpu_samples"], power["gpu_devices"] = n, len(self.samples)
            if n:
                ts = [t for v in self.samples.values() for t, _ in v]
                power["gpu_span_s"] = round(max(ts) - min(ts), 1)
            if any(len(v) >= 2 for v in self.samples.values()):
                rec["wh_gpu"] = round(sum(integrate(v) for v in self.samples.values()), 3)
            else:
                self.errors.append(f"gpu: {self.gpu_host}: fewer than two samples")
        elif self.gpu_host:
            self.errors.append(f"gpu: {self.gpu_host}: sampler never started")
        parts = [v for v in (rec["wh_cpu"], rec["wh_gpu"]) if v is not None]
        rec["wh"] = round(sum(parts), 3) if parts else None
        if self.errors:
            power["errors"] = self.errors[:6]
        rec["power"] = power
        return rec


def probe(cpu_host, gpu_host, seconds=3, ssh_out=_ssh_out):
    """One live reading per sensor for `dark power --probe`: package watts
    over `seconds`, and the GPUs' draw now. Nulls where a sensor is off or
    unreadable."""
    out = {"cpu_watts": None, "gpu_watts": None, "gpu": []}
    if cpu_host:
        rc0, a = ssh_out(cpu_host, f"cat {RAPL}/energy_uj")
        t0 = time.monotonic()
        time.sleep(seconds)
        rc1, b = ssh_out(cpu_host, f"cat {RAPL}/energy_uj")
        dt = time.monotonic() - t0
        if rc0 == 0 and rc1 == 0:
            try:
                out["cpu_watts"] = round((int(b.strip()) - int(a.strip())) / 1e6 / dt, 1)
            except ValueError:
                pass
    if gpu_host:
        rc, o = ssh_out(gpu_host, "nvidia-smi --query-gpu=index,name,power.draw --format=csv,noheader,nounits")
        if rc == 0:
            for line in o.strip().splitlines():
                try:
                    idx, name, w = [p.strip() for p in line.split(",")]
                    out["gpu"].append({"index": int(idx), "name": name, "watts": float(w)})
                except ValueError:
                    continue
            if out["gpu"]:
                out["gpu_watts"] = round(sum(g["watts"] for g in out["gpu"]), 1)
    return out
