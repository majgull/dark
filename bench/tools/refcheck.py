#!/usr/bin/env python3
"""refcheck — run a task's hidden acceptance against a reference other than
its oracle (L1: the second reference, and the mutation variants).

Given a directory holding a solved copy of a task's starting tree (what
tools/refwork.py laid out and a writer then edited), this rebuilds the
pristine starting tree as a git repository, lays the solved tree over it as
one commit (files the solution deleted are deleted), and runs the tree's
own `.dark/verify.sh` and then the task's `acceptance/run.sh`, printing
one line per CHECK.

  python3 tools/refcheck.py --work DIR --tasks a,b [--json OUT] [--keep]

DIR holds one subdirectory per task, named by task id, as refwork.py wrote
it. Exit 1 if any task's acceptance did not pass; the point of the exercise
is to look at which check disagreed, not only at the exit code.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The task set lives in its own repository (github.com/majgull/dark-tasks); DARK_TASKS
# names its checkout. Unset, the two example tasks beside this tool are used.
TASKS_ROOT = os.environ.get("DARK_TASKS", HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from validate import GOCACHE, build_start, checks, copy_tree, run  # noqa: E402

SKIP = {".git", "SPEC.md", "NOTES.md"}


def rel_files(root):
    out = set()
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            r = os.path.relpath(os.path.join(dp, fn), root)
            if r.split(os.sep)[0] in SKIP or r in SKIP:
                continue
            out.add(r)
    return out


def apply_solution(work, dest):
    """Lay the solved tree over the pristine one: files it changed or added
    are copied, files it removed are removed. SPEC.md and NOTES.md belong to
    the exercise, not to the solution, and are left out."""
    before, after = rel_files(dest), rel_files(work)
    for r in sorted(before - after):
        os.remove(os.path.join(dest, r))
    for r in sorted(after):
        d = os.path.join(dest, r)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(work, r), d)
    return sorted(after - before), sorted(before - after)


def one(tid, work_dir, templates, keep):
    tdir = os.path.join(TASKS_ROOT, "tasks", tid)
    with open(os.path.join(tdir, "task.toml"), "rb") as f:
        lang = tomllib.load(f)["lang"]
    dest = tempfile.mkdtemp(prefix=f"ref-{tid}-")
    build_start(tdir, lang, templates, dest)

    def git(*a):
        subprocess.run(["git", "-c", "user.name=refcheck", "-c", "user.email=refcheck@bench", *a],
                       cwd=dest, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", "-A", "-f")
    git("commit", "-q", "--allow-empty", "-m", "starting tree")
    origin = dest.rstrip("/") + "-origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", dest, origin], check=True, capture_output=True)
    git("remote", "add", "origin", origin)
    added, removed = apply_solution(work_dir, dest)
    git("add", "-A", "-f")
    git("commit", "-q", "--allow-empty", "-m", "reference")
    copy_tree(os.path.join(tdir, "acceptance"), os.path.join(dest, ".acceptance"))
    vrc, vout = run(["bash", ".dark/verify.sh"], dest)
    arc, aout = run(["bash", ".acceptance/run.sh"], dest)
    ok_n, total = checks(aout)
    failed = [n for n, v in _named(aout) if v == "fail"]
    crash = arc != 0 and "Traceback (most recent call last)" in aout
    res = {"task": tid, "verify": vrc, "acceptance": arc, "checks_ok": ok_n, "checks": total,
           "failed": failed, "crash": crash, "added": added, "removed": removed}
    print(f"{tid:26} verify {'ok' if vrc == 0 else 'RED'}, acceptance "
          f"{'ok' if arc == 0 else 'RED'} ({ok_n}/{total} checks)"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    if keep:
        res["tree"] = dest
        print(f"    kept {dest}")
    else:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(origin, ignore_errors=True)
    if vrc != 0:
        res["verify_tail"] = vout.strip().splitlines()[-10:]
    if arc != 0:
        res["acceptance_tail"] = aout.strip().splitlines()[-20:]
    return res


def _named(out):
    import re
    return re.findall(r"^CHECK\s+(\S+)\s+(ok|fail)", out, re.MULTILINE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="directory holding one solved tree per task")
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--templates", default=os.path.join(os.path.dirname(HERE), "templates"))
    ap.add_argument("--json", help="write every result to this file")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    out = []
    for tid in args.tasks.split(","):
        wd = os.path.join(args.work, tid)
        if not os.path.isdir(wd):
            print(f"{tid:26} MISSING: no {wd}")
            out.append({"task": tid, "missing": True})
            continue
        out.append(one(tid, wd, args.templates, args.keep))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
    bad = [r for r in out if r.get("missing") or r.get("acceptance") != 0]
    print(f"{len(bad)} task(s) the acceptance did not pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
