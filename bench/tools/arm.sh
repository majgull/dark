#!/usr/bin/env bash
# arm.sh — run one bench task through one SESSION arm (design §7b), from the hub.
#
#   tools/arm.sh <arm> <task-id> [shift-id]
#
# 1. materialize the task repo on the runner host (dark/t-<id>, main = start)
# 2. clone it into a scratch checkout; write the session's brief: the spec,
#    the may-edit grant, "run .factory/verify.sh, commit, do not push"
# 3. one bouncer session (arm's cell/brain/model), waited for, scored from
#    the trail (tool calls, guard denials)
# 4. commit whatever the session left (its own commit, or ours), push it as
#    run/<arm>-<stamp> with the factory-push verb, then stage it on the
#    runner host: hidden acceptance in a fresh VM, one run.end with arm=<arm>
#
# Tokens: recorded only when the brain's transcript exposes them, else NOT
# MEASURED (the runner stores None). Wall seconds and guard denials always.
#
# Resume (L0): before anything else the arm asks the runner whether this
# shift already holds a verdict for this task (`dark done`). A step whose
# branch already passed is not run again; its delivered branch is printed in
# the shape chain.sh reads, so the next step still starts from it.
#
# Wall cap (L0): the session runs under the same cap the pipeline would have
# given the run (`dark envelope`: the envelope's seconds plus the judging
# time), not under no cap at all. A session stopped at the cap is still
# staged, and its record says `capped`.
#
# Tool calls (L0): bouncer writes a pre record when a call starts and a post
# record when it returns, so the number of records is about twice the number
# of calls; the count here is the pre count, and only when every pre has its
# post. On a mismatch the count is NOT MEASURED rather than a number that
# cannot be checked.
set -euo pipefail
ARM=${1:?usage: arm.sh <arm> <task-id> [shift-id]}
TASK=${2:?usage: arm.sh <arm> <task-id> [shift-id]}
SHIFT=${3:-$(date +%Y%m%d-%H%M)-$ARM}
HERE=$(cd "$(dirname "$0")/.." && pwd)
GITEA=${DARK_GITEA:-http://git-host:3400}
ORG=${DARK_ORG:-dark}
RUNNER_HOST=${DARK_RUNNER_HOST:-git-host}
SCRATCH=${DARK_ARM_SCRATCH:-$HERE/.arms}
SLOT=${DARK_ARM_SLOT:-1}   # staging VM slot; two arm loops in parallel need two slots (a shift uses 0)
PREFIX=${DARK_REPO_PREFIX:-t-}   # work repo = <prefix><task>; an arm beside a shift names its own (chain.sh)
mkdir -p "$SCRATCH"
# DARK_WORK_ORG travels with every runner command: an arm run against a
# separate work organisation (the smoke suite) must materialize and stage
# in the same one it clones from
on_runner() { ssh -o BatchMode=yes "$RUNNER_HOST" "cd ~/dark-runner && ${DARK_WORK_ORG:+DARK_WORK_ORG=$DARK_WORK_ORG} $*"; }

read_arm() { python3 - "$HERE/arms.toml" "$ARM" "$1" <<'EOF'
import sys, tomllib
d = tomllib.load(open(sys.argv[1], "rb"))["arm"][sys.argv[2]]
print(d.get(sys.argv[3], ""))
EOF
}
MODE=$(read_arm mode); [ "$MODE" = session ] || { echo "arm $ARM is not a session arm" >&2; exit 2; }
BRAIN=$(read_arm brain); TIER=$(read_arm tier); CELL=$(read_arm cell)
TDIR="${DARK_TASKS:-$HERE}/tasks/$TASK"; [ -f "$TDIR/task.toml" ] || { echo "no task $TASK" >&2; exit 2; }
say() { printf '== arm %s/%s %s\n' "$ARM" "$TASK" "$*"; }

# resume: this shift may already hold a verdict for this step
RESUME=$(on_runner "python3 -m dark done --shift $SHIFT --task $TASK --arm $ARM" 2>/dev/null || true)
if printf '%s' "$RESUME" | grep -q '"resumable": true'; then
  PRIOR=$(printf '%s' "$RESUME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["branch"])')
  say "resumed: this shift already holds a pass for it; pushed $PRIOR"
  printf '%s\n' "$RESUME"
  exit 0
fi

# the wall cap the pipeline would have given this run
WALL=$(on_runner "python3 -m dark envelope --task $TASK --tier $TIER" 2>/dev/null \
       | python3 -c 'import json,sys
try:
    print(int(json.load(sys.stdin)["wall_seconds"]))
except Exception:
    print(0)' 2>/dev/null || echo 0)
WALL=${DARK_ARM_WALL:-$WALL}
spec_field() { python3 - "$TDIR/task.toml" "$1" <<'EOF'
import sys, tomllib
v = tomllib.load(open(sys.argv[1], "rb")).get(sys.argv[2], "")
print(", ".join(v) if isinstance(v, list) else v)
EOF
}
SPEC=$(spec_field spec); GRANT=$(spec_field may_edit); LANG_=$(spec_field lang); AFTER=$(spec_field after)
STAMP=$(date +%Y%m%d-%H%M%S)
BRANCH="run/$ARM-$STAMP"
NAME="bench-$TASK-$ARM-$STAMP"

# chains (decision 17/18): a follow-up starts from the branch this arm
# delivered for the step before it (DARK_AFTER_BRANCH, set by chain.sh when
# that step passed), else from that step's oracle tree
BASE_ARGS=(); BASE_KIND=""
if [ -n "$AFTER" ]; then
  if [ -n "${DARK_AFTER_BRANCH:-}" ]; then BASE_ARGS=(--after-branch "$DARK_AFTER_BRANCH"); BASE_KIND=delivered; else BASE_KIND=oracle; fi
  say "follows $AFTER, starts from its $BASE_KIND tree ${DARK_AFTER_BRANCH:-}"
fi
say "materialize on $RUNNER_HOST"
on_runner "DARK_REPO_PREFIX=$PREFIX python3 -m dark materialize --tasks $TASK ${BASE_ARGS[*]}" | tail -1

WORK="$SCRATCH/$NAME"
rm -rf "$WORK"
git clone -q "$GITEA/$ORG/$PREFIX$TASK.git" "$WORK"
cat > "$WORK/.dark-brief.md" <<EOF
You are working alone in the git checkout at $WORK (a $LANG_ project). Implement the task below end to end. The gate is \`bash .factory/verify.sh\` at the repo root: it must be green when you finish. Commit your work with git when done (any message). Do NOT push, do NOT create branches, do NOT edit .factory/verify.sh or anything under .gitea/, and do not touch files outside this checkout.

Existing files you may rewrite: ${GRANT:-none (add new files only)}. Any other existing file must stay as it is.

TASK:
$SPEC
EOF
say "session $NAME ($CELL/$BRAIN/$TIER), wall cap ${WALL}s"
T0=$(date +%s)
CAPPED=0
cap() {  # run "$@" under what is left of the wall cap; 124 = the cap stopped it
  if [ "${WALL:-0}" -gt 0 ]; then
    LEFT=$(( WALL - ($(date +%s) - T0) )); [ "$LEFT" -lt 5 ] && LEFT=5
    timeout "${LEFT}s" "$@"
  else
    "$@"
  fi
}
# the cell mounts the current directory into the container: run from the checkout
RC=0
( cd "$WORK" && cap bouncer "$CELL" "$BRAIN" -m "$TIER" --name "$NAME" --title "bench $TASK via $ARM" --in-place -p "$(cat "$WORK/.dark-brief.md")" >/dev/null 2>&1 ) || RC=$?
[ "$RC" = 124 ] && CAPPED=1
RC=0
cap bouncer session wait "$NAME" >/dev/null 2>&1 || RC=$?
[ "$RC" = 124 ] && CAPPED=1
if [ "$CAPPED" = 1 ]; then
  say "wall cap of ${WALL}s reached; killing the session and staging what it left"
  bouncer session kill "$NAME" >/dev/null 2>&1 || true
fi
T1=$(date +%s)
SECONDS_=$((T1 - T0))
TRAIL=$(bouncer audit --session "$NAME" --tail 100000 2>/dev/null || true)
DENIALS=$(printf '%s\n' "$TRAIL" | grep -c ' deny ' || true)
# bouncer's own footer: "calls: <n> pre, <m> post, <k> pre-without-post".
# A record is written when a call is admitted and another when it returns,
# so `pre` counts calls the model asked for and `post` counts calls that
# ran. A refused call never runs and so never returns: pre minus post is
# the refusals, and the number of tool calls is the post count. Only when
# the gap is larger than the refusals is anything actually missing.
PAIRS=$(printf '%s\n' "$TRAIL" | grep -oE '^calls: [0-9]+ pre, [0-9]+ post' | tail -1)
PRE=$(printf '%s' "$PAIRS" | grep -oE '[0-9]+ pre' | grep -oE '[0-9]+' || true)
POST=$(printf '%s' "$PAIRS" | grep -oE '[0-9]+ post' | grep -oE '[0-9]+' || true)
CALL_ARGS=()
if [ -z "$PRE" ] || [ -z "$POST" ]; then
  CALLS="NOT MEASURED (${PRE:-?} pre, ${POST:-?} post, no footer)"
elif [ "$PRE" = "$POST" ]; then
  CALLS="$POST"; CALL_ARGS=(--calls "$POST")
elif [ "$((PRE - POST))" = "$DENIALS" ]; then
  CALLS="$POST (+$DENIALS refused)"; CALL_ARGS=(--calls "$POST")
else
  CALLS="NOT MEASURED ($PRE pre, $POST post, $DENIALS refused: the gap is not the refusals)"
fi
say "session done in ${SECONDS_}s, tool calls $CALLS, denials $DENIALS, capped $CAPPED"

cd "$WORK"
rm -f .dark-brief.md
git add -A
git -c user.name="bench-$ARM" -c user.email="bench@hub" commit -qm "bench: $TASK via $ARM ($NAME)" || true
git switch -qc "$BRANCH" 2>/dev/null || git switch -q "$BRANCH"
factory-push "$WORK" "HEAD:$BRANCH" 2>&1 | tail -1
say "pushed $BRANCH; staging on $RUNNER_HOST"
on_runner "DARK_REPO_PREFIX=$PREFIX python3 -m dark stage --task $TASK --branch $BRANCH --arm $ARM --tier $TIER --shift $SHIFT --slot $SLOT --seconds $SECONDS_ ${CALL_ARGS[*]} ${BASE_KIND:+--base $BASE_KIND} $([ "$CAPPED" = 1 ] && echo --capped)" || true
