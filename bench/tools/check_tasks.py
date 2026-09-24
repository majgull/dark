#!/usr/bin/env python3
"""check_tasks — the bench's CI gate: every tasks/<id> is a valid record.
The runner's tasks.py holds the authority on where task sets live, so this
shares its resolver (stdlib only, no install) instead of keeping a second
copy that drifts; this tool catches the mistakes before a shift does.

  python3 tools/check_tasks.py [path ...]

Each path is a task-set root holding tasks/; several may be given, and each
may itself be a colon-separated list. Unset, $DARK_TASKS is used, and with
neither, the two example tasks beside this tool."""

import os
import sys
import tomllib

CLASSES = ("additive", "mechanical", "repair")
LANGS = ("go", "python")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the runner package sits beside the bench in this checkout; the resolver in
# dark/tasks.py is the one implementation of the task path
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runner"))

from dark.tasks import TaskError, task_dirs  # noqa: E402

# The task set lives in its own repository (github.com/majgull/dark-tasks); DARK_TASKS
# names one or more checkouts of it, `:` separated. Unset, the two example
# tasks beside this tool are used.
TASKS_ROOT = os.environ.get("DARK_TASKS", HERE)


def check(path):
    problems = []
    tid = os.path.basename(path)
    tpath = os.path.join(path, "task.toml")
    try:
        with open(tpath, "rb") as f:
            d = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [f"{tid}: task.toml: {e}"]
    if d.get("id") != tid:
        problems.append(f"{tid}: id {d.get('id')!r} != directory name")
    if d.get("class") not in CLASSES:
        problems.append(f"{tid}: class must be one of {CLASSES}")
    if d.get("lang") not in LANGS:
        problems.append(f"{tid}: lang must be one of {LANGS}")
    if not str(d.get("spec", "")).strip():
        problems.append(f"{tid}: spec is empty")
    me = d.get("may_edit", [])
    if not isinstance(me, list):
        problems.append(f"{tid}: may_edit must be a list")
    elif d.get("class") == "additive" and me:
        problems.append(f"{tid}: additive tasks grant no existing file")
    if not os.path.isfile(os.path.join(path, "acceptance", "run.sh")):
        problems.append(f"{tid}: acceptance/run.sh missing")
    after = d.get("after")
    if after is not None:  # chains: id order is chain order
        if not isinstance(after, str) or not after < tid:
            problems.append(f"{tid}: after {after!r} must be a task id that sorts before {tid!r}")
        elif not os.path.isfile(os.path.join(os.path.dirname(path), after, "task.toml")):
            problems.append(f"{tid}: after {after!r}: no such task")
    for sub in ("start", "oracle"):
        p = os.path.join(path, sub)
        if os.path.isdir(p) and not any(os.listdir(p)):
            problems.append(f"{tid}: {sub}/ is empty")
    return problems


def main():
    given = sys.argv[1:]
    # several path arguments are one PATH-style list, like the variable they
    # fall back to
    spec = os.pathsep.join(given) if given else TASKS_ROOT
    try:
        dirs = task_dirs(spec, fallback=HERE)
    except TaskError as e:
        print(f"tasks: {e}")
        return 1
    problems = []
    for _, d in dirs:
        problems += check(d)
    for p in problems:
        print(p)
    print(f"{len(dirs)} task(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
