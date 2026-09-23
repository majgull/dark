#!/usr/bin/env bash
# ops/smoke.sh — the seven checks of feasibility B1 plus three kill points.
#
# Run from the hub: every runner command goes to the runner host over ssh
# (so the runs, the VMs, the sensors and the ledger are all there), and the
# one check that needs a chat session runs here, because that is where
# bouncer is.
#
#   ops/smoke.sh [--bench <bench checkout on this machine>]
#
# Words used below. A *shift* is one launch of the runner over a list of
# tasks, named by a shift id. A *run* is one task on one tier inside a
# shift. An *arm* is one way of solving a task: the runner's pipeline, or
# one chat session. The *work org* is the git organisation the task
# repositories are created in. A *trail* is the audit record bouncer keeps
# for a session.
#
# Each check prints one PASS or FAIL line with its evidence; the exit code
# is 0 only if every check passed. Nothing here measures a model: a check
# that needs a task to pass says so when it does not, and fails.
set -uo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
RUNNER_HOST=${DARK_RUNNER_HOST:-git-host}
WORK_ORG=${DARK_SMOKE_WORK_ORG:-dark-runs}
LOCAL_TIER=${DARK_SMOKE_LOCAL_TIER:-my/qwen-3.5-9b-nonthink}
CLOUD_TIER=${DARK_SMOKE_CLOUD_TIER:-deepseek-v4-flash:cloud}
TASK=${DARK_SMOKE_TASK:-hello-python}
# checks 4 and 5 need step 1 to pass, so they use a pair any tier passes:
# what they report is the machinery, not the model (obs-01-parse failed on
# the cheap cloud tier on the first smoke run and took both checks with it)
STEP1=${DARK_SMOKE_STEP1:-smoke-chain-a}
STEP2=${DARK_SMOKE_STEP2:-smoke-chain-b}
SESSION_ARM=${DARK_SMOKE_ARM:-session-dsf}
BENCH=${DARK_BENCH_HUB:-}
CHAIN_TASKS=${DARK_SMOKE_CHAIN:-obs-01-parse,obs-02-count,obs-03-package,obs-04-tzfix,obs-05-report,obs-06-typehints,smoke-chain-a,smoke-chain-b}
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=${DARK_SMOKE_OUT:-/tmp/dark-smoke-$STAMP}
mkdir -p "$OUT"
while [ $# -gt 0 ]; do
  case "$1" in
    --bench) BENCH=$2; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

PASSED=0; FAILED=0
say() { printf '%s\n' "$*" | tee -a "$OUT/smoke.log"; }
ok()  { PASSED=$((PASSED + 1)); say "PASS $1: $2"; }
no()  { FAILED=$((FAILED + 1)); say "FAIL $1: $2"; }
on_runner() { ssh -o BatchMode=yes "$RUNNER_HOST" "cd ~/dark-runner && DARK_WORK_ORG=$WORK_ORG $*" </dev/null; }
led() { on_runner "python3 ops/led.py $*"; }
# fld <dotted.path> reads a JSON record on stdin; an absent value prints empty
fld() { python3 -c '
import json, sys
d = json.loads(sys.stdin.read() or "{}")
for k in sys.argv[1].split("."):
    d = (d or {}).get(k) if isinstance(d, dict) else None
print("" if d is None else d)' "$1"; }

say "== dark smoke $STAMP: runner $RUNNER_HOST, work org $WORK_ORG, log $OUT"
say "== tiers: local $LOCAL_TIER, cloud $CLOUD_TIER; tasks $TASK, $STEP1 -> $STEP2"

# --------------------------------------------------------------------------
# 7. the work org holds no repository from the last round
# (first, because the checks below create repositories in it)
# --------------------------------------------------------------------------
# strict: the org must be empty, full stop. Exempting this round's own task
# names lets a go-launch push into last round's repositories, so strict is
# the default whenever the round runs the obs chain (a real launch);
# DARK_SMOKE_STRICT_ORG=1 or =0 says so outright.
STRICT_ORG=${DARK_SMOKE_STRICT_ORG:-auto}
if [ "$STRICT_ORG" = auto ]; then
  case "$TASK" in obs-*) STRICT_ORG=1;; *) STRICT_ORG=0;; esac
fi
ROUND_TASKS=" $TASK $STEP1 $STEP2 "
LEFTOVER=""
REPOS=$(on_runner "python3 -m dark archive-work --src $WORK_ORG" 2>&1 | sed -n 's|^ *'"$WORK_ORG"'/\([^ ]*\) ->.*|\1|p')
for r in $REPOS; do
  if [ "$STRICT_ORG" = 1 ]; then
    LEFTOVER="$LEFTOVER $r"
  else
    case "$ROUND_TASKS" in *" ${r#t-} "*) ;; *) LEFTOVER="$LEFTOVER $r";; esac
  fi
done
if [ -z "$LEFTOVER" ]; then
  ok "7 work org" "$WORK_ORG holds no repository from an earlier round (${REPOS:-none} present, strict=$STRICT_ORG)"
elif [ "$STRICT_ORG" = 1 ]; then
  no "7 work org" "$WORK_ORG is not empty:$LEFTOVER; a launch must start in an empty work org (dark archive-work --src $WORK_ORG --to <fresh org> --apply)"
else
  no "7 work org" "$WORK_ORG still holds:$LEFTOVER (archive them: dark archive-work --src $WORK_ORG --apply)"
fi

# --------------------------------------------------------------------------
# 1. preflight: orgs, tokens, VM host, the model VM wakes, both sensors read
# --------------------------------------------------------------------------
if on_runner "python3 -m dark preflight" >"$OUT/preflight.txt" 2>&1; then
  POWER=$(on_runner "python3 -m dark power --seconds 3" 2>/dev/null | tail -1)
  CPU=$(printf '%s' "$POWER" | fld cpu_watts)
  GPU=$(printf '%s' "$POWER" | fld gpu_watts)
  if [ -n "$CPU" ] && [ -n "$GPU" ]; then
    ok "1 preflight" "every check green; sensors read ${CPU} W package, ${GPU} W GPUs"
  else
    no "1 preflight" "checks green but a sensor is dead: package '${CPU:-null}', GPUs '${GPU:-null}'"
  fi
else
  no "1 preflight" "$(tail -3 "$OUT/preflight.txt" | tr '\n' ' ')"
fi

# --------------------------------------------------------------------------
# 2. every chain step validated: acceptance green on the oracle, red on the start
# --------------------------------------------------------------------------
if on_runner "python3 \$HOME/dark-bench/tools/validate.py --tasks $CHAIN_TASKS" >"$OUT/validate.txt" 2>&1; then
  ok "2 chain steps" "acceptance green on every oracle and red on every start: $(tail -1 "$OUT/validate.txt")"
else
  no "2 chain steps" "$(tail -3 "$OUT/validate.txt" | tr '\n' ' ')"
fi

# --------------------------------------------------------------------------
# 3. one metered run on a local tier and one on a cloud tier
# --------------------------------------------------------------------------
metered() {  # metered <name> <tier> <think>
  local name=$1 tier=$2 think=$3 rec sid
  on_runner "python3 -m dark shift --tasks $TASK --tier '$tier' --think $think --arm smoke-$name --no-push" \
    >"$OUT/metered-$name.txt" 2>&1
  # bind the record to this shift: reading "the last run of this arm" showed
  # the previous round's record when the new shift produced none at all
  sid=$(sed -n 's/^shift \([^:]*\):.*/\1/p' "$OUT/metered-$name.txt" | head -1)
  if [ -z "$sid" ]; then
    no "3 metered $name" "the shift printed no id; see $OUT/metered-$name.txt"
    return
  fi
  rec=$(led --kind run.end --shift "$sid" --task "$TASK" --last)
  printf '%s' "$rec" >"$OUT/metered-$name.json"
  if [ -z "$rec" ]; then
    no "3 metered $name" "shift $sid produced no run.end: $(led --kind block --shift "$sid" --last --field reason | head -c 200)"
    return
  fi
  local wh think_at chars alive vms outcome
  wh=$(printf '%s' "$rec" | fld wh); think_at=$(printf '%s' "$rec" | fld think)
  chars=$(printf '%s' "$rec" | fld reasoning_chars); outcome=$(printf '%s' "$rec" | fld outcome)
  alive=$(printf '%s' "$rec" | fld asserts.sensors_alive); vms=$(printf '%s' "$rec" | fld asserts.vms_destroyed)
  # a capability failure is the model's and leaves the meter intact; a
  # structural one is the instrument's and is what this check exists to
  # catch (the first round printed PASS on the push death that motivated
  # the work-org team check)
  if [ "$outcome" = "fail:structural" ] || [ "$outcome" = "abort" ]; then
    no "3 metered $name" "$outcome on $tier ($(printf '%s' "$rec" | fld reason)): the run never reached or never left the model; see $OUT/metered-$name.txt"
  elif [ -n "$wh" ] && [ -n "$think_at" ] && [ -n "$chars" ] && [ "$alive" = "True" ] && [ "$vms" = "True" ]; then
    ok "3 metered $name" "$outcome on $tier: ${wh} Wh, think $think_at, ${chars} reasoning chars, sensors alive, VMs destroyed"
  else
    no "3 metered $name" "$outcome on $tier: wh '${wh:-null}', think '${think_at:-null}', chars '${chars:-null}', sensors_alive '${alive:-null}', vms_destroyed '${vms:-null}'"
  fi
}
metered local "$LOCAL_TIER" none
metered cloud "$CLOUD_TIER" low

# --------------------------------------------------------------------------
# resuming a shift that was broken on purpose
# --------------------------------------------------------------------------
# --resume names the shift and nothing else: the runner repeats the launch
# it recorded. Naming the tasks and tier here is what made every resumed
# kill-point run of the first two rounds run at think=low against a
# think=none launch.
resume_shift() {  # resume_shift <label for the log> <shift>
  on_runner "python3 -m dark shift --resume $2 --no-push" >"$OUT/resume-$1.txt" 2>&1
}

# --------------------------------------------------------------------------
# 4. one chained pair: step 2 starts from step 1's delivery
# 5. kill the shift after step 1 and resume it
# --------------------------------------------------------------------------
OUTC=$(on_runner "bash ops/break-at.sh smoke-chain '$CLOUD_TIER' $STEP1,$STEP2 pass:$STEP1 0")
printf '%s\n' "$OUTC" >"$OUT/break-chain.txt"
SH=$(printf '%s\n' "$OUTC" | sed -n 's/^SHIFT //p' | tail -1)
RES=$(printf '%s\n' "$OUTC" | tail -1)
if [ -z "$SH" ] || [ "$SH" = "none" ] || [ "$(printf '%s' "$RES" | fld killed)" != "True" ]; then
  no "4 chained pair" "the shift was not broken after $STEP1: $(printf '%s' "$RES" | fld detail)"
  no "5 resume" "the shift was not broken after $STEP1"
else
  BR1=$(led --kind run.end --shift "$SH" --task "$STEP1" --outcome pass --last --field branch)
  ENDED=$(led --kind shift.end --shift "$SH" --count)
  STARTS_BEFORE=$(led --kind run.start --shift "$SH" --task "$STEP1" --count)
  STEP2_BEFORE=$(led --kind run.end --shift "$SH" --task "$STEP2" --count)
  say "== shift $SH broken after $STEP1 delivered $BR1; resuming"
  resume_shift smoke-chain "$SH"
  STARTS_AFTER=$(led --kind run.start --shift "$SH" --task "$STEP1" --count)
  SKIPPED=$(led --kind shift.resume --shift "$SH" --last --field skipped)
  BASE=$(led --kind chain.base --shift "$SH" --task "$STEP2" --last)
  BASE_KIND=$(printf '%s' "$BASE" | fld base); BASE_BR=$(printf '%s' "$BASE" | fld branch)
  if [ "$BASE_KIND" = "delivered" ] && [ "$BASE_BR" = "$BR1" ]; then
    ok "4 chained pair" "$STEP2 started from $STEP1's delivery $BASE_BR (recorded after the break, by the resumed shift)"
  else
    no "4 chained pair" "$STEP2 started from '${BASE_KIND:-nothing}' '${BASE_BR:-}' (expected the delivery $BR1)"
  fi
  # the break must have been real: a shift that finished by itself proves nothing
  if [ "$ENDED" = "0" ] && [ "$STEP2_BEFORE" = "0" ] && [ "$STARTS_AFTER" = "$STARTS_BEFORE" ] \
     && printf '%s' "$SKIPPED" | grep -q "$STEP1" && ! printf '%s' "$SKIPPED" | grep -q "$STEP2"; then
    ok "5 resume" "the broken shift wrote no shift.end and no $STEP2 run; the resumed one skipped only $SKIPPED and left $STEP1's $STARTS_BEFORE run.start alone"
  else
    no "5 resume" "shift.end before resume $ENDED, $STEP2 runs before resume $STEP2_BEFORE, $STEP1 run.start $STARTS_BEFORE -> $STARTS_AFTER, skipped '${SKIPPED:-nothing}'"
  fi
fi

# --------------------------------------------------------------------------
# K1-K3. break the shift mid model call, mid push, mid judging
# --------------------------------------------------------------------------
kill_point() {  # kill_point <name> <state> <after seconds>
  local name=$1 state=$2 after=$3 out sh res reached killed rerun outcome
  # launch and watcher in one process on the runner host: driving them from
  # here cost seconds per round trip and the run was over before the
  # watcher arrived
  out=$(on_runner "bash ops/break-at.sh smoke-$name '$LOCAL_TIER' $TASK $state $after --think none")
  printf '%s\n' "$out" >"$OUT/kill-$name.txt"
  sh=$(printf '%s\n' "$out" | sed -n 's/^SHIFT //p' | tail -1)
  res=$(printf '%s\n' "$out" | tail -1)
  reached=$(printf '%s' "$res" | fld reached); killed=$(printf '%s' "$res" | fld killed)
  local at; at=$(printf '%s' "$res" | fld state_at_kill)
  if [ -z "$sh" ] || [ "$sh" = "none" ] || [ "$reached" != "True" ] || [ "$killed" != "True" ]; then
    no "K $name" "$(printf '%s' "$res" | fld detail)"
    return
  fi
  # the point is only smoked if the run was still in that state when the
  # process died: a run that had moved on is a different kill
  if [ "$at" != "$state" ]; then
    no "K $name" "the kill landed in $at, not in $state (lower --after for this task)"
    return
  fi
  resume_shift "smoke-$name" "$sh"
  rerun=$(led --kind run.end --shift "$sh" --task "$TASK" --count)
  local rec vms
  rec=$(led --kind run.end --shift "$sh" --task "$TASK" --last)
  outcome=$(printf '%s' "$rec" | fld outcome)
  vms=$(printf '%s' "$rec" | fld asserts.vms_destroyed)
  # the killed shift left its executor VM running: the resumed run takes the
  # same slot, so the VM the break stranded must be gone by the end
  if [ "$rerun" -ge 1 ] && [ -n "$outcome" ] && [ "$vms" = "True" ]; then
    ok "K $name" "killed in $(printf '%s' "$res" | fld state_at_kill); the resumed shift ran the task again, ended $outcome, VMs destroyed"
  else
    no "K $name" "after the kill in $state: run.end records $rerun, outcome '${outcome:-none}', vms_destroyed '${vms:-null}'"
  fi
}
kill_point mid-call executing 8
kill_point mid-push verifying 0
kill_point mid-judging staging 3

# --------------------------------------------------------------------------
# 6. one session step judged through the arm script, with its trail count
# --------------------------------------------------------------------------
if [ -z "$BENCH" ] || [ ! -x "$BENCH/tools/arm.sh" ]; then
  no "6 session arm" "no bench checkout on this machine (--bench <dir>); the arm script needs bouncer, which lives here"
else
  ARM_SHIFT="$STAMP-smoke-arm"
  ( cd "$BENCH" && DARK_ORG=$WORK_ORG DARK_WORK_ORG=$WORK_ORG DARK_RUNNER_HOST=$RUNNER_HOST \
      tools/arm.sh "$SESSION_ARM" "$TASK" "$ARM_SHIFT" ) >"$OUT/arm.txt" 2>&1
  REC=$(led --kind run.end --shift "$ARM_SHIFT" --task "$TASK" --last)
  printf '%s' "$REC" >"$OUT/arm.json"
  OUTCOME=$(printf '%s' "$REC" | fld outcome); CALLS=$(printf '%s' "$REC" | fld calls)
  TRAIL=$(grep -o 'tool calls [^,]*' "$OUT/arm.txt" | tail -1)
  if [ -n "$OUTCOME" ] && [ -n "$TRAIL" ]; then
    ok "6 session arm" "$OUTCOME through $SESSION_ARM; $TRAIL (record: calls ${CALLS:-NOT MEASURED})"
  else
    no "6 session arm" "outcome '${OUTCOME:-none}', trail line '${TRAIL:-none}'; see $OUT/arm.txt"
  fi
fi

say "== smoke $STAMP: $PASSED passed, $FAILED failed"
[ "$FAILED" = 0 ]
