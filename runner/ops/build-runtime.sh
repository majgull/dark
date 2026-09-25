#!/usr/bin/env bash
# build-runtime — build the runtime archive a session VM unpacks.
#
# The runtime archive is one tar.gz with two directories at its root: node/
# is the node linux-x64 build from nodejs.org with the parts a session never
# reads removed, and pi/ is the pi npm package with its dependencies. A
# session, long, review or user arm downloads it from Gitea and runs
# node/bin/node pi/dist/cli.js (dark/session.py, fetch_runtime); without it
# no such arm can start.
#
#   bash ops/build-runtime.sh [--node <version>] [--pi <version>] [--out <file>] [--print]
#
# --print lists every command it would run and runs none. Everything
# temporary lives under one mktemp -d removed on exit.
set -euo pipefail

NODE_VERSION=26.7.0
PI_VERSION=0.84.2
OUT=runtime.tar.gz
PRINT=0
NODE_ARCH=linux-x64

usage() {
  cat <<'EOF'
usage: build-runtime.sh [--node <version>] [--pi <version>] [--out <file>] [--print]
  --node <version>  node version to build in (default 26.7.0)
  --pi <version>    @earendil-works/pi-coding-agent version (default 0.84.2)
  --out <file>      archive to write (default runtime.tar.gz)
  --print           list the commands it would run, run none
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE_VERSION=${2:?--node needs a version}; shift 2 ;;
    --pi) PI_VERSION=${2:?--pi needs a version}; shift 2 ;;
    --out) OUT=${2:?--out needs a file}; shift 2 ;;
    --print) PRINT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

NODE_BASE="https://nodejs.org/dist/v$NODE_VERSION"
NODE_TARBALL="node-v$NODE_VERSION-$NODE_ARCH.tar.gz"
NODE_URL="$NODE_BASE/$NODE_TARBALL"
SUMS_URL="$NODE_BASE/SHASUMS256.txt"
PI_PKG="@earendil-works/pi-coding-agent@$PI_VERSION"

if [ "$PRINT" = 1 ]; then
  TMP=/tmp/build-runtime.XXXXXXXX
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
fi
DL=$TMP/dl
STAGE=$TMP/stage
PREFIX=$TMP/prefix

run() {
  if [ "$PRINT" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}
say() { printf '== %s\n' "$*"; }

say "node $NODE_VERSION ($NODE_ARCH) from $NODE_URL"
run mkdir -p "$DL" "$STAGE"
run curl -fsSL -o "$DL/$NODE_TARBALL" "$NODE_URL"
run curl -fsSL -o "$DL/SHASUMS256.txt" "$SUMS_URL"

say "checking $NODE_TARBALL against SHASUMS256.txt"
if [ "$PRINT" = 1 ]; then
  printf '+ %s\n' "(cd $DL && sha256sum -c <(grep '  $NODE_TARBALL\$' SHASUMS256.txt))"
else
  ( cd "$DL" && grep -E "  ${NODE_TARBALL}\$" SHASUMS256.txt > check.txt )
  [ "$(wc -l < "$DL/check.txt")" -eq 1 ] || {
    echo "no single SHASUMS256.txt entry for $NODE_TARBALL" >&2; exit 1; }
  ( cd "$DL" && sha256sum -c check.txt ) || {
    echo "sha256 mismatch for $NODE_TARBALL: refusing to build" >&2; exit 1; }
fi

say "unpacking node and trimming it"
run tar -xzf "$DL/$NODE_TARBALL" -C "$STAGE"
run mv "$STAGE/node-v$NODE_VERSION-$NODE_ARCH" "$STAGE/node"
run rm -rf "$STAGE/node/include" "$STAGE/node/share" \
           "$STAGE/node/lib/node_modules/npm/docs"

say "installing $PI_PKG with the node just unpacked"
run env "PATH=$STAGE/node/bin:/usr/bin:/bin" "npm_config_cache=$TMP/npm-cache" \
    npm install --omit=dev --global --prefix "$PREFIX" "$PI_PKG"
run mv "$PREFIX/lib/node_modules/@earendil-works/pi-coding-agent" "$STAGE/pi"
run rm -rf "$STAGE/pi/docs" "$STAGE/pi/examples" "$PREFIX"

say "writing $OUT"
run tar -czf "$OUT" -C "$STAGE" .

say "checking $OUT: unpack it and run the cli"
run rm -rf "$TMP/check"
run mkdir -p "$TMP/check"
run tar -xzf "$OUT" -C "$TMP/check"
run "$TMP/check/node/bin/node" "$TMP/check/pi/dist/cli.js" --version

if [ "$PRINT" = 1 ]; then
  say "nothing was run (--print)"
else
  say "DONE: $OUT"
fi
