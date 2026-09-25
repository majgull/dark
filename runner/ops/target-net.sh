#!/usr/bin/env bash
# target-net — limit what a user-arm sandbox can reach on the docker backend.
#
# A user-arm sandbox joins a second, non-internal docker network so it can
# reach the application under check. A non-internal network reaches everything
# the host can, so the network's subnet is fenced off in the DOCKER-USER
# chain: one DROP for the whole subnet and one ACCEPT per named host:port,
# inserted above that DROP.
#
#   target-net.sh apply <network> <subnet> <host:port>...
#   target-net.sh show <network> <subnet>
#   target-net.sh remove <network> <subnet>
#
# apply creates the bridge network when it is absent and refuses when it
# already exists with a different subnet, then makes the chain hold exactly
# one DROP for the subnet and one ACCEPT per host:port above it. Running it
# twice changes nothing; an ACCEPT for a host:port no longer named is taken
# out. show prints the network and the chain lines for the subnet. remove
# deletes those rules and the network.
#
# Every argument is validated before anything is touched. iptables runs
# through sudo unless this is root. The docker and iptables calls sit behind
# dkr() and ipt(), so a test can replace them.
set -euo pipefail

NETWORK=""
SUBNET=""
DESIRED=()   # canonical rule specs the chain must hold, in order
CUR=()       # canonical specs of the rules this script manages there now

dkr() { docker "$@"; }
ipt() {
  if [ "$EUID" -eq 0 ]; then
    iptables "$@"
  else
    sudo iptables "$@"
  fi
}

die() { printf 'target-net: %s\n' "$*" >&2; exit 1; }

usage() {
  printf 'usage: target-net.sh apply <network> <subnet> <host:port>...\n' >&2
  printf '       target-net.sh show <network> <subnet>\n' >&2
  printf '       target-net.sh remove <network> <subnet>\n' >&2
}

# --- validation, all of it before the first CLI call -----------------------

valid_network() {
  local name=$1
  if [ -z "$name" ] || [ "${#name}" -gt 64 ]; then return 1; fi
  case "$name" in
    *[!A-Za-z0-9_.-]*) return 1 ;;
  esac
  case "$name" in
    [!A-Za-z0-9]*) return 1 ;;
  esac
  return 0
}

valid_ipv4() {
  local ip=$1 o
  local -a parts=()
  local IFS=.
  read -r -a parts <<< "$ip"
  if [ "${#parts[@]}" -ne 4 ]; then return 1; fi
  for o in "${parts[@]}"; do
    case "$o" in
      ''|*[!0-9]*) return 1 ;;
    esac
    if [ "${#o}" -gt 3 ]; then return 1; fi
    if [ "$((10#$o))" -gt 255 ]; then return 1; fi
  done
  return 0
}

valid_cidr() {
  local cidr=$1 ip prefix
  case "$cidr" in
    */*) ;;
    *) return 1 ;;
  esac
  ip=${cidr%/*}
  prefix=${cidr##*/}
  valid_ipv4 "$ip" || return 1
  case "$prefix" in
    ''|*[!0-9]*) return 1 ;;
  esac
  if [ "${#prefix}" -gt 2 ]; then return 1; fi
  prefix=$((10#$prefix))
  if [ "$prefix" -lt 1 ] || [ "$prefix" -gt 32 ]; then return 1; fi
  return 0
}

valid_hostport() {
  local hp=$1 host port
  case "$hp" in
    *:*) ;;
    *) return 1 ;;
  esac
  host=${hp%:*}
  port=${hp##*:}
  valid_ipv4 "$host" || return 1
  case "$port" in
    ''|*[!0-9]*) return 1 ;;
  esac
  if [ "${#port}" -gt 5 ]; then return 1; fi
  port=$((10#$port))
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then return 1; fi
  return 0
}

# --- reading the chain -----------------------------------------------------

# rule_src <line> — the source of a DOCKER-USER line, or nothing.
rule_src() {
  local -a a=()
  read -r -a a <<< "$1"
  if [ "${a[0]:-}" != "-A" ]; then return 0; fi
  local i=2 n=${#a[@]} tok
  while [ "$i" -lt "$n" ]; do
    tok=${a[$i]}
    case "$tok" in
      -m) i=$((i+2)) ;;
      -s|--source)
        printf '%s\n' "${a[$((i+1))]:-}"
        return 0 ;;
      -d|--destination|-p|--protocol|--dport|-j|--jump) i=$((i+2)) ;;
      *) return 0 ;;
    esac
  done
  return 0
}

# rule_spec <line> — the canonical spec of a line this script manages, or
# nothing when the line is not one of its own. A foreign rule is read, never
# rewritten.
rule_spec() {
  local src="" dst="" proto="" dport="" jump="" i=2 tok
  local -a a=()
  read -r -a a <<< "$1"
  if [ "${a[0]:-}" != "-A" ]; then return 0; fi
  local n=${#a[@]}
  while [ "$i" -lt "$n" ]; do
    tok=${a[$i]}
    case "$tok" in
      -m) i=$((i+2)) ;;
      -s|--source) src=${a[$((i+1))]:-}; i=$((i+2)) ;;
      -d|--destination) dst=${a[$((i+1))]:-}; i=$((i+2)) ;;
      -p|--protocol) proto=${a[$((i+1))]:-}; i=$((i+2)) ;;
      --dport) dport=${a[$((i+1))]:-}; i=$((i+2)) ;;
      -j|--jump) jump=${a[$((i+1))]:-}; i=$((i+2)) ;;
      *) return 0 ;;
    esac
  done
  if [ "$src" != "$SUBNET" ]; then return 0; fi
  if [ "$jump" = DROP ] && [ -z "$dst" ] && [ -z "$proto" ] && [ -z "$dport" ]; then
    printf -- '-s %s -j DROP\n' "$src"
  elif [ "$jump" = ACCEPT ] && [ -n "$dst" ] && [ "$proto" = tcp ] && [ -n "$dport" ]; then
    printf -- '-s %s -d %s -p tcp --dport %s -j ACCEPT\n' "$src" "$dst" "$dport"
  fi
  return 0
}

# read_chain — fill CUR with the canonical specs of this script's rules for
# SUBNET, in chain order. Refuses when there is no DOCKER-USER chain.
read_chain() {
  local out="" line="" spec=""
  out=$(ipt -S DOCKER-USER 2>/dev/null) || die "no DOCKER-USER chain: is docker running?"
  CUR=()
  while IFS= read -r line; do
    spec=$(rule_spec "$line")
    if [ -n "$spec" ]; then
      CUR+=("$spec")
    fi
  done <<< "$out"
}

# --- writing the chain -----------------------------------------------------

build_desired() {
  DESIRED=()
  local hp="" host="" port=""
  for hp in "$@"; do
    host=${hp%:*}
    port=${hp##*:}
    DESIRED+=("-s $SUBNET -d $host/32 -p tcp --dport $port -j ACCEPT")
  done
  DESIRED+=("-s $SUBNET -j DROP")
}

# reconcile <current-spec>... — leave foreign rules alone, leave the rules
# already correct alone, and otherwise replace this script's whole block for
# SUBNET with DESIRED. The block goes above everything else, ACCEPTs in
# argument order above the DROP.
reconcile() {
  local -a cur=("$@")
  local n=${#cur[@]} m=${#DESIRED[@]} i=0 j=0
  while [ "$i" -lt "$n" ] && [ "$i" -lt "$m" ] && [ "${cur[$i]}" = "${DESIRED[$i]}" ]; do
    i=$((i+1))
  done
  if [ "$i" -eq "$n" ] && [ "$i" -eq "$m" ]; then return 0; fi
  local -a argv=()
  for (( j=0; j<n; j++ )); do
    read -r -a argv <<< "${cur[$j]}"
    ipt -D DOCKER-USER "${argv[@]}"
  done
  for (( j=m-1; j>=0; j-- )); do
    read -r -a argv <<< "${DESIRED[$j]}"
    ipt -I DOCKER-USER 1 "${argv[@]}"
  done
}

# --- the network -----------------------------------------------------------

# ensure_network — the bridge network exists with SUBNET, created if absent,
# refused when it exists with another subnet.
ensure_network() {
  local out="" subnet="" found=0
  if out=$(dkr network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$NETWORK" 2>/dev/null); then
    for subnet in $out; do
      if [ "$subnet" = "$SUBNET" ]; then found=1; fi
    done
    if [ "$found" -ne 1 ]; then
      die "network $NETWORK exists with subnet '${out:-none}', not $SUBNET"
    fi
  else
    dkr network create --driver bridge --subnet "$SUBNET" "$NETWORK" >/dev/null
  fi
}

main() {
  local cmd=${1:-}
  case "$cmd" in
    -h|--help|help) usage; exit 0 ;;
    apply|show|remove) ;;
    *) usage; exit 2 ;;
  esac
  shift

  local network=${1:-} subnet=${2:-}
  if [ -z "$network" ] || [ -z "$subnet" ]; then usage; exit 2; fi
  valid_network "$network" || die "bad network name: $network"
  valid_cidr "$subnet" || die "bad subnet (want an IPv4 CIDR like 172.30.41.0/24): $subnet"
  NETWORK=$network
  SUBNET=$subnet
  shift 2

  case "$cmd" in
    show)
      if [ "$#" -ne 0 ]; then die "show takes exactly <network> <subnet>"; fi
      dkr network inspect "$NETWORK" || die "no network $NETWORK"
      local line="" src=""
      printf '== rules for %s on %s\n' "$SUBNET" "$NETWORK"
      local out=""
      out=$(ipt -S DOCKER-USER 2>/dev/null) || die "no DOCKER-USER chain: is docker running?"
      while IFS= read -r line; do
        src=$(rule_src "$line")
        if [ "$src" = "$SUBNET" ]; then printf '%s\n' "$line"; fi
      done <<< "$out"
      ;;
    remove)
      if [ "$#" -ne 0 ]; then die "remove takes exactly <network> <subnet>"; fi
      DESIRED=()
      read_chain
      reconcile "${CUR[@]}"
      if dkr network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$NETWORK" >/dev/null 2>&1; then
        dkr network rm "$NETWORK" >/dev/null
      fi
      ;;
    apply)
      local -a allows=() seen=()
      local hp="" prev="" found=0
      for hp in "$@"; do
        valid_hostport "$hp" || die "bad host:port (want a dotted IPv4 host and a port 1..65535): $hp"
        found=0
        for prev in "${seen[@]}"; do
          if [ "$prev" = "$hp" ]; then found=1; fi
        done
        if [ "$found" -eq 1 ]; then die "duplicate host:port: $hp"; fi
        seen+=("$hp")
        allows+=("$hp")
      done
      build_desired "${allows[@]}"
      ensure_network
      read_chain
      reconcile "${CUR[@]}"
      ;;
  esac
}

main "$@"
