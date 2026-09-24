#!/bin/bash
# The factory's feedback loop for this repo (thread 928). The agent runs it
# after each edit; CI re-runs it as the merge gate. gofmt -w auto-formats
# (so weak models needn't produce perfect layout) and `go vet` is the cheap
# static-analysis "review" gate — bouncer's riskscan reproduced with the
# toolchain's own built-ins. Agents must NOT edit this file.
set -e
cd "$(dirname "$0")/.."
# The in-VM agent runs as root with no $HOME set, so Go can't locate its
# build cache; give it stable defaults. CI already has HOME, so the :- keeps
# its values.
export HOME="${HOME:-/root}"
export GOCACHE="${GOCACHE:-$HOME/.cache/go-build}"
export GOPATH="${GOPATH:-$HOME/go}"
gofmt -w .
go vet ./...
# Build OUTSIDE the tree: `go build ./...` drops a binary named after the module
# into the repo root, and a renamed module escapes the gitignore (948: a 2.7MB
# ELF shipped on an earlier repository that way). The agent's `git add -A` must never
# see a build product.
out=$(mktemp -d) && go build -o "$out/" ./... && rm -rf "$out"
go test ./...
echo "verify OK"
