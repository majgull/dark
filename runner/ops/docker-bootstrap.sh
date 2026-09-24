#!/usr/bin/env bash
# docker-bootstrap: one-time (and re-runnable) setup for the docker
# deployment in compose.yaml. Run it on the host from the repository root,
# after the images exist:
#
#   docker compose -p dark up -d gitea runner
#   bash runner/ops/docker-bootstrap.sh dark
#
# It creates the admin user and the agent user with the Gitea CLI inside the
# container, writes their tokens to the two files dark/config.py reads,
# creates the orgs and repos and the write team through the Gitea API (the
# same work ops/gitea-bootstrap.sh does for a Proxmox deployment), builds the
# sandbox image the runner spawns its containers from, and pushes this
# checkout's templates into the code org. Safe to run twice.
#
# Overridable: DARK_COMPOSE_PROJECT, DARK_GITEA_PORT, DARK_ORG,
# DARK_WORK_ORG, DARK_ADMIN_USER, DARK_AGENT_USER, DARK_SANDBOX_IMAGE.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=${1:-${DARK_COMPOSE_PROJECT:-dark}}
GITEA_PORT=${DARK_GITEA_PORT:-3410}
ORG=${DARK_ORG:-dark}
WORK_ORG=${DARK_WORK_ORG:-dark-runs}
ADMIN_USER=${DARK_ADMIN_USER:-dark-admin}
AGENT_USER=${DARK_AGENT_USER:-dark-agent}
SANDBOX_IMAGE=${DARK_SANDBOX_IMAGE:-dark-sandbox}
GITEA_IN=http://gitea:3000           # from inside the network
STATE=/root/.dark                    # the runner container's state volume

COMPOSE=(docker compose -p "$PROJECT")
say() { printf '== %s\n' "$*"; }
gitea_cli() { "${COMPOSE[@]}" exec -T --user git gitea gitea "$@"; }
runner_sh() { "${COMPOSE[@]}" exec -T runner sh -c "$1"; }

say "project $PROJECT"
"${COMPOSE[@]}" up -d gitea runner

say "waiting for gitea"
ready=0
for _ in $(seq 1 60); do
  if gitea_cli admin user list >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || { echo "gitea did not come up (port $GITEA_PORT on the host)" >&2; exit 1; }
# the CLI answers from the database before the web server listens; the API
# calls below need the web server, so wait for its health endpoint as well
ready=0
for _ in $(seq 1 60); do
  if runner_sh "curl -fsS -o /dev/null $GITEA_IN/api/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || { echo "gitea web server did not answer on $GITEA_IN/api/healthz" >&2; exit 1; }

create_user() { # create_user <name> <password> [--admin]
  local name=$1 password=$2 admin=${3:-} out
  out=$(mktemp)
  if gitea_cli admin user create --username "$name" --password "$password" \
       --email "$name@localhost" --must-change-password=false $admin >"$out" 2>&1; then
    say "user $name created"
  elif grep -qi 'already exists' "$out"; then
    say "user $name exists"
  else
    cat "$out" >&2
    rm -f "$out"
    echo "could not create user $name" >&2
    exit 1
  fi
  rm -f "$out"
}

create_user "$ADMIN_USER" "${DARK_ADMIN_PASSWORD:-dark-admin}" --admin
create_user "$AGENT_USER" "${DARK_AGENT_PASSWORD:-dark-agent}"

write_token() { # write_token <gitea-user> <token-name> <file>
  local user=$1 token_name=$2 file=$3 token
  # --raw prints the token alone. Gitea refuses a token name a user already
  # has, so each run mints a fresh token under a time-stamped name; a re-run
  # after a partial failure then replaces the file instead of stopping here
  token=$(gitea_cli admin user generate-access-token --username "$user" \
            --token-name "$token_name-$(date +%s)" --raw | tr -d '\r' | tail -1)
  [ -n "$token" ] || { echo "no token for $user" >&2; exit 1; }
  printf '%s\n' "$token" | runner_sh "umask 077; mkdir -p $STATE; cat > $file"
  say "token $token_name written to $file"
}

write_token "$ADMIN_USER" dark-admin "$STATE/dark-admin.token"
write_token "$AGENT_USER" dark-agent "$STATE/dark-agent.token"

# the orgs, the code repos and the write team: the same work
# ops/gitea-bootstrap.sh does for a Proxmox deployment, run inside the runner
say "org $ORG"
"${COMPOSE[@]}" exec -T runner bash ops/gitea-bootstrap.sh "$GITEA_IN"
say "work org $WORK_ORG"
"${COMPOSE[@]}" exec -T runner bash ops/gitea-bootstrap.sh --work-org "$WORK_ORG" "$GITEA_IN"

say "sandbox image $SANDBOX_IMAGE"
docker build -q -t "$SANDBOX_IMAGE" runner/sandbox >/dev/null && say "built"

# the templates repo: the starting trees a task is laid over, pushed from the
# copy baked into the runner image so the code org is not empty
say "templates -> $ORG/templates"
"${COMPOSE[@]}" exec -T -e STATE="$STATE" -e ORG="$ORG" runner sh -c '
  set -e
  url="http://dark-admin:$(cat "$STATE"/dark-admin.token)@gitea:3000/$ORG/templates.git"
  tmp=$(mktemp -d)
  cp -a /root/dark-runner/templates/. "$tmp/"
  cd "$tmp"
  git init -q -b main
  git add -A
  git -c user.name=dark-bootstrap -c user.email=dark-bootstrap@localhost commit -q -m "templates: the Go and Python starting trees"
  git push -q -f "$url" main:main
  cd /; rm -rf "$tmp"
'
say "bootstrap DONE: $PROJECT"
