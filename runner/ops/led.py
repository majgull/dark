#!/usr/bin/env python3
"""ops/led.py — read the ledger from a script.

The smoke suite and the bench scripts have to ask the ledger simple questions
from shell ("did this shift already pass this task", "what branch did that run
deliver", "how many runs ended in this shift") without embedding python in
an ssh string. One record per line as JSON, or one field per line with
--field, or a count with --count. Exit 1 when nothing matched, so a shell
`if` reads naturally.

Read-only: it never writes to the ledger.
"""

import argparse
import json
import os
import sys

DEFAULT = "~/.dark/ledger.jsonl"


def read(path):
    out = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line: the writer was killed mid-record
    except FileNotFoundError:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="led.py")
    ap.add_argument("--ledger", default=DEFAULT)
    ap.add_argument("--kind")
    ap.add_argument("--shift")
    ap.add_argument("--task")
    ap.add_argument("--run")
    ap.add_argument("--arm")
    ap.add_argument("--tier")
    ap.add_argument("--outcome")
    ap.add_argument("--to", help="run.transition: the state moved into")
    ap.add_argument("--last", action="store_true", help="only the newest match")
    ap.add_argument("--count", action="store_true", help="print how many matched")
    ap.add_argument("--field", help="print this field instead of the record")
    args = ap.parse_args(argv)

    want = {k: v for k, v in (("kind", args.kind), ("shift", args.shift), ("task", args.task),
                              ("run", args.run), ("arm", args.arm), ("tier", args.tier),
                              ("outcome", args.outcome), ("to", args.to)) if v is not None}
    rows = [r for r in read(args.ledger) if all(r.get(k) == v for k, v in want.items())]
    if args.last:
        rows = rows[-1:]
    if args.count:
        print(len(rows))
        return 0 if rows else 1
    for r in rows:
        if args.field:
            v = r.get(args.field)
            print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))
        else:
            print(json.dumps(r, sort_keys=True))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
