# The docker deployment

`compose.yaml` runs the runner without Proxmox. Gitea, the runner and an
optional local model are services on one internal network; each sandbox, the
throwaway container a run executes or judges in, is created as a sibling
container on that same network through the docker socket the runner mounts.
The pipeline is the same: a branch is cloned into a fresh sandbox, the hidden
acceptance runs there, and the outcome lands in the same append-only ledger.

## What it needs

Docker with the compose plugin. Everything else is built from this checkout:
the runner image from `runner/Dockerfile`, and the sandbox image from
`runner/sandbox` (the bootstrap builds it).

## Steps

From the repository root:

```
docker compose -p dark up -d gitea runner        # build the runner image and start Gitea and the runner
bash runner/ops/docker-bootstrap.sh dark         # users, tokens, orgs, repos, sandbox image (once)
docker compose -p dark exec runner dark check-config
docker compose -p dark exec runner dark preflight --no-model
```

`-p dark` is the compose project name; the bootstrap takes the same one.
Gitea answers on `http://localhost:3410`.

`preflight` refuses when a model id in `models.toml` is not served by its
provider. `--no-model` skips that check and the wake, for a deployment with
no model endpoint configured. It is never implied; a run that needs a model
must pass the real check.

The runner image bakes this checkout in, so rebuild it after editing code or
the toml files: `docker compose -p dark build runner`.

### The two verdicts, with no model

`dark stage` judges a branch that already exists in a task's work repo with
the hidden acceptance in a fresh sandbox. First create the work repo with its
starting tree on `main`, then push the reference solution as a branch the way
a run would:

```
docker compose -p dark exec runner dark materialize --tasks hello-python

docker compose -p dark exec runner sh -c '
  set -e
  tok=$(cat /root/.dark/dark-admin.token)
  git clone -q "http://dark-admin:$tok@gitea:3000/dark-runs/t-hello-python.git" /tmp/oracle
  cp /root/dark-runner/bench/tasks/hello-python/oracle/hello.py \
     /root/dark-runner/bench/tasks/hello-python/oracle/test_hello.py /tmp/oracle/
  cd /tmp/oracle
  git -c user.name=dark-arm -c user.email=dark-arm@localhost add -A
  git -c user.name=dark-arm -c user.email=dark-arm@localhost commit -q -m "hello-python: the reference solution"
  git push -q origin HEAD:oracle
  cd /; rm -rf /tmp/oracle
'
```

Judge the reference solution, then the starting tree:

```
docker compose -p dark exec runner dark stage --task hello-python \
  --branch oracle --arm oracle-ref --tier example-small --shift kc-stage
docker compose -p dark exec runner dark stage --task hello-python \
  --branch main --arm oracle-start --tier example-small --shift kc-stage
```

The first prints `{"run": "hello-python-oracle-ref-...", "outcome": "pass",
"checks": "4/4", ...}` and exits 0. The second prints
`{"run": "hello-python-oracle-start-...", "outcome": "fail:capability",
"checks": "0/4", ...}` and exits 1. `--tier` is recorded on the run; a staging
run spawns no executor, so any id from `models.toml` is enough.

Each run wrote one `run.end` line to the ledger in the runner's state volume:

```
docker compose -p dark exec runner sh -c 'grep run.end /root/.dark/ledger.jsonl'
```

Both lines say `"vms_destroyed": true` in `asserts`, and no sandbox is left
behind:

```
docker ps -a --filter label=dark.vmid
```

### With a model

Point the local provider at the ollama service and set the model ids to ones
it serves. `models.toml` is the file a human edits for this:

```
[provider.local]
url = "http://localhost:11434/v1"     # change to http://ollama:11434/v1
```

```
docker compose -p dark --profile model up -d ollama
docker compose -p dark exec ollama ollama pull <id>
docker compose -p dark exec runner dark preflight
docker compose -p dark exec runner dark shift --tasks hello-python --tier <id>
```

The model is a service on the internal network, reached by name, which is the
one hop a sandbox is allowed. `wake` stays off: the docker plane does not
sleep, so the keep-awake lease is a logged no-op.

## What containers keep, weaken and lose against VMs

| Property | Verdict |
|---|---|
| A fresh disk per run, discarded at the end | Kept: a fresh writable layer per container, removed with force afterwards |
| A second fresh sandbox for staging | Kept: a second container |
| Egress denied, the service host only | Kept: the internal network has no route off the host; Gitea and the model are reachable by name |
| DNS and DHCP inside the sandbox | Kept, mechanism changed: docker's own DNS, no DHCP |
| Ingress denied | Weakened: any container on the network can open a port to another; a Proxmox firewall dropped inbound |
| Kernel isolation from the runner host | Weakened, the largest difference: the container shares the host kernel, so an escape is a host compromise, not a guest one |
| Root inside the sandbox is not the runner host's root | Weakened: container root is bounded by namespaces and dropped capabilities, but it is not a hypervisor boundary |
| Resource limits | Lost unless the backend sets them: the docker backend passes `--cpus`, `--memory` and `--pids-limit` from `host.toml` |
| Snapshot and rollback | Unchanged: the code never asks for one |
| Keep-awake lease and wake | Lost, harmless: there is no sleeping compute plane |
| Energy metering | Weakened: it needs a readable sensor and reports null, never a guess, when there is none |
| VM id as identity | Changed: the container name is the identity; no MAC is needed |
| Address discovery | Changed: `docker inspect` instead of a guest agent |
| The executor holds only the agent token, never the acceptance | Kept |
| Verdict nonce and post-spawn timestamps | Kept |
| The runner reaches the compute plane | Equivalent privilege, different shape: the runner container holds the docker socket, which is host-equivalent. The sandboxes never get the socket |

## Cleanup

```
docker compose -p dark down -v
```

This removes the containers, the two networks and the named volumes (Gitea
data, the runner state with tokens and ledger, the pulled models).
