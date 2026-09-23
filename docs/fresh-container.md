# A stranger runs the two commands

Recorded 2026-09-23 from a fresh `debian:13` container with only `git`, `jq`, `python3` and `python3-pytest` installed, cloning this repository and the paper's at the commits shown and running the commands both READMEs give. Nothing else was on the machine. The transcript is what the container printed; the paper's two commands are included because the paper is the tool's first use.

```
== 2026-09-23T00:01:44Z debian:13 fresh container
== installed: git jq python3 python3-pytest
== clone of dark e8d0bee
== clone of dark-tasks 76fca4e
== clone of dark-paper c08759e
$ cd dark/runner && python3 -m pytest -q tests
362 passed in 135.24s (0:02:15)
$ cd dark/bench && python3 tools/check_tasks.py ../../dark-tasks
31 task(s), 0 problem(s)
$ cd dark-paper && python3 tools/fetch-evidence.py --source /src/dark-evidence   # the dataset is still private; a local copy of revision 8c8d87a5 stands in
40 files, 40 fetched, 0 bad
$ cd dark-paper && python3 appendix.py --check
evidence/ledger/2026-09-06.jsonl: no row carries more distinct replies than calls
375 rows checked, 0 disagree with numbers.md
== exit codes 0 0 0 0
== all four exit 0
container exit 0
```
