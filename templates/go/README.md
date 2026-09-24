# go template (dark/templates)

The starting tree every Go task repo is materialised from: `.dark/verify.sh` (gofmt, vet, out-of-tree build, tests: the executor's feedback loop and the stager's first gate), a `.gitignore` that keeps build products out, `.editorconfig`, `go.mod` (module `app`) and a `main.go` that builds. The task's `start/` overlay is applied on top; hidden acceptance tests never live here.
