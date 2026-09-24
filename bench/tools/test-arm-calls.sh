#!/usr/bin/env bash
# Prove the tool-call accounting rule in tools/arm.sh against the trails the
# rule must classify, plus one it must not.
# The rule is read out of arm.sh itself, not copied, so the test proves
# the deployed script.
set -uo pipefail
ARM=${1:?usage: test-arm-calls.sh <path to arm.sh>}

BLOCK=$(awk '/^CALL_ARGS=\(\)$/,/^fi$/' "$ARM")
[ -n "$BLOCK" ] || { echo "FAIL: no decision block found in $ARM"; exit 1; }

fail=0
run() {  # pre post denials expected_calls expected_args
  PRE=$1 POST=$2 DENIALS=$3
  eval "$BLOCK"
  got_args="${CALL_ARGS[*]:-}"
  if [ "$CALLS" = "$4" ] && [ "$got_args" = "$5" ]; then
    echo "ok    $1 pre, $2 post, $3 refused -> $CALLS"
  else
    echo "FAIL  $1 pre, $2 post, $3 refused -> '$CALLS' [$got_args], wanted '$4' [$5]"
    fail=1
  fi
}

run 29 27 2  "27 (+2 refused)" "--calls 27"
run 26 25 1  "25 (+1 refused)" "--calls 25"
run 30 29 1  "29 (+1 refused)" "--calls 29"
run 91 90 1  "90 (+1 refused)" "--calls 90"
run 37 35 0  "NOT MEASURED (37 pre, 35 post, 0 refused: the gap is not the refusals)" ""
run 28 28 0  "28" "--calls 28"
run 14 14 0  "14" "--calls 14"
run "" "" 0  "NOT MEASURED (? pre, ? post, no footer)" ""

[ $fail = 0 ] && echo "all cases ok" || echo "cases failed"
exit $fail
