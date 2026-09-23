#!/usr/bin/env python3
"""ops/kill_at.py — break an unattended shift at a named point.

Resume is only proven if the break happens somewhere specific and the
relaunch is checked from there. This waits until a run of the given shift
has moved into a named state and then kills the shift process:

  executing   the executor VM is up and the model call is what that state does
  verifying   verify.sh came back green: the branch is being pushed
  staging     the branch is pushed and the artefact is being judged
  pass        --task's run has passed: the break lands between two steps of
              a chain, which is where resume has to skip a delivered step

--after delays the kill inside that state (a state entered a moment ago is
not yet the state's real work). One JSON line on stdout says what it saw
and what it killed. Exit 1 if the point was never reached, if the run
ended before the kill, or if the process was already gone.
"""

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ops.led import read  # noqa: E402


def runs_of(evs, shift):
    return {r["run"] for r in evs if r.get("kind") == "run.start" and r.get("shift") == shift}


def ended(evs, runs):
    return {r["run"] for r in evs if r.get("kind") == "run.end" and r.get("run") in runs}


def alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kill_at.py")
    ap.add_argument("--ledger", default="~/.dark/ledger.jsonl")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--shift", required=True)
    ap.add_argument("--to", required=True, choices=["executing", "verifying", "staging", "pass"])
    ap.add_argument("--task", help="with --to pass: the task whose pass is the break point")
    ap.add_argument("--after", type=float, default=10.0, help="seconds to wait inside the state")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)

    t0 = time.time()
    out = {"shift": args.shift, "point": args.to, "pid": args.pid,
           "reached": False, "killed": False, "run": None, "state_at_kill": None, "detail": ""}
    while time.time() - t0 < args.timeout:
        evs = read(args.ledger)
        runs = runs_of(evs, args.shift)
        done = ended(evs, runs)
        if args.to == "pass":
            hits = [r for r in evs if r.get("kind") == "run.end" and r.get("shift") == args.shift
                    and r.get("task") == args.task and r.get("outcome") == "pass"]
        else:
            hits = [r for r in evs if r.get("kind") == "run.transition"
                    and r.get("run") in runs and r.get("to") == args.to and r["run"] not in done]
        if hits:
            run = hits[-1]["run"]
            out["reached"], out["run"] = True, run
            time.sleep(args.after)
            evs = read(args.ledger)
            if args.to != "pass" and run in ended(evs, runs_of(evs, args.shift)):
                out["detail"] = f"{run} ended before the kill; the point is too late for this task"
                print(json.dumps(out))
                return 1
            last = [r for r in evs if r.get("kind") == "run.transition" and r.get("run") == run]
            out["state_at_kill"] = "pass" if args.to == "pass" else (last[-1]["to"] if last else None)
            if not alive(args.pid):
                out["detail"] = "the shift process was already gone"
                print(json.dumps(out))
                return 1
            os.kill(args.pid, signal.SIGKILL)
            time.sleep(1.0)
            out["killed"] = not alive(args.pid)
            out["detail"] = f"killed {args.pid} at {out['state_at_kill']} of {run}"
            print(json.dumps(out))
            return 0 if out["killed"] else 1
        if args.to != "pass" and runs and runs <= done:
            out["detail"] = "every run of the shift ended before the point was reached"
            print(json.dumps(out))
            return 1
        if not alive(args.pid):
            out["detail"] = "the shift process ended before the point was reached"
            print(json.dumps(out))
            return 1
        time.sleep(args.poll)
    out["detail"] = f"the point was never reached in {args.timeout}s"
    print(json.dumps(out))
    return 1


if __name__ == "__main__":
    sys.exit(main())
