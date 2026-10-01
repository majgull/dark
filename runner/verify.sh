#!/usr/bin/env bash
# verify.sh — the one gate for dark/runner: tests, config validation, no
# binaries in the tree, no credentials in its history (gitleaks) and no test
# that names /tmp. The agent-side VM never runs this file (the runner is not a
# factory workload); CI does.
set -euo pipefail
cd "$(dirname "$0")"
TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/dark-verify.XXXXXX")"
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT
echo "== tests"
PYTHONWARNINGS=ignore::ResourceWarning python3 -m unittest discover -s tests -t . -q
echo "== config"
python3 -m dark check-config
echo "== release pressure"
python3 -m dark.pressure ../CHANGELOG.md
echo "== no binaries"
bash ops/no-binaries.sh
echo "== no private"
bash ops/no-private.sh
echo "== no credentials"
(cd .. && bash runner/ops/leaks.sh)
echo "== no /tmp in tests"
if grep -rnE "[\"']/tmp(/|[\"'])" tests; then
  echo "tests use tempfile, never /tmp by name"
  exit 1
fi
echo "verify OK"
