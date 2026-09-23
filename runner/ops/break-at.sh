#!/usr/bin/env bash
# ops/break-at.sh — launch a shift and break it at a named point, all on the
# runner host. Run ON the runner host (the smoke suite calls it over one ssh):
#
#   ops/break-at.sh <arm> <tier> <tasks> <state> <after-seconds> [extra dark shift args]
#
# <state> is executing | verifying | staging, or pass:<task-id> to break as
# soon as that task of the shift has delivered.
#
# The smoke suite first drove this from the hub: launch over one ssh, read
# the shift id over another, start the watcher over a third. Each round trip
# is seconds and the run it was trying to interrupt is about a minute, so
# the watcher arrived after the run had already ended and every kill point
# reported nothing. Here the launch, the id and the watcher are one process
# on the machine the shift runs on.
#
# Prints "SHIFT <id>" and then ops/kill_at.py's one JSON line. Exits with
# kill_at's status: 0 only if the point was reached and the process killed.
set -uo pipefail
cd "$(dirname "$0")/.."
ARM=${1:?usage: break-at.sh <arm> <tier> <tasks> <state> <after> [extra...]}
TIER=${2:?}; TASKS=${3:?}; STATE=${4:?}; AFTER=${5:?}; shift 5
AT=(--to "$STATE")
case "$STATE" in pass:*) AT=(--to pass --task "${STATE#pass:}");; esac
LOG=/tmp/dark-break-$ARM.log

python3 -m dark shift --tasks "$TASKS" --tier "$TIER" --arm "$ARM" --no-push "$@" >"$LOG" 2>&1 </dev/null &
PID=$!
SH=""
for _ in $(seq 1 60); do
  SH=$(python3 ops/led.py --kind shift.start --last --field shift 2>/dev/null | tail -1)
  case "$SH" in *"-$ARM") break;; *) SH=""; sleep 0.5;; esac
done
if [ -z "$SH" ]; then
  kill -9 $PID 2>/dev/null
  echo "SHIFT none"
  echo '{"reached": false, "killed": false, "detail": "the shift wrote no shift.start record"}'
  exit 1
fi
echo "SHIFT $SH"
python3 ops/kill_at.py --pid "$PID" --shift "$SH" "${AT[@]}" --after "$AFTER" --timeout 900
rc=$?
kill -9 $PID 2>/dev/null
exit $rc
