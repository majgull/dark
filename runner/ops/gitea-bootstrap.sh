#!/usr/bin/env bash
# gitea-bootstrap — birth an org on the factory Gitea. Idempotent.
# Run ON the runner host (it reads the admin token there; the hub never does):
#   bash ops/gitea-bootstrap.sh [gitea-url]
#   bash ops/gitea-bootstrap.sh --work-org <name> [gitea-url]
# Creates: the org, the repos runner/bench/templates (trunk main), and an
# `agents` team with write access to every repo of the org holding the
# executor user, so the agent token can push run branches.
#
# --work-org makes the org a run's work organisation instead: the org and
# the team, no code repos. The team is the part that matters and the part
# that was missed when dark-runs was created by hand: the org existed, and
# every run reached its push and was refused ("User permission denied for
# writing"), which preflight now checks for before a shift starts.
set -euo pipefail
WORK_ONLY=0
if [ "${1:-}" = "--work-org" ]; then WORK_ONLY=1; DARK_ORG=${2:?--work-org needs a name}; shift 2; fi
URL=${1:-http://localhost:3400}
ORG=${DARK_ORG:-dark}
TOK_FILE=${DARK_ADMIN_TOKEN_FILE:-$HOME/.dark/dark-admin.token}
AGENT_USER=${DARK_AGENT_USER:-dark-agent}
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT
tok() { head -1 "$TOK_FILE"; }
api() { # api METHOD PATH [JSON] -> prints the HTTP code; body in $OUT
  local m=$1 p=$2 d=${3:-}
  curl -sS -o "$OUT" -w '%{http_code}' -X "$m" -H "Authorization: token $(tok)" \
       -H "Content-Type: application/json" ${d:+-d "$d"} "$URL/api/v1$p"
}
say() { printf '== %s\n' "$*"; }

[ -s "$TOK_FILE" ] || { echo "no admin token at $TOK_FILE" >&2; exit 1; }
code=$(api GET /user); [ "$code" = 200 ] || { echo "token refused by $URL ($code)" >&2; exit 1; }
say "gitea $URL as $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["login"])' "$OUT")"

code=$(api GET "/orgs/$ORG")
if [ "$code" = 404 ]; then
  code=$(api POST /orgs "{\"username\":\"$ORG\",\"visibility\":\"public\",\"description\":\"factory v2 (hub 949): runner, bench, templates\"}")
  [ "$code" = 201 ] || { echo "org create failed ($code): $(cat "$OUT")" >&2; exit 1; }
  say "org $ORG created"
else
  say "org $ORG exists"
fi

for r in $([ "$WORK_ONLY" = 1 ] && echo || echo runner bench templates); do
  code=$(api GET "/repos/$ORG/$r")
  if [ "$code" = 404 ]; then
    code=$(api POST "/orgs/$ORG/repos" "{\"name\":\"$r\",\"default_branch\":\"main\",\"auto_init\":false,\"description\":\"dark/$r\"}")
    [ "$code" = 201 ] || { echo "repo $r create failed ($code): $(cat "$OUT")" >&2; exit 1; }
    say "repo $ORG/$r created"
  else
    say "repo $ORG/$r exists"
  fi
done

# The executor pushes run branches with the agent token: it needs write on
# every repo of the org, including t-* repos created later -> a team with
# includes_all_repositories.
code=$(api GET "/orgs/$ORG/teams")
team_id=$(python3 -c 'import json,sys; ts=[t for t in json.load(open(sys.argv[1])) if t["name"]=="agents"]; print(ts[0]["id"] if ts else "")' "$OUT")
if [ -z "$team_id" ]; then
  code=$(api POST "/orgs/$ORG/teams" '{"name":"agents","description":"executor VMs (agent token)","permission":"write","includes_all_repositories":true,"can_create_org_repo":false,"units":["repo.code","repo.issues","repo.pulls","repo.releases"]}')
  [ "$code" = 201 ] || { echo "team create failed ($code): $(cat "$OUT")" >&2; exit 1; }
  team_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$OUT")
  say "team agents created ($team_id)"
else
  say "team agents exists ($team_id)"
fi
code=$(api PUT "/teams/$team_id/members/$AGENT_USER")
case "$code" in 204) say "$AGENT_USER is in team agents";; *) echo "adding $AGENT_USER failed ($code): $(cat "$OUT")" >&2; exit 1;; esac
say "bootstrap DONE: $URL/$ORG"
