#!/usr/bin/env python3
"""refwork — build a spec-only workspace for a second reference.

The hidden acceptance of a task is validated against one reference, its
`oracle/`. A single reference cannot say whether the tests agree with the
specification or only with that one solution, so a second model is asked
for a second reference, written from the specification text alone.

This tool lays out what that writer sees: one directory per task holding
the task's *starting* tree (the template, then every earlier step of its
chain, then its own `start/`) and `SPEC.md`, the specification text out of
`task.toml`. The task's own `oracle/` and its `acceptance/` never appear.

  python3 tools/refwork.py --out DIR --tasks obs-01-parse,obs-02-count [--git]

--git makes DIR a git repository with one commit, which is what a session
arm needs to work in.
"""

import argparse
import os
import subprocess
import sys
import tomllib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the runner package sits beside the bench in this checkout; its tasks.py is
# the one resolver for the task path
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runner"))

from dark.tasks import TaskError, task_dir_index  # noqa: E402

# The task set lives in its own repository (github.com/majgull/dark-tasks); DARK_TASKS
# names one or more checkouts of it, `:` separated. Unset, the two example
# tasks beside this tool are used.
TASKS_ROOT = os.environ.get("DARK_TASKS", HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from validate import build_start  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--tasks-dir", help="task-set root(s), ':' separated (default: $DARK_TASKS, else the examples here)")
    ap.add_argument("--templates", default=os.path.join(os.path.dirname(HERE), "templates"))
    ap.add_argument("--git", action="store_true")
    args = ap.parse_args()
    try:
        index = task_dir_index(args.tasks_dir or TASKS_ROOT)
    except TaskError as e:
        print(f"tasks: {e}")
        return 1
    os.makedirs(args.out, exist_ok=True)
    for tid in args.tasks.split(","):
        if tid not in index:
            print(f"tasks: unknown task {tid!r}")
            return 1
        tdir = index[tid][1]
        with open(os.path.join(tdir, "task.toml"), "rb") as f:
            meta = tomllib.load(f)
        dest = os.path.join(args.out, tid)
        build_start(tdir, meta["lang"], args.templates, dest)
        with open(os.path.join(dest, "SPEC.md"), "w") as f:
            f.write(f"# {meta['title']}\n\n{meta['spec'].strip()}\n")
        print(f"{tid}: {dest} ({len(os.listdir(dest))} entries at the root)")
    if args.git:
        def git(*a):
            subprocess.run(["git", "-c", "user.name=refwork", "-c", "user.email=refwork@bench", *a],
                           cwd=args.out, check=True, capture_output=True)
        if not os.path.isdir(os.path.join(args.out, ".git")):
            git("init", "-q", "-b", "main")
        git("add", "-A", "-f")
        git("commit", "-q", "--allow-empty", "-m", "starting trees and specifications")
        print(f"git repository at {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
