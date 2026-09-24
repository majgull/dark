# dark/templates

The starting trees the runner materialises task repos from: `go/` and `python/`. Each carries its `.dark/verify.sh` (the executor's feedback loop and the first gate of judging), a CI that runs it plus the no-binaries rule, and the ignore/editor files that keep build products and whitespace arguments out of history. A task's `start/` overlay goes on top; hidden acceptance tests never live in a template.

To rebuild: run `runner/ops/gitea-bootstrap.sh`, then push this repository.
