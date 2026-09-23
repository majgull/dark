# A stranger runs the two commands

Recorded 2026-09-23 from a fresh `debian:13` container with only `git`, `jq`, `python3` and `python3-pytest` installed, cloning this repository and the paper's at the commits shown and running the commands both READMEs give. Nothing else was on the machine. The transcript is what the container printed; the paper's two commands are included because the paper is the tool's first use.

```
== 2026-09-23T00:32:58Z debian:13 fresh container
== installed: git jq python3 python3-pytest
== clone of dark a9ba7f7
== clone of dark-tasks 76fca4e
== clone of dark-paper 55008b5
$ cd dark/runner && python3 -m pytest -q tests
362 passed in 133.73s (0:02:13)
$ cd dark/bench && python3 tools/check_tasks.py ../../dark-tasks
31 task(s), 0 problem(s)
$ cd dark-paper && python3 tools/fetch-evidence.py
40 files, 40 fetched, 0 bad
$ cd dark-paper && python3 appendix.py --check
evidence/ledger/2026-09-06.jsonl: no row carries more distinct replies than calls
375 rows checked, 0 disagree with numbers.md
== exit codes 0 0 0 0
== all four exit 0
container exit 0
```
