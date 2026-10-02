#!/bin/bash
# The factory's feedback loop for this repo. The agent runs it after each edit;
# CI re-runs it as the merge gate. It refuses a stale generated contract, installs
# exact versions from the lock file when needed, and runs the one check. The
# browser leg is separate (`npm run e2e`) because the agent VM has no browser.
# Agents must NOT edit this file.
set -e
cd "$(dirname "$0")/.."
python3 tools/gen_types.py --check
if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
  npm ci
fi
npm run check
echo "verify OK"
