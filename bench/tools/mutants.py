#!/usr/bin/env python3
"""mutants — the other direction of the ground-truth check (L1).

The second reference asks whether the hidden tests reject a correct
solution. This asks the opposite: whether they accept a wrong one. A
*mutant* is the task's own reference with one clause of the specification
broken on purpose. Each lives in `mutants/<id>/<name>/` (moved out of
`tasks/`, decision 950: tasks/ is what dark/tasks.py:tests_version() hashes
as the test version, and pulling a mutant must not move it), holding a
one-line file `WHY` naming the sentence of the specification it violates
and either the files it replaces (laid over the oracle) or a `MUTATE.py`
run inside the solved tree, which keeps a one-line break to one line.

  python3 tools/mutants.py [--tasks a,b] [--json OUT]

A mutant the acceptance still passes is a hole in the tests and is printed
as MISSED. So is one that only `visible-tests` catches: that check runs the
tests in the tree, which under a mutant are the oracle's own, and a model
solving the task writes its own. Exit 1 on either, or on a missing WHY.
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

from refcheck import one  # noqa: E402
from validate import build_start, overlay  # noqa: E402


def solved_tree(tdir, lang, templates, dest, mutant=None):
    build_start(tdir, lang, templates, dest)
    overlay(os.path.join(tdir, "oracle"), dest)
    if mutant:
        script = os.path.join(mutant, "MUTATE.py")
        overlay(mutant, dest)
        for junk in ("WHY", "MUTATE.py"):
            p = os.path.join(dest, junk)
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(script):
            subprocess.run([sys.executable, script], cwd=dest, check=True)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks")
    ap.add_argument("--templates", default=os.path.join(os.path.dirname(HERE), "templates"))
    ap.add_argument("--json")
    args = ap.parse_args()
    root = os.path.join(TASKS_ROOT, "tasks")
    only = set(args.tasks.split(",")) if args.tasks else None
    out, bad = [], 0
    for tid in sorted(os.listdir(root)):
        tdir = os.path.join(root, tid)
        mdir = os.path.join(TASKS_ROOT, "mutants", tid)
        if not os.path.isdir(mdir) or (only and tid not in only):
            continue
        with open(os.path.join(tdir, "task.toml"), "rb") as f:
            lang = tomllib.load(f)["lang"]
        for name in sorted(os.listdir(mdir)):
            m = os.path.join(mdir, name)
            if not os.path.isdir(m):
                continue
            why = os.path.join(m, "WHY")
            clause = open(why).read().strip() if os.path.exists(why) else ""
            work = tempfile.mkdtemp(prefix=f"mut-{tid}-{name}-")
            solved_tree(tdir, lang, args.templates, work, mutant=m)
            r = one(f"{tid}", work, args.templates, keep=False)
            shutil.rmtree(work, ignore_errors=True)
            # `visible-tests` runs the tests in the tree. Under a mutant those
            # are the oracle's own, which a model solving the task would not
            # have written, so a mutant only `visible-tests` catches is one
            # the hidden set does not catch by itself.
            hidden = [f for f in r["failed"] if f != "visible-tests"]
            caught = r["acceptance"] != 0
            # a mutant that makes a check raise instead of printing a verdict
            # is still caught, by the acceptance dying: the run is red and no
            # branch is accepted. It is recorded separately because a check
            # that crashes names no clause.
            crash = r.get("crash") and not hidden
            row = {"task": tid, "mutant": name, "clause": clause, "caught": caught,
                   "caught_hidden": bool(hidden) or bool(crash), "crash": bool(crash),
                   "failed": r["failed"], "verify": r["verify"]}
            out.append(row)
            if not clause:
                print(f"  {tid}/{name}: no WHY file naming the clause it violates")
                bad += 1
            if hidden:
                print(f"  {tid}/{name}: caught by {', '.join(hidden)} -- {clause}")
            elif crash:
                print(f"  {tid}/{name}: caught by a crash in the acceptance, no verdict line -- {clause}")
            elif caught:
                print(f"  {tid}/{name}: caught ONLY by the tree's own tests -- {clause}")
                bad += 1
            else:
                print(f"  {tid}/{name}: MISSED, the acceptance passed it -- {clause}")
                bad += 1
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
    caught_n = sum(1 for r in out if r.get("caught_hidden"))
    print(f"{caught_n}/{len(out)} mutants caught by a hidden check, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
