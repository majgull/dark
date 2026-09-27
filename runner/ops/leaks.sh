#!/usr/bin/env bash
# leaks — gitleaks over this repository's git history, so a credential
# cannot reach a push. Run from the repo root; an optional argument is a
# git log range (e.g. lan/main..HEAD) to scan instead of the whole history.
# A missing gitleaks is a failure, not a skip: a gate that passes because
# its tool is absent passes everything. CI installs the pinned release
# (.github/workflows/ci.yml); locally, put gitleaks >= 8.19 on PATH.
set -euo pipefail
command -v gitleaks >/dev/null || {
  echo "LEAKS FAIL: gitleaks not on PATH (https://github.com/gitleaks/gitleaks/releases)" >&2; exit 1; }
args=(git --no-banner --no-color --redact --verbose --exit-code 1 --log-level warn)
[ $# -gt 0 ] && args+=(--log-opts "$1")
if ! out=$(gitleaks "${args[@]}" . 2>&1); then
  echo "$out" >&2
  echo "LEAKS FAIL: gitleaks found a credential in the history (see above)" >&2
  exit 1
fi
echo "leaks OK"
