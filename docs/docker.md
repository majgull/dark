# The docker deployment

`compose.yaml` runs the runner without Proxmox. Gitea, the runner, the gates
and an optional local model are services on one internal network; each
sandbox, the throwaway container a run executes or judges in, is created as a
sibling container on that same network through the docker socket the runner
mounts. The pipeline is the same: a branch is cloned into a fresh sandbox, the
hidden acceptance runs there, and the outcome lands in the same append-only
ledger.

## What it needs

Docker with the compose plugin. Everything else is built from this checkout:
the runner image from `runner/Dockerfile`, and the sandbox image from
`runner/sandbox` (the bootstrap builds it).

## Steps

From the repository root:

```
docker compose -p dark up -d gitea runner        # build the runner image and start Gitea, the runner and the gate
bash runner/ops/docker-bootstrap.sh dark         # users, tokens, orgs, repos, sandbox image (once)
DARK_RUNTIME_ARCHIVE=/root/runtime.tar.gz bash runner/ops/docker-bootstrap.sh dark   # optional: the session runtime, see below
docker compose -p dark exec runner dark check-config
docker compose -p dark exec runner dark preflight --no-model
```

`-p dark` is the compose project name; the bootstrap takes the same one.
Gitea answers on `http://localhost:3410`; set `DARK_GITEA_ROOT_URL` to serve
it under a different address, for example behind a reverse proxy. When an OpenID Connect sign-in source is added, its `[oauth2_client]` keys come from `DARK_GITEA_OAUTH_AUTO_REGISTRATION`, `DARK_GITEA_OAUTH_ACCOUNT_LINKING` and `DARK_GITEA_OAUTH_USERNAME`, each defaulting to Gitea's own.

### The session runtime archive

A session, long, review or user run starts pi inside a sandbox, and the
sandbox image does not hold pi. The runtime archive is the tar.gz a session
sandbox downloads from Gitea and unpacks before pi starts: it holds `node/`
and `pi/` at its root, and nothing else. Build one with

```
bash runner/ops/build-runtime.sh
```

copy it into the runner container, and run the bootstrap again with the path
inside the container named, which is what the optional line in the steps
above does:

```
docker compose -p dark cp runtime.tar.gz runner:/root/runtime.tar.gz
DARK_RUNTIME_ARCHIVE=/root/runtime.tar.gz bash runner/ops/docker-bootstrap.sh dark
```

The bootstrap then commits the archive to `<org>/vm-runtime` on `main`
through `runner/ops/push-runtime.sh`, which is the repository the runner's
runtime URL points at by default. Without it, a session, long, review or user
arm has no runtime and every such run fails on the download.

`preflight` refuses when a model id in `models.toml` is not served by its
provider. `--no-model` skips that check and the wake, for a deployment with
no model endpoint configured. It is never implied; a run that needs a model
must pass the real check.

## Where the config lives

`models.toml`, `budgets.toml` and `host.toml` are read from a directory
outside the image. Compose mounts `${DARK_CONF_DIR:-./runner}` read-only at
`/root/dark-conf` in the runner and sets `DARK_CONF=/root/dark-conf`. With
`DARK_CONF_DIR` unset that is this checkout's `runner/` directory, which
holds example values only. Point it at a directory outside the checkout to
keep real endpoints and model ids out of the repository:

```
DARK_CONF_DIR=$HOME/.config/dark docker compose -p dark up -d gitea runner
```

Inside the container, `dark` takes its config directory from `--conf`, else
`$DARK_CONF`, else `./runner` when the working directory is a dark checkout,
else the toml files beside the package. Editing a toml needs a
runner restart (`docker compose -p dark restart runner`), never a rebuild.

### Several task sets

The runner reads its task set from `DARK_TASKS`, one task-set root (a
directory holding `tasks/`) or several joined with `:`. Compose mounts two
host directories, named by `DARK_TASKS_DIRS` and `DARK_TASKS_DIRS_2`, read
only, and lists the ones that are set:

```
DARK_TASKS_DIRS=$HOME/tasks/public DARK_TASKS_DIRS_2=$HOME/tasks/private \
  docker compose -p dark up -d gitea runner
```

Inside the runner `DARK_TASKS` is `/root/dark-tasks/1`, plus
`:/root/dark-tasks/2` when `DARK_TASKS_DIRS_2` is set. A task name in two
task sets is refused, naming both, rather than one silently winning. With
neither variable set, `DARK_TASKS` is `/root/dark-tasks/1`, which compose
mounts from `./bench`, so the bench checkout's two example tasks are the
only task set.

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

## The model gate

A sandbox on `back` reaches Gitea and two gates, and nothing else. A gate
here is a pinned reverse proxy that forwards to one configured upstream: the
model gate to the model endpoint, and the OTLP gate (`otlp-gate`, see
[Traces](#traces)) to a trace collector. A third gate, the claude gate, is on
a network of its own that only a sandbox running Claude Code joins (see
[Claude Code as the executor](#claude-code-as-the-executor)).

The model gate listens on `http://model-gate:11434`
and forwards to `DARK_MODEL_UPSTREAM`. The runner and every sandbox use
`http://model-gate:11434/v1`; they never name a provider directly. The
bundled `ollama` service is on `front` only, so it can pull models and no
sandbox can reach it without the gate.

`DARK_MODEL_UPSTREAM` is a scheme, host and port, `http://ollama:11434` by
default. Caddy does not accept a path in the upstream address, and the
request path is passed through unchanged, so `https://api.example.com` serves
`https://api.example.com/v1/models` when dark asks the gate for
`/v1/models`.

### The bundled ollama

Start it with the model profile and pull one small model:

```
docker compose -p dark --profile model up -d
docker compose -p dark exec ollama ollama pull qwen2.5-coder:1.5b
```

In the config directory, point the provider at the gate and give the tier the
id the endpoint serves:

```
[provider.local]
url = "http://model-gate:11434/v1"
catalog = "models"
window = ""
think_api = "chat_template"
stream = true

[model."qwen2.5-coder:1.5b"]
provider = "local"
cost = "local"
speed = "fast"
watts = "low"
ctx = 32768
```

`budgets.toml` must name the same id: every class's `provisional` list, and
`[admission].cost_order` if the tier's rank (`local/low` here) is not already
in it. The runner refuses a provisional id that is not in `models.toml`.
Then:

```
docker compose -p dark exec runner dark preflight
docker compose -p dark exec runner dark shift --tasks hello-python --tier qwen2.5-coder:1.5b
```

`preflight` passes the model check only when the endpoint serves every id in
`models.toml`, so it is the check that the gate and the pull both work.

## Your own endpoint

The gate is the only model URL a sandbox sees, so the endpoint itself never
enters the checkout. Keep the three toml files in a directory outside the
checkout (for example in your own private config repository), point
`models.toml` at `http://model-gate:11434/v1` as above, and set the model ids
and the `provisional` lists to ids the endpoint serves. Then start compose
with that directory and the endpoint's origin:

```
DARK_CONF_DIR=$HOME/.config/dark \
DARK_MODEL_UPSTREAM=https://api.example.com \
docker compose -p dark --profile model up -d
```

Nothing in the checkout changes; `git status --short` stays clean. The
bundled `ollama` is not started unless the `model` profile is asked for, and
it is never used when `DARK_MODEL_UPSTREAM` names somewhere else.

## Claude Code as the executor

A tier whose id is written `claude:<model-id>` runs [Claude Code](https://code.claude.com/docs), Anthropic's coding-agent CLI, in the sandbox in place of pi. `dark long` takes such a tier for its session (`--tier`), for its judge (`--judge-tier`) or for both. It runs only when you name it, on the docker backend only; the user arm refuses it, because it asks its model through the chat endpoint.

Claude Code speaks to Anthropic's API and needs a token of your Claude account. Neither is handed to a sandbox:

- **The claude gate** (`claude-gate` in `compose.yaml`, behind the profile `claude`) is a third reverse proxy. It forwards to `https://api.anthropic.com` (`DARK_CLAUDE_UPSTREAM`) and replaces the `Authorization` header of every request with `Bearer <token>`.
- **The token** is a file named `claude-oauth-token`, mounted read-only into the claude gate and into nothing else. The runner never reads it, and Claude Code in the sandbox is started with a placeholder in its place, which Anthropic refuses when it arrives unreplaced. So the token cannot end up in a work repository, a record or the ledger: it is never where the model is.
- **The network.** The claude gate is on `claude`, a second internal network. A sandbox whose run has a `claude:` tier joins it besides `back`, before it starts; no other sandbox does, so no other run can spend the token.

Set it up once:

```
claude setup-token                               # on a machine with a browser; prints a token valid for one year
sudo install -d -m 0700 /etc/dark/secrets
sudo sh -c 'umask 077; cat > /etc/dark/secrets/claude-oauth-token'    # paste the token, Enter, Ctrl-D
DARK_CLAUDE_TOKEN_DIR=/etc/dark/secrets docker compose -p dark --profile claude up -d claude-gate
```

With `DARK_CLAUDE_TOKEN_DIR` unset the gate reads the file from the named volume `claude-secrets` instead (`docker run --rm -i -v dark_claude-secrets:/s busybox sh -c 'umask 077; cat > /s/claude-oauth-token'` writes it there, the volume carrying the project name as its prefix). The file is read at every request, so a new token needs no restart; one trailing newline is dropped.

Then give the tier an entry in `models.toml`, with a provider and a window of its own so that its calls are counted apart from your other models. dark itself never calls this provider: its `url` is only a label, and preflight leaves a `claude:` id out of the check that every id is served.

```toml
[provider.claude]
url = "http://claude-gate:8443"
catalog = "models"
window = "claude"
think_api = "none"

[model."claude:claude-sonnet-5"]     # what follows "claude:" is passed to `claude --model`
provider = "claude"
cost = "sub-window"
speed = "fast"
```

and in `budgets.toml` the window, and the tier's rank in the cost order:

```toml
[window.claude]
daily_calls = 2000
daily_tokens = 200000000

[admission]
cost_order = ["local/low", "local/high", "sub-window/fast"]
```

`compose.yaml` already gives the runner the gate's address (`DARK_CLAUDE_GATE_URL`, `claude_gate_url` in `host.toml`) and the network's name (`DARK_CLAUDE_NETWORK`, `claude_network`). A run on such a tier is refused before anything is spawned when the address is empty or the backend is not docker.

```
docker compose -p dark exec runner dark long --task <task-dir> --tier claude:claude-sonnet-5 --judge-tier claude:claude-sonnet-5
```

The run is recorded like any other. Its `run.end` row counts one call per request Claude Code made, the tool calls it ran, and the tokens Claude Code reports at the end; the input count includes what was read from and written to the prompt cache, so it is large. `stream.jsonl` in the records repository is Claude Code's own stream.

What to know before relying on it:

- The gate does not ask who is calling. Whatever is on the `claude` network can spend the token, which is why only the sandboxes that need it are put there, and why the gate publishes no port.
- The calls count against your Claude account's own limits. The window above only counts them; Anthropic's refusal is the real bound, and it reaches the run as its failure detail.
- A sandbox has no route to `platform.claude.com`, where Claude Code exchanges and refreshes a login. The token from `claude setup-token` is valid for one year and needs neither; when it expires or is revoked, runs end with Anthropic's 401 until the file holds a new one.
- Claude Code runs with its permission prompts skipped, which it allows as root only when `IS_SANDBOX=1` is set. The sandbox is the boundary, as it is for pi.
- The version is pinned by sha256 in `runner/sandbox/Dockerfile`; change it there and rebuild the sandbox image.

## The user arm's target network

One arm of a run, the *user arm*, checks an application that is already
running the way a person would, through its pages and its API, instead of
writing code in a checkout. That application lives outside the sandbox, so a
user-arm sandbox joins a second docker network as well as the internal one.
This second network is the *target network*; its docker name is the runner's
`target_network` key (`DARK_TARGET_NETWORK` when set through the environment),
and the runner connects each user-arm sandbox to it after the container is
created.

A docker network marked *internal* has no route off the host, which is what
keeps an ordinary sandbox to Gitea and the model gate. A network that is not
internal has the host's own route, so a sandbox on it reaches every address
the host can: the local network, the host itself, the internet. The target
network must therefore be limited, or the user arm trades its confinement
for reach. The limit is drawn on the *subnet*, the block of addresses the
network hands out, written as an IPv4 CIDR — an address, a slash and the
number of leading bits that name the block, such as `172.30.41.0/24` — in
the `DOCKER-USER` chain: the iptables chain docker reserves for rules an
operator adds and reads before its own. One rule drops every connection
opened from the subnet (*egress* is what the sandbox starts outwards), and
one rule per `host:port` the application is reached on accepts it and sits
above that drop.

`runner/ops/target-net.sh` writes and removes those rules:

```
bash runner/ops/target-net.sh apply  dark_target 172.30.41.0/24 192.0.2.203:631
bash runner/ops/target-net.sh show   dark_target 172.30.41.0/24
bash runner/ops/target-net.sh remove dark_target 172.30.41.0/24
```

`apply` creates the network when it is absent — refusing when it already
exists with a different subnet — then makes the chain hold exactly one drop
for the subnet and exactly one accept per named `host:port` above it.
Running it a second time changes nothing, and an accept for a `host:port` no
longer named is removed. `show` prints the network and the chain lines for
the subnet; `remove` deletes the rules and the network. Every argument is
checked before anything is touched, and iptables runs through `sudo` unless
the command runs as root.

### Keeping it across reboots

Rules typed by hand are gone after a reboot. Install
`runner/ops/dark-target-net.service`, a systemd unit (the file systemd reads
to start a service), which runs the same `apply` once `docker.service` is up
and takes its arguments from `/etc/dark/target-net.env`. Copy the script to
the path the unit calls, write the three variables, then enable the unit:

```
sudo install -m 0755 runner/ops/target-net.sh /usr/local/bin/dark-target-net.sh
sudo install -d /etc/dark
sudo tee /etc/dark/target-net.env >/dev/null <<'EOF'
TARGET_NETWORK=dark_target
TARGET_SUBNET=172.30.41.0/24
TARGET_ALLOW=192.0.2.203:631
EOF
sudo install -m 0644 runner/ops/dark-target-net.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dark-target-net.service
```

`TARGET_ALLOW` is the space-separated list of `host:port` addresses, so two
of them are `TARGET_ALLOW="192.0.2.203:631 192.0.2.204:8080"`. After
editing the file, `sudo systemctl restart dark-target-net.service` applies
the change; the work is idempotent, so a restart is always safe. The unit
creates the network itself; if something else created it first, `apply`
refuses it unless the subnet matches.

## Watching a user-arm run

Every user-arm run records its browser twice, beside `steps.jsonl` and the `steps/` screenshots, in the *records repository*: the Gitea repository `dark-records/<shift>`, which holds one directory per run named `<run id>`; the run's `records` field (for example `dark-records/s1/shop-user-1`) names it. A *Playwright trace* is `trace.zip`: for every action the browser took, the page's DOM as it was, a screencast frame (one still picture of the screen), the console output and the network calls. A *video* is `video.webm`, a screen recording of the whole run at 1280 by 720.

```
<run id>/steps.jsonl   <run id>/steps/01.png   <run id>/trace.zip   <run id>/video.webm
```

Clone the records repository and open the trace on your own machine (it needs Node.js, which provides `npx`):

```
npx playwright show-trace <run id>/trace.zip
```

The same file opens in a browser tab at `https://trace.playwright.dev`, which "loads the trace entirely in your browser and does not transmit any data externally". The video plays in any player that reads WebM, such as `mpv <run id>/video.webm` or a browser.

Either file can be missing: a browser that died before it could save the trace leaves none, and an image built without ffmpeg (the program that encodes the video; `Dockerfile.browser` installs it) leaves no video. `steps.jsonl` is written either way, and a missing recording never changes a verdict. The trace also holds the pages' request headers and response bodies, so treat the records repository as readable by everyone who can open it.

## Traces

A *span* is one timed operation with a name, start and end times and key-value attributes; a *trace* is a span with the spans nested under it; *OTLP/HTTP JSON* is OpenTelemetry's wire format, a JSON body posted to `<endpoint>/v1/traces`; a *collector* is the program that receives traces, such as an OpenTelemetry Collector, Jaeger or Grafana Tempo.

When `otlp_endpoint` is set, every finished run is sent as one trace, right after its `run.end` row is written. The parent span is named `invoke_agent <task>` and carries `gen_ai.operation.name`, `gen_ai.agent.name` (the arm), `gen_ai.request.model` (the tier), `gen_ai.conversation.id` (the run id) and dark's own `dark.outcome`, `dark.calls`, `dark.seconds` and `dark.class`. Each tool call the session made is a child span named `execute_tool <tool>` carrying `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`, the last two cut at 2000 characters. The `gen_ai.*` names are OpenTelemetry's GenAI semantic conventions, its agreed names for the spans and attributes of AI agents. The tool calls are read back from the run's `stream.jsonl` in the records repository, so a run that pushed none, and every pipeline-arm run, is a trace of one span. A user-arm run's rows are one browser action each: every row becomes a child span named `execute_tool browser.<do>` carrying the action and the result JSON and `dark.step`, with no time of its own, so its child spans are spaced evenly across the parent span.

Set it in `runner/host.toml` (`otlp_endpoint = "http://collector:4318"`) or with the variable `DARK_OTLP_ENDPOINT`. The runner's container joins only the internal `back` network, which has no route off the host, so a collector elsewhere is reached through `otlp-gate`, a second reverse proxy in `compose.yaml` beside `model-gate`: set `DARK_OTLP_UPSTREAM` on it to the collector's own address and `otlp_endpoint = "http://otlp-gate:4318"` in `host.toml`. Its default upstream, `http://127.0.0.1:9`, refuses at once, so an unset collector costs a run nothing. The default is empty, and while it is empty nothing is sent and no stream is read back. Tool arguments and results can hold file contents and command output, so point it only at a collector you run.

A collector that is down, slow or refusing is logged in one line and never changes a run; sending gives up after 5 seconds.

## What containers keep, weaken and lose against VMs

The Docker column is this deployment. The LXC column is the `lxc` backend
(`runner/dark/lxc.py`, `backend = "lxc"`), where each sandbox is a full clone
of a real container on the Proxmox host, taken from a named snapshot.

| Property | Docker | LXC |
|---|---|---|
| A fresh disk per run, discarded at the end | Kept: a fresh writable layer per container, removed with force afterwards | Kept: a full clone of the source container's snapshot per run, destroyed with `--purge` afterwards |
| A second fresh sandbox for staging | Kept: a second container | Kept: a second full clone |
| Egress denied, the service host only | Kept: the internal network has no route off the host; Gitea and the gates are reachable by name, and a gate is the only model URL | Kept: the same default-drop firewall file with `GROUP agentfw`, on a bridge that reaches only the service host |
| DNS and DHCP inside the sandbox | Kept, mechanism changed: docker's own DNS, no DHCP | Kept: `ip=dhcp` on the clone's net0; DNS as the source container resolves it |
| Ingress denied | Weakened: any container on the network can open a port to another; a Proxmox firewall dropped inbound | Kept: the firewall file drops inbound |
| Kernel isolation from the runner host | Weakened, the largest difference: the container shares the host kernel, so an escape is a host compromise, not a guest one | Weakened, as with docker: the container shares the Proxmox host's kernel, so an escape is a compromise of that host |
| Root inside the sandbox is not the runner host's root | Weakened: container root is bounded by namespaces and dropped capabilities, but it is not a hypervisor boundary | Inherited from the source: an unprivileged source gives a user-namespaced root; a privileged one gives the Proxmox host's root |
| Resource limits | Lost unless the backend sets them: the docker backend passes `--cpus`, `--memory` and `--pids-limit` from `host.toml` | Inherited: the clone keeps the source container's cores, memory and swap |
| Snapshot and rollback | Refused: `snapshot` and `rollback` raise `NotImplementedError`; the runner never calls them | Available: `pct snapshot` and `pct rollback`; the runner does not call them yet |
| Keep-awake lease and wake | Lost, harmless: there is no sleeping compute plane | Kept: the containers live on the Proxmox host, woken as for VMs |
| Energy metering | Weakened: it needs a readable sensor and reports null, never a guess, when there is none | Kept: the same package counter on the Proxmox host |
| VM id as identity | Changed: the container name is the identity; no MAC is needed | Kept: the container id is the sandbox id, the run name its hostname |
| Address discovery | Changed: `docker inspect` instead of a guest agent | Changed: `pct exec <id> -- hostname -I` instead of a guest agent |
| The executor holds only the agent token, never the acceptance | Kept | Kept for what the runner injects; the clone also carries whatever the source container holds, so the source's snapshot must hold no secret the executor may not see |
| Verdict nonce and post-spawn timestamps | Kept | Kept |
| The runner reaches the compute plane | Equivalent privilege, different shape: the runner container holds the docker socket, which is host-equivalent. The sandboxes never get the socket | Kept: ssh to the Proxmox host, as for VMs |

## Cleanup

```
docker compose -p dark down -v
```

This removes the containers, the networks and the named volumes (Gitea data,
the runner state with tokens and ledger, the pulled models).

Every service in `compose.yaml` restarts on its own after the host reboots or
the docker daemon restarts, unless it was stopped by hand: `docker compose -p
dark stop` keeps a service stopped across a reboot.

## Running a task under other harnesses

`python3 -m dark export-harbor <task-dir> <out-dir>` writes one dark task as a Terminal-Bench task: Terminal-Bench is a public benchmark whose task directory holds `task.yaml` (the instruction and its limits), a `Dockerfile`, `docker-compose.yaml`, `run-tests.sh` (how the hidden tests run) and `solution.sh` (the reference solution). The export adds `tests/` (the hidden acceptance, the test set the agent never sees) and `start/` (the task's starting files), and its `run-tests.sh` copies the acceptance to `.acceptance/` and runs it the way the stager does (the stager is dark's component that runs a run's hidden acceptance in a fresh sandbox).
Harbor, the harness from the same authors that runs a task in docker, takes the exported directory after a conversion:

```
harbor tasks migrate -i <out-dir> -o <harbor-dir>
harbor run --path <harbor-dir>/<task> --agent oracle
```

The oracle agent runs `solution.sh`, which carries the dark oracle overlay, and Harbor grades the run on the exit code of `run-tests.sh`.
