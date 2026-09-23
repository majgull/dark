#!/usr/bin/env python3
"""dark stager — staging is in the definition of done. Runs INSIDE a fresh
VM (never the executor's), injected alone via cloud-init with
/opt/task.json. Stdlib only.

Clones the pushed work branch, runs the repo's own verify script (an
out-of-tree build plus the executor's tests), then unpacks the hidden
acceptance set the executor never saw and runs `run.sh` from the repo
root. `CHECK <name> ok|fail` lines give partial credit; the exit code is
the verdict. Posts ONE STAGE-DONE comment with a DARK: stage tag. It never
decides an outcome: the runner maps the tag.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request


def _load_task():
    try:
        with open(os.environ.get("DARK_TASK", "/opt/task.json")) as f:
            return json.load(f)
    except OSError:
        return {}


TASK = _load_task()
API = f"{TASK.get('gitea', '')}/api/v1"
REPO = TASK.get("repo", "")
WORK = os.environ.get("DARK_WORK", "/opt/stage")
ACC = ".acceptance"
T0 = time.time()


def gitea(path, method="GET", data=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"token {TASK['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "{}")


def comment(body):
    body = body.replace(TASK.get("token", "\0"), "***")
    try:
        gitea(f"/repos/{REPO}/issues/{TASK['issue']}/comments", "POST", {"body": body})
    except Exception as e:  # noqa: BLE001
        print(f"comment failed: {e}", file=sys.stderr)


def tag(ev, **kw):
    return "DARK:" + json.dumps({"v": 2, "ev": ev, **kw}, sort_keys=True)


def parse_checks(output):
    """(ok, total) from CHECK lines; a suite without them is one check."""
    found = re.findall(r"^CHECK\s+(\S+)\s+(ok|fail)\b", output, re.MULTILINE)
    return sum(1 for _, v in found if v == "ok"), len(found)


def report(ok, checks_ok, checks_total, detail, env=False):
    """env=True: the staging environment failed, not the work (the runner
    turns it into fail:structural, never fail:capability). The nonce the
    runner gave this VM is echoed so the executor cannot forge the verdict."""
    comment(f"STAGE-DONE {'ok' if ok else 'fail'} checks={checks_ok}/{checks_total}\n"
            f"```\n{detail[-3000:]}\n```\n"
            + tag("stage", ok=ok, checks_ok=checks_ok, checks_total=checks_total,
                  detail=detail[:120] + (" ... " + detail[-280:] if len(detail) > 400 else detail[120:]),
                  seconds=int(time.time() - T0), env=bool(env), nonce=TASK.get("nonce", "")))
    return 0 if ok else 1


PROTECTED = (".factory", ".gitea")


def tampered():
    """Protected paths changed on the branch relative to main: the executor's
    reply protocol refuses them, but model code running in the executor VM
    holds the agent token and git. The branch is the evidence."""
    r = subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=WORK, capture_output=True, text=True)
    if r.returncode != 0:
        return None  # no main to compare against (the runner materialises one; report, do not guess)
    r = subprocess.run(["git", "diff", "--name-only", "origin/main", "HEAD", "--", *PROTECTED],
                       cwd=WORK, capture_output=True, text=True)
    return [p for p in r.stdout.split("\n") if p.strip()]


def run(cmd, timeout):
    try:
        r = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return 124, f"{(e.stdout or '')}{(e.stderr or '')}\nTIMEOUT after {timeout}s"


def clone_url():
    base = TASK.get("git_url") or TASK["gitea"]
    if base.startswith("http"):
        base = base.replace("://", f"://factory-agent:{TASK['token']}@", 1)
    return f"{base}/{REPO}.git"


def main():
    try:
        r = subprocess.run(["git", "clone", "-q", "--branch", TASK["branch"], "--single-branch",
                            clone_url(), WORK], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return report(False, 0, 0, "STAGE-ENV clone timed out after 300s", env=True)
    if r.returncode != 0:
        return report(False, 0, 0, "STAGE-ENV clone failed: " + r.stderr[-300:], env=True)
    changed = tampered()
    if changed:
        return report(False, 0, 0, f"STAGE-ENV protected paths changed on the branch: {changed}", env=True)
    timeout = int(TASK.get("timeout", 600))
    if os.path.exists(f"{WORK}/.factory/verify.sh"):
        rc, out = run(["bash", ".factory/verify.sh"], timeout)
        if rc != 0:
            return report(False, 0, 0, "STAGE-ENV verify.sh red in staging:\n" + out, env=True)
    blob = base64.b64decode(TASK.get("acceptance_tar_b64", ""))
    if not blob:
        return report(False, 0, 0, "STAGE-ENV no acceptance set in task", env=True)
    dest = f"{WORK}/{ACC}"
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/") or m.issym() or m.islnk():
                return report(False, 0, 0, f"STAGE-ENV bad path in acceptance tar: {m.name}", env=True)
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # python < 3.12 without the filter argument
            tf.extractall(dest)
    if not os.path.exists(f"{dest}/run.sh"):
        return report(False, 0, 0, "STAGE-ENV acceptance has no run.sh", env=True)
    rc, out = run(["bash", f"{ACC}/run.sh"], timeout)
    ok_n, total = parse_checks(out)
    if total == 0:
        ok_n, total = (1 if rc == 0 else 0), 1
    return report(rc == 0, ok_n, total, out)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        report(False, 0, 0, f"STAGE-ENV crashed: {e!r}", env=True)
        raise
