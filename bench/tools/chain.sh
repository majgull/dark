#!/usr/bin/env bash
# chain.sh — run a chain of follow-up steps through one session arm.
#
#   tools/chain.sh <arm> <task-id> [<task-id> ...]
#   DARK_CHAIN_SHIFT=<shift id> tools/chain.sh <arm> <task-id>...   # resume
#
# One arm.sh run per step, in the order given. A step whose stage passed
# hands its pushed branch to the next step (DARK_AFTER_BRANCH: the runner
# materialises the next repo from it, git log included); a step that failed
# hands nothing, and the next step starts from the failed step's oracle
# tree (a failure does not stop the chain; every step is
# measured). The session's continuity is the repository, never a
# conversation: each step is a fresh session in the checkout.
#
# Resume: the chain is identified by its shift id, printed at the start
# and again at the end. Relaunching with DARK_CHAIN_SHIFT set to that id
# continues the same chain: arm.sh asks the runner for each step's verdict
# first and skips a step whose branch already passed, handing its delivered
# branch to the next step exactly as a fresh pass would.
set -uo pipefail
ARM=${1:?usage: chain.sh <arm> <task-id>...}
shift
[ $# -ge 1 ] || { echo "usage: chain.sh <arm> <task-id>..." >&2; exit 2; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
SHIFT=${DARK_CHAIN_SHIFT:-$(date +%Y%m%d-%H%M)-$ARM-chain}
# the arm's own work repos (dark/<arm>-<task>): a chain beside a shift or
# beside another arm must not force-push the shared dark/t-<task> main
export DARK_REPO_PREFIX=${DARK_REPO_PREFIX:-$ARM-}
LOG=${DARK_CHAIN_LOG:-$HERE/.arms/chain-$SHIFT.log}
mkdir -p "$(dirname "$LOG")"
PREV=""
echo "== chain $SHIFT: $ARM over $*" | tee -a "$LOG"
echo "== chain: to continue this chain after a break, relaunch with" \
     "DARK_CHAIN_SHIFT=$SHIFT DARK_REPO_PREFIX=$DARK_REPO_PREFIX tools/chain.sh $ARM $*" | tee -a "$LOG"
for TASK in "$@"; do
  OUT=$(DARK_AFTER_BRANCH="$PREV" "$HERE/tools/arm.sh" "$ARM" "$TASK" "$SHIFT" 2>&1)
  printf '%s\n' "$OUT" | tee -a "$LOG"
  BRANCH=$(printf '%s\n' "$OUT" | grep -o 'pushed run/[^;]*' | head -1 | cut -d' ' -f2)
  if printf '%s\n' "$OUT" | grep -q '"outcome": "pass"'; then
    PREV=$BRANCH
    echo "== chain: $TASK passed; next step starts from $BRANCH" | tee -a "$LOG"
  else
    PREV=""
    echo "== chain: $TASK did not pass; next step starts from its oracle tree" | tee -a "$LOG"
  fi
done
echo "== chain $SHIFT done" | tee -a "$LOG"
