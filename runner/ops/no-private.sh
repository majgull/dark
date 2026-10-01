#!/usr/bin/env bash
# no-private — no tracked text file may carry an address in the private
# 192.168/16 range or a home-directory path /home/<name>/ that names a
# real user. Such a value belongs to one machine, not to an example, so rule
# 40 asks for a documentation address or a ~ path instead. The script and
# its own test are skipped: each must write the patterns it looks for. Run
# from the repository root, or from runner/ the way verify.sh calls it.
set -euo pipefail
bad=0
fail() { echo "NO-PRIVATE FAIL: $*" >&2; bad=1; }
# /home/<name>/ with <name> not exactly `user`: each alternative rules out
# one prefix of `user` (a name that starts the same way but is longer, such
# as `users`, still matches the last alternative).
HOME_RE='/home/([^u/][^/]*|u[^s/][^/]*|us[^e/][^/]*|use[^r/][^/]*|user[^/][^/]*)/'
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  case "$f" in
    */no-private.sh|no-private.sh|*/test_no_private.py|test_no_private.py) continue ;;
  esac
  LC_ALL=C grep -Iq . -- "$f" || continue
  while IFS= read -r hit; do
    fail "$f:$hit"
  done < <(LC_ALL=C grep -n '192\.168\.' -- "$f" || true)
  while IFS= read -r hit; do
    fail "$f:$hit"
  done < <(LC_ALL=C grep -nE "$HOME_RE" -- "$f" || true)
done < <(git ls-files -z)
[ "$bad" -eq 0 ] || exit 1
echo "no-private OK"
