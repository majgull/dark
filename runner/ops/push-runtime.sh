#!/usr/bin/env bash
# push-runtime — commit the runtime archive to <org>/vm-runtime in Gitea.
#
# The runtime archive is the tar.gz ops/build-runtime.sh writes: node and pi
# at its root, which a session VM downloads and unpacks before pi starts
# (dark/session.py, fetch_runtime). Putting it in a repository on the service
# host is what makes it one hop from the sandbox.
#
#   bash ops/push-runtime.sh <gitea-url> <org> <file>
#
# <gitea-url> is the base URL of the Gitea instance (http://gitea:3000 from
# inside the runner container); <org> is the organisation to hold the
# repository; <file> is the archive ops/build-runtime.sh wrote. The archive
# is committed as runtime.tar.gz, the name the runtime URL ends in. The admin
# token is read from DARK_ADMIN_TOKEN_FILE (default ~/.dark/dark-admin.token)
# and never printed. Re-runnable: the repository is created when absent, and
# a run with an unchanged archive commits nothing.
set -euo pipefail

URL=${1:?usage: push-runtime.sh <gitea-url> <org> <file>}
ORG=${2:?usage: push-runtime.sh <gitea-url> <org> <file>}
FILE=${3:?usage: push-runtime.sh <gitea-url> <org> <file>}
REPO=vm-runtime
ARCHIVE_NAME=runtime.tar.gz
admin_auth_file=${DARK_ADMIN_TOKEN_FILE:-$HOME/.dark/dark-admin.token}

[ -s "$FILE" ] || { echo "no archive at $FILE" >&2; exit 1; }
[ -s "$admin_auth_file" ] || { echo "no admin token at $admin_auth_file" >&2; exit 1; }
admin_auth=$(head -1 "$admin_auth_file")
[ -n "$admin_auth" ] || { echo "empty admin token at $admin_auth_file" >&2; exit 1; }

URL=${URL%/}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
OUT=$tmp/out

say() { printf '== %s\n' "$*"; }
api() { # api METHOD PATH [JSON] -> prints the HTTP code; body in $OUT
  local m=$1 p=$2 d=${3:-}
  curl -sS -o "$OUT" -w '%{http_code}' -X "$m" \
       -H "Authorization: token $admin_auth" \
       -H "Content-Type: application/json" ${d:+-d "$d"} "$URL/api/v1$p"
}

code=$(api GET /user)
[ "$code" = 200 ] || { echo "admin token refused by $URL ($code)" >&2; exit 1; }
admin_user=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["login"])' "$OUT")
[ -n "$admin_user" ] || { echo "Gitea reported no login for the token" >&2; exit 1; }
say "gitea $URL as $admin_user"

code=$(api GET "/repos/$ORG/$REPO")
if [ "$code" = 200 ]; then
  say "repo $ORG/$REPO exists"
elif [ "$code" = 404 ]; then
  code=$(api POST "/orgs/$ORG/repos" "{\"name\":\"$REPO\",\"default_branch\":\"main\",\"auto_init\":false,\"description\":\"dark: the session runtime archive (node + pi)\"}")
  [ "$code" = 201 ] || { echo "repo create failed ($code): $(cat "$OUT")" >&2; exit 1; }
  say "repo $ORG/$REPO created"
else
  echo "could not read $ORG/$REPO ($code): $(cat "$OUT")" >&2
  exit 1
fi

# the versions as the archive itself reports them, so the README describes
# what is in the file and not what the builder was asked for
tar -xzf "$FILE" -C "$tmp"
node_version=$("$tmp/node/bin/node" --version)
pi_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$tmp/pi/package.json")

cat > "$tmp/README.md" <<EOF
# vm-runtime

The session runtime archive: the tar.gz a session VM fetches and unpacks
before pi starts, and which the dark runner points at by default. It holds
the sandbox's node and pi, and nothing else.

- node/ — node $node_version for linux-x64 from nodejs.org, with include/,
  share/ and npm's docs/ removed.
- pi/ — the npm package @earendil-works/pi-coding-agent $pi_version with its
  dependencies under pi/node_modules, and its own docs/ and examples/
  removed.

A session VM downloads it from this repository at

    /api/v1/repos/$ORG/$REPO/media/$ARCHIVE_NAME?ref=main

and runs node/bin/node pi/dist/cli.js (runner/dark/session.py,
fetch_runtime). Built with:

    bash runner/ops/build-runtime.sh

Committed by runner/ops/push-runtime.sh as $ARCHIVE_NAME.
EOF

say "committing $ARCHIVE_NAME ($node_version, pi $pi_version)"
# basic auth in a config header, not in the URL: a URL with credentials
# leaks into git's error output, and the token must never be printed
export GIT_TERMINAL_PROMPT=0
git -c "http.extraheader=Authorization: Basic $(printf '%s:%s' "$admin_user" "$admin_auth" | base64 -w0)" \
    clone -q "$URL/$ORG/$REPO.git" "$tmp/repo"
cd "$tmp/repo"
cp "$FILE" "$ARCHIVE_NAME"
cp "$tmp/README.md" README.md
git add "$ARCHIVE_NAME" README.md
if git diff --cached --quiet; then
  say "archive and README unchanged; nothing to commit"
else
  git -c user.name=dark-bootstrap -c user.email=dark-bootstrap@localhost \
      commit -q -m "runtime: node $node_version + pi $pi_version"
  git -c "http.extraheader=Authorization: Basic $(printf '%s:%s' "$admin_user" "$admin_auth" | base64 -w0)" \
      push -q origin HEAD:main
  say "pushed $(git rev-parse --short HEAD) to $ORG/$REPO main"
fi
say "DONE: $URL/$ORG/$REPO"
