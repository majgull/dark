#!/usr/bin/env bash
# shift.sh — launch a shift detached on the deployment host with an
# unbuffered log (a redirected `python3` block-buffers stdout, so the log
# stays empty until the shift ends). Prints the log path.
#   bash ops/shift.sh [dark shift args...]      e.g. --arm dark-cloud --tier deepseek-v4-flash:cloud
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p "$HOME/.dark/logs"
log="$HOME/.dark/logs/shift-$(date +%Y%m%d-%H%M%S).log"
nohup python3 -u -m dark shift "$@" >"$log" 2>&1 &
echo "$log"
