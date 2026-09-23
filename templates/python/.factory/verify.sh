#!/bin/bash
# The gate: every .py compiles, every test_*.py passes. Dependency-free
# (no pytest) so it runs in the agent VM, in CI, and on any host alike.
# A repo fresh from the template has no sources yet — that is a green
# state by design; the first issue brings the first code.
set -euo pipefail
cd "$(dirname "$0")/.."
shopt -s nullglob

fail() { echo "VERIFY FAIL: $*" >&2; exit 1; }

py=(./*.py)
if [ ${#py[@]} -eq 0 ]; then
  echo "no sources yet — gate green by design (fresh from template)"
  echo "verify OK"
  exit 0
fi

echo "== python syntax =="
python3 -m py_compile "${py[@]}" || fail "syntax"

echo "== unit tests =="
for t in test_*.py; do
  python3 "$t" || fail "$t"
done

echo "verify OK"
