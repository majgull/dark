#!/usr/bin/env bash
# no-binaries — every tracked, non-empty file must be text and under 1MB,
# so an unexpected binary cannot reach a commit. Run from the repo root.
set -euo pipefail
MAX=${NO_BINARIES_MAX:-1048576}
fail() { echo "NO-BINARIES FAIL: $*" >&2; exit 1; }
while IFS= read -r -d '' f; do
  [ -f "$f" ] && [ -s "$f" ] || continue
  LC_ALL=C grep -Iq . -- "$f" || fail "binary file tracked: $f"
  [ "$(stat -c %s -- "$f")" -le "$MAX" ] || fail "file over $MAX bytes tracked: $f"
done < <(git ls-files -z)
echo "no-binaries OK"
