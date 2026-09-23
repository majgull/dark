#!/usr/bin/env bash
# deploy — put the runner, templates and bench checkouts on the runner host
# and check the config. Idempotent: clone if missing, fast-forward if present.
# Run ON the runner host:  bash ops/deploy.sh [gitea-url]
set -euo pipefail
URL=${1:-http://localhost:3400}
ORG=${DARK_ORG:-dark}
say() { printf '== %s\n' "$*"; }
sync() { # sync <repo> <dir>
  local repo=$1 dir=$2
  if [ -d "$dir/.git" ]; then
    git -C "$dir" pull --ff-only -q && say "$dir at $(git -C "$dir" rev-parse --short HEAD)"
  else
    git clone -q "$URL/$ORG/$repo.git" "$dir" && say "$dir cloned at $(git -C "$dir" rev-parse --short HEAD)"
  fi
}
sync runner "$HOME/dark-runner"
sync templates "$HOME/dark-templates"
sync bench "$HOME/dark-bench"
mkdir -p "$HOME/.dark/abort" "$HOME/.dark/scratch"
cd "$HOME/dark-runner" && python3 -m dark check-config
