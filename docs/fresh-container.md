# A stranger runs the Quick start

Recorded 2026-09-28 in a fresh, throwaway cloud container (Ubuntu 24.04.4 LTS, Python 3.11.15, no `pytest` installed for that Python), cloning this repository at the commit shown into an empty directory and running the README's Quick start verbatim, in a new virtual environment. The transcript is what the container printed, cut where marked; each command is followed by its exit code and its wall-clock time. The dark-tasks check and the paper's commands were not run in this record.

```
== 2026-09-28T18:37:25Z fresh container, Ubuntu 24.04.4 LTS, python3 3.11.15, no pytest
== clone of dark d7c59f1
$ python3 -m venv .venv && . .venv/bin/activate
== exit 0, 5s
$ pip install '.[dev]'
[...]
Successfully built dark
Successfully installed dark-0.6.0 iniconfig-2.3.0 packaging-26.3 pluggy-1.6.0 pygments-2.21.0 pytest-9.1.1
== exit 0, 4s
$ dark --help
usage: dark [-h] [--conf CONF]
            {check-config,preflight,admission,status,shift,materialize,archive-work,power,stage,review,long,user,done,envelope,spec-review,bench,export-harbor,mcp,ledger-tail,digest,void,abort}
            ...
[...]
== exit 0, 1s
$ (cd runner && bash verify.sh)
[...]
Ran 636 tests in 179.248s

OK (skipped=1)
== config
config OK: 2 models across 1 providers, 7 classes, windows []; host org dark, state /root/.dark
== no binaries
no-binaries OK
verify OK
== exit 0, 180s
$ python3 bench/tools/check_tasks.py
2 task(s), 0 problem(s)
== exit 0, 0s
== all five exit 0
```

With `pip install .` in place of `pip install '.[dev]'`, the same container ended `verify.sh` with `ModuleNotFoundError: No module named 'pytest'` from `runner/tests/test_repo_hygiene.py` and `FAILED (errors=1, skipped=1)`, and never printed `verify OK`.
