#!/usr/bin/env python3
"""check_tasks — the bench's CI gate: every tasks/<id> is a valid record.
Stdlib only, standalone (the runner's loader is the authority at run time;
this catches the mistakes before a shift does)."""

import os
import sys
import tomllib

CLASSES = ("additive", "mechanical", "repair")
LANGS = ("go", "python")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The task set lives in its own repository (github.com/majgull/dark-tasks); DARK_TASKS
# names its checkout. Unset, the two example tasks beside this tool are used.
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
    if after is not None:  # chains (decision 17): id order is chain order
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
    root = os.path.join(sys.argv[1] if len(sys.argv) > 1 else TASKS_ROOT, "tasks")
    dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    problems = []
    for d in dirs:
        problems += check(os.path.join(root, d))
    for p in problems:
        print(p)
    print(f"{len(dirs)} task(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
