#!/usr/bin/env python3
"""validate — prove each task's acceptance set agrees with its oracle and
rejects its starting tree (the oracle exists only to validate the hidden
tests). Runs locally, no VM: template + start (+ oracle) in a
temp dir, the tree's own verify.sh, then acceptance/run.sh at the root.

  python3 tools/validate.py [--templates DIR] [--tasks a,b] [--keep]

For every task it reports:
  oracle:  verify.sh green and run.sh exit 0 (every CHECK ok)   -> required
  start:   run.sh exit != 0 on the untouched starting tree       -> required
           (an acceptance that passes before any work is broken)
`oracle/.delete` lists paths the reference solution removes.
Exit 1 if any task fails either half."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the runner package sits beside the bench in this checkout; its tasks.py is
# the one resolver for the task path
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runner"))

from dark.tasks import TaskError, task_dir_index  # noqa: E402

# The task set lives in its own repository (github.com/majgull/dark-tasks); DARK_TASKS
# names one or more checkouts of it, `:` separated. Unset, the two example
# tasks beside this tool are used.
TASKS_ROOT = os.environ.get("DARK_TASKS", HERE)


def copy_tree(src, dst):
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            s = os.path.join(dp, fn)
            d = os.path.join(dst, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)


def overlay(src, dest):
    """Lay src over dest; a .delete file in src lists paths removed first."""
    dele = os.path.join(src, ".delete")
    if os.path.exists(dele):
        with open(dele) as f:
            for line in f:
                p = os.path.join(dest, line.strip())
                if line.strip() and os.path.exists(p):
                    os.remove(p)
    for dp, dns, fns in os.walk(src):
        for fn in fns:
            if fn == ".delete":
                continue
            s = os.path.join(dp, fn)
            d = os.path.join(dest, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)


def task_after(task_dir):
    import tomllib
    with open(os.path.join(task_dir, "task.toml"), "rb") as f:
        return tomllib.load(f).get("after")


def build_start(task_dir, lang, templates, dest):
    """The tree a run starts from: the template, then (chains)
    every earlier step's start/ and oracle/ in order, then this task's
    start/. This task's own oracle and acceptance are not in it, which is
    what makes it usable as a spec-only workspace (tools/refwork.py)."""
    copy_tree(os.path.join(templates, lang), dest)
    chain = []
    t = task_dir
    while task_after(t):
        t = os.path.join(os.path.dirname(t), task_after(t))
        chain.insert(0, t)
    for step in chain:
        for sub in ("start", "oracle"):
            if os.path.isdir(os.path.join(step, sub)):
                overlay(os.path.join(step, sub), dest)
    if os.path.isdir(os.path.join(task_dir, "start")):
        overlay(os.path.join(task_dir, "start"), dest)


def build(task_dir, lang, templates, with_oracle, dest):
    """The starting tree as a git repository with an origin, then this task's
    oracle/ on top when asked, then its acceptance in .acceptance.
    This mirrors what the runner builds."""
    build_start(task_dir, lang, templates, dest)
    # the starting tree is main on an origin, as the runner materialises it,
    # so the acceptance's git-dependent checks (files touched, moved
    # unchanged, bodies unchanged) run here too instead of self-skipping;
    # the oracle is a commit on top of it
    def git(*args):
        subprocess.run(["git", "-c", "user.name=validate", "-c", "user.email=validate@bench", *args], cwd=dest, check=True,
                       capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", "-A", "-f")
    git("commit", "-q", "--allow-empty", "-m", "starting tree")
    origin = dest.rstrip("/") + "-origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", dest, origin], check=True, capture_output=True)
    git("remote", "add", "origin", origin)
    if with_oracle and os.path.isdir(os.path.join(task_dir, "oracle")):
        overlay(os.path.join(task_dir, "oracle"), dest)
        git("add", "-A", "-f")
        git("commit", "-q", "--allow-empty", "-m", "oracle")
    acc = os.path.join(dest, ".acceptance")
    copy_tree(os.path.join(task_dir, "acceptance"), acc)


# One Go build cache on disk, reused by every run of this tool and of
# refcheck/mutants. A fresh mkdtemp here was never removed: each run left
# ~80 MB in /tmp, which is a RAM-backed tmpfs on the bench laptop (54 dirs,
# 3.2 GB by 2026-09-23). Go trims its own cache; DARK_VALIDATE_GOCACHE moves it.
GOCACHE = os.environ.get("DARK_VALIDATE_GOCACHE") or os.path.join(
    os.path.expanduser("~"), ".cache", "dark-validate", "go-build")
os.makedirs(GOCACHE, exist_ok=True)


def run(cmd, cwd, timeout=600):
    # the acceptance scripts default GOCACHE to /root/... (they run as root in
    # the fresh judging VM); locally we are not root
    env = dict(os.environ)
    env["GOCACHE"] = GOCACHE
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    return r.returncode, r.stdout + r.stderr


def checks(out):
    found = re.findall(r"^CHECK\s+(\S+)\s+(ok|fail)", out, re.MULTILINE)
    return sum(1 for _, v in found if v == "ok"), len(found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", default=os.path.join(os.path.dirname(HERE), "templates"))
    ap.add_argument("--tasks-dir", help="task-set root(s), ':' separated (default: $DARK_TASKS, else the examples here)")
    ap.add_argument("--tasks")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    try:
        index = task_dir_index(args.tasks_dir or TASKS_ROOT)
    except TaskError as e:
        print(f"tasks: {e}")
        return 1
    only = set(args.tasks.split(",")) if args.tasks else None
    bad = 0
    for tid in sorted(index):
        if only and tid not in only:
            continue
        tdir = index[tid][1]
        import tomllib
        with open(os.path.join(tdir, "task.toml"), "rb") as f:
            lang = tomllib.load(f)["lang"]
        for half in ("oracle", "start"):
            dest = tempfile.mkdtemp(prefix=f"val-{tid}-{half}-")
            build(tdir, lang, args.templates, half == "oracle", dest)
            vrc, vout = run(["bash", ".dark/verify.sh"], dest)
            arc, aout = run(["bash", ".acceptance/run.sh"], dest)
            ok_n, total = checks(aout)
            if half == "oracle":
                good = vrc == 0 and arc == 0
                print(f"{tid:26} oracle: verify {'ok' if vrc == 0 else 'RED'}, acceptance {'ok' if arc == 0 else 'RED'} ({ok_n}/{total} checks)"
                      + ("" if good else "   <-- FAIL"))
            else:
                good = arc != 0
                print(f"{tid:26} start:  acceptance {'rejects' if arc != 0 else 'ACCEPTS'} the starting tree ({ok_n}/{total} checks)"
                      + ("" if good else "   <-- FAIL"))
            if not good:
                bad += 1
                print("    " + "\n    ".join((vout if vrc else "").strip().splitlines()[-8:] + aout.strip().splitlines()[-12:]))
            if args.keep:
                print(f"    kept {dest}")
            else:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.rmtree(dest.rstrip("/") + "-origin.git", ignore_errors=True)
    print(f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
