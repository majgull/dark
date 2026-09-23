# mutants

One directory per task, `mutants/<task-id>/<name>/`: the task's own oracle
with one clause of the specification broken on purpose, driven by
`tools/mutants.py`. A one-line `WHY` names the sentence it violates.

Lives here rather than under `tasks/<id>/mutants/` because `tasks/` is
what `dark/tasks.py:tests_version()` hashes as the test version (the last
commit touching it): a commit that only changes a mutant must not move
that version.
