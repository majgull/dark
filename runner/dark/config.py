"""dark/config.py — models.toml and budgets.toml, loaded and validated.

The two files are the only place a human writes a number. Loading refuses
with one line naming the field on any miss: a class without a hard cap, a
tier whose provider has no window, a provisional tier that is not in the
catalog. Live validation (is the id served?) is preflight's job, not this
module's; this module never touches the network.
"""

import os
import tomllib
from dataclasses import dataclass

from . import spec

COSTS = ("local", "sub-window")
SPEEDS = ("fast", "mid", "slow")
WATTS = ("low", "mid", "high")
ROLES = ("validator", "planner", "planner-light", "reviewer")
HARD_KEYS = ("runs", "calls", "seconds", "max_reasoning_chars")


class ConfigError(Exception):
    """One line, naming the file and field. Never a traceback for a config miss."""


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    catalog: str | None   # "<url>/<catalog>" lists served ids
    health: str | None    # a URL whose JSON carries "models" (and "key")
    wake: bool
    window: str           # "" = local (watts), else a [window.*] name in budgets
    # how the provider is told dark's thinking level (the cut is dark's own,
    # identical everywhere): "reasoning_effort" (OpenAI-style field; ollama
    # honours none/low/medium/high), "chat_template"
    # (llama.cpp: chat_template_kwargs.enable_thinking on/off, no level),
    # "none" (the claude-cli proxy: nothing reaches the model)
    think_api: str = "none"
    stream: bool = False  # SSE; needed for the live cut (default: the wake flag)


THINK_LEVELS = frozenset({"none", "low", "medium", "high"})
THINK_APIS = frozenset({"none", "reasoning_effort", "chat_template"})
THINK_DEFAULT_CHARS = {"none": 0, "low": 8000, "medium": 32000, "high": 64000}


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    cost: str
    speed: str
    watts: str | None
    roles: tuple
    ctx: int | None
    timeout: int
    max_tokens: int
    rate_limit_signature: str | None
    note: str
    # thinking budget on top of max_tokens (the response budget): one
    # max_tokens shared by thinking and answer starves the answer, so the two
    # are separate. 0 = the tier does not think.
    thinking_tokens: int = 0
    # think: the tier's default level when a shift sets none (None = the
    # provider's default behaviour, i.e. what a user gets out of the box)
    think: str | None = None
    # think_presets: per-level provider model ids ((level, id) pairs): llama.cpp
    # router presets carrying --reasoning-budget, so the model ends its own
    # thinking inside the level in one request; dark's cut stays the bound.
    # () = the tier's own id at every level.
    think_presets: tuple = ()

    def preset(self, level):
        """The provider model id to request at a level."""
        return dict(self.think_presets).get(level) or self.id

    @property
    def paid(self):
        return self.cost == "sub-window"

    @property
    def local(self):
        return self.cost == "local"

    @property
    def rank_key(self):
        """Position in budgets [admission].cost_order: watts for local, speed for sub-window."""
        return f"{self.cost}/{self.watts if self.local else self.speed}"


@dataclass(frozen=True)
class Catalog:
    providers: dict
    models: dict
    defaults: dict

    def model(self, mid):
        try:
            return self.models[mid]
        except KeyError:
            raise ConfigError(f"models.toml: unknown model id {mid!r}") from None

    def provider_of(self, mid):
        return self.providers[self.model(mid).provider]

    def window_of(self, mid):
        return self.provider_of(mid).window

    def with_role(self, role):
        return [m.id for m in self.models.values() if role in m.roles]

    def ids_by_provider(self, pname):
        return {m.id for m in self.models.values() if m.provider == pname}


@dataclass(frozen=True)
class Hard:
    runs: int
    calls: int
    seconds: int
    max_reasoning_chars: int


@dataclass(frozen=True)
class ClassBudget:
    cls: str
    hard: Hard
    headroom: float
    admit_at: float
    provisional: tuple
    # think: dark's thinking level the class runs at when the shift sets none:
    # admission and the soft envelope count only runs at this level, so a
    # tier's uncontrolled-thinking past never pools with its measured rate.
    # None = each tier's own.
    think: str | None = None


@dataclass(frozen=True)
class Window:
    name: str
    daily_calls: int
    daily_tokens: int
    reserve_for_data_runs: float


@dataclass(frozen=True)
class Budgets:
    windows: dict
    daily_wh: float
    draw: dict            # watts class -> W
    classes: dict
    min_runs: int
    last_runs: int
    cost_order: tuple
    watchdog: dict
    shift: dict
    think: dict           # {"levels": {level: chars per call}, "chars_per_token": n}

    def cls(self, name):
        try:
            return self.classes[name]
        except KeyError:
            raise ConfigError(f"budgets.toml: unknown class {name!r}") from None


def _read(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML: {e}") from None


def _need(table, key, path, where, types=None):
    if key not in table:
        raise ConfigError(f"{path}: {where} needs `{key}`")
    v = table[key]
    if types is not None and not isinstance(v, types):
        raise ConfigError(f"{path}: {where}.{key} has the wrong type ({type(v).__name__})")
    return v


def _pos_int(table, key, path, where, default=None):
    v = table.get(key, default)
    if v is None:
        raise ConfigError(f"{path}: {where} needs `{key}`")
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ConfigError(f"{path}: {where}.{key} must be a positive integer")
    return v


def load_catalog(path):
    d = _read(path)
    defaults = d.get("defaults", {})
    timeout = _pos_int(defaults, "timeout", path, "[defaults]", 600)
    max_tokens = _pos_int(defaults, "max_tokens", path, "[defaults]", 8192)
    providers = {}
    for name, p in (d.get("provider") or {}).items():
        where = f"[provider.{name}]"
        url = _need(p, "url", path, where, str).rstrip("/")
        think_api = str(p.get("think_api", "none"))
        if think_api not in THINK_APIS:
            raise ConfigError(f"{path}: {where}.think_api {think_api!r} not in {sorted(THINK_APIS)}")
        wake = bool(p.get("wake", False))
        providers[name] = Provider(
            name=name, url=url, catalog=p.get("catalog"), health=p.get("health"),
            wake=wake, window=str(p.get("window", "")),
            think_api=think_api, stream=bool(p.get("stream", wake)))
    if not providers:
        raise ConfigError(f"{path}: no [provider.*] tables")
    models = {}
    for mid, m in (d.get("model") or {}).items():
        where = f'[model."{mid}"]'
        prov = _need(m, "provider", path, where, str)
        if prov not in providers:
            raise ConfigError(f"{path}: {where}.provider {prov!r} is not a [provider.*]")
        cost = _need(m, "cost", path, where, str)
        if cost not in COSTS:
            raise ConfigError(f"{path}: {where}.cost must be one of {COSTS}")
        speed = _need(m, "speed", path, where, str)
        if speed not in SPEEDS:
            raise ConfigError(f"{path}: {where}.speed must be one of {SPEEDS}")
        watts = m.get("watts")
        if cost == "local":
            if watts not in WATTS:
                raise ConfigError(f"{path}: {where} is local and needs watts in {WATTS}")
            if providers[prov].window:
                raise ConfigError(f"{path}: {where} is local but provider {prov!r} names a window")
        else:
            if watts is not None:
                raise ConfigError(f"{path}: {where} is sub-window; watts is for local tiers")
            if not providers[prov].window:
                raise ConfigError(f"{path}: {where} is sub-window but provider {prov!r} has no window")
        roles = m.get("role", [])
        if isinstance(roles, str):
            roles = [roles]
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ConfigError(f"{path}: {where}.role {bad} not in {ROLES}")
        ctx = m.get("ctx")
        if ctx is not None:
            ctx = _pos_int(m, "ctx", path, where)
        tt = m.get("thinking_tokens", 0)
        if not isinstance(tt, int) or isinstance(tt, bool) or tt < 0:
            raise ConfigError(f"{path}: {where}.thinking_tokens must be an integer >= 0")
        think = m.get("think")
        if think is not None and think not in THINK_LEVELS:
            raise ConfigError(f"{path}: {where}.think {think!r} not in {sorted(THINK_LEVELS)}")
        tp = m.get("think_presets", {})
        if not isinstance(tp, dict) or any(k not in THINK_LEVELS or k == "none" or not isinstance(v, str) or not v
                                           for k, v in tp.items()):
            raise ConfigError(f"{path}: {where}.think_presets must map low/medium/high to model ids")
        models[mid] = Model(
            id=mid, provider=prov, cost=cost, speed=speed, watts=watts, roles=tuple(roles),
            ctx=ctx, timeout=_pos_int(m, "timeout", path, where, timeout),
            max_tokens=_pos_int(m, "max_tokens", path, where, max_tokens),
            rate_limit_signature=m.get("rate_limit_signature"), note=str(m.get("note", "")),
            thinking_tokens=tt, think=think, think_presets=tuple(sorted(tp.items())))
    if not models:
        raise ConfigError(f"{path}: no [model.*] tables")
    return Catalog(providers=providers, models=models,
                   defaults={"timeout": timeout, "max_tokens": max_tokens,
                             "temperature": defaults.get("temperature")})


def load_budgets(path, catalog):
    d = _read(path)
    windows = {}
    for name, w in (d.get("window") or {}).items():
        where = f"[window.{name}]"
        reserve = w.get("reserve_for_data_runs", 0.0)
        if not isinstance(reserve, (int, float)) or not 0 <= reserve < 1:
            raise ConfigError(f"{path}: {where}.reserve_for_data_runs must be in [0, 1)")
        windows[name] = Window(name=name,
                               daily_calls=_pos_int(w, "daily_calls", path, where),
                               daily_tokens=_pos_int(w, "daily_tokens", path, where),
                               reserve_for_data_runs=float(reserve))
    for p in catalog.providers.values():
        if p.window and p.window not in windows:
            raise ConfigError(f"{path}: provider {p.name!r} names window {p.window!r}, no [window.{p.window}]")
    watts = d.get("watts") or {}
    daily_wh = watts.get("daily_wh")
    if not isinstance(daily_wh, (int, float)) or daily_wh <= 0:
        raise ConfigError(f"{path}: [watts].daily_wh must be a positive number")
    draw = {}
    for k in WATTS:
        v = watts.get(k)
        if not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"{path}: [watts].{k} (draw in W) must be a positive number")
        draw[k] = float(v)
    classes = {}
    ctab = d.get("class") or {}
    for cls in spec.CLASSES:
        if cls not in ctab:
            raise ConfigError(f"{path}: class {cls!r} has no hard cap ([class.{cls}] missing)")
        c = ctab[cls]
        where = f"[class.{cls}]"
        hard = _need(c, "hard", path, where, dict)
        hard = Hard(**{k: _pos_int(hard, k, path, f"{where}.hard") for k in HARD_KEYS})
        headroom = c.get("headroom", 0.0)
        if not isinstance(headroom, (int, float)) or headroom < 0:
            raise ConfigError(f"{path}: {where}.headroom must be >= 0")
        admit_at = c.get("admit_at")
        # 0 = no admission: every tier may run the class, the operator names it
        if isinstance(admit_at, bool) or not isinstance(admit_at, (int, float)) or not 0 <= admit_at <= 1:
            raise ConfigError(f"{path}: {where}.admit_at must be in [0, 1] (0 = no admission)")
        prov = c.get("provisional")
        if not isinstance(prov, list) or not prov:
            raise ConfigError(f"{path}: {where}.provisional must be a non-empty list of model ids")
        for mid in prov:
            if mid not in catalog.models:
                raise ConfigError(f"{path}: {where}.provisional names {mid!r}, not in models.toml")
        think = c.get("think")
        if think is not None and think not in THINK_LEVELS:
            raise ConfigError(f"{path}: {where}.think {think!r} not in {sorted(THINK_LEVELS)}")
        classes[cls] = ClassBudget(cls=cls, hard=hard, headroom=float(headroom),
                                   admit_at=float(admit_at), provisional=tuple(prov), think=think)
    for extra in set(ctab) - set(spec.CLASSES):
        raise ConfigError(f"{path}: [class.{extra}] is not a class in spec.CLASSES")
    adm = d.get("admission") or {}
    min_runs = _pos_int(adm, "min_runs", path, "[admission]")
    last_runs = _pos_int(adm, "last_runs", path, "[admission]")
    if last_runs < min_runs:
        raise ConfigError(f"{path}: [admission].last_runs must be >= min_runs")
    order = adm.get("cost_order")
    if not isinstance(order, list) or not order:
        raise ConfigError(f"{path}: [admission].cost_order must be a non-empty list")
    for m in catalog.models.values():
        if m.rank_key not in order:
            raise ConfigError(f"{path}: [admission].cost_order lacks {m.rank_key!r} (model {m.id!r})")
    wd = d.get("watchdog") or {}
    watchdog = {k: _pos_int(wd, k, path, "[watchdog]") for k in
                ("heartbeat_seconds", "silent_kill_seconds", "max_denials", "max_calls_low_variety")}
    poll = wd.get("poll_seconds")
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError(f"{path}: [watchdog].poll_seconds must be a positive number")
    watchdog["poll_seconds"] = poll
    watchdog["rate_cooldown_seconds"] = _pos_int(wd, "rate_cooldown_seconds", path, "[watchdog]", 900)
    watchdog["net_wait_seconds"] = _pos_int(wd, "net_wait_seconds", path, "[watchdog]", 180)
    if watchdog["silent_kill_seconds"] <= watchdog["heartbeat_seconds"]:
        raise ConfigError(f"{path}: [watchdog].silent_kill_seconds must exceed heartbeat_seconds")
    sh = d.get("shift") or {}
    shift = {k: _pos_int(sh, k, path, "[shift]") for k in
             ("max_runs", "concurrency", "vm_template", "vmid_base")}
    th = d.get("think") or {}
    levels = dict(THINK_DEFAULT_CHARS)
    for lvl in THINK_LEVELS:
        if lvl in th:
            v = th[lvl]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (lvl == "none" and v != 0):
                raise ConfigError(f"{path}: [think].{lvl} must be an integer >= 0 (none is always 0)")
            levels[lvl] = v
    if not levels["low"] < levels["medium"] < levels["high"]:
        raise ConfigError(f"{path}: [think] levels must grow: low < medium < high")
    think = {"levels": levels, "chars_per_token": _pos_int(th, "chars_per_token", path, "[think]", 3)}
    return Budgets(windows=windows, daily_wh=float(daily_wh), draw=draw, classes=classes,
                   min_runs=min_runs, last_runs=last_runs, cost_order=tuple(order),
                   watchdog=watchdog, shift=shift, think=think)


def load(conf_dir):
    """(catalog, budgets) from <conf_dir>/models.toml and budgets.toml."""
    catalog = load_catalog(os.path.join(conf_dir, "models.toml"))
    budgets = load_budgets(os.path.join(conf_dir, "budgets.toml"), catalog)
    return catalog, budgets


HOST_ENV = {
    "org": "DARK_ORG", "gitea_url": "DARK_GITEA_URL", "gitea_lan_url": "DARK_GITEA_LAN",
    "git_lan_url": "DARK_GIT_LAN",
    "admin_token_file": "DARK_ADMIN_TOKEN_FILE", "agent_token_file": "DARK_AGENT_TOKEN_FILE",
    "proxmox": "DARK_PROXMOX", "backend": "DARK_BACKEND", "ntfy_url": "DARK_NTFY", "state_dir": "DARK_STATE",
    "sandbox_image": "DARK_SANDBOX_IMAGE", "sandbox_network": "DARK_SANDBOX_NETWORK",
    "sandbox_cpus": "DARK_SANDBOX_CPUS", "sandbox_memory": "DARK_SANDBOX_MEMORY",
    "sandbox_pids": "DARK_SANDBOX_PIDS",
    "sandbox_container": "DARK_SANDBOX_CONTAINER", "sandbox_snapshot": "DARK_SANDBOX_SNAPSHOT",
    "sandbox_bridge": "DARK_SANDBOX_BRIDGE", "sandbox_pool": "DARK_SANDBOX_POOL",
    "browser_image": "DARK_BROWSER_IMAGE",
    "target_network": "DARK_TARGET_NETWORK", "target_host": "DARK_TARGET_HOST",
    "templates_dir": "DARK_TEMPLATES", "bench_dir": "DARK_BENCH", "task_dirs": "DARK_TASKS",
    "wake_timeout": "DARK_WAKE_TIMEOUT",
    "power_cpu_host": "DARK_POWER_CPU", "power_gpu_host": "DARK_POWER_GPU", "work_org": "DARK_WORK_ORG",
    "agent_user": "DARK_AGENT_USER", "records_org": "DARK_RECORDS_ORG",
}


@dataclass
class Host:
    org: str = "dark"
    work_org: str = ""        # where task work repos (t-*, tl-*, session-*) are created; "" = org
    gitea_url: str = "http://localhost:3400"
    gitea_lan_url: str = "http://localhost:3400"
    git_lan_url: str = ""     # where VMs clone/push from; "" = gitea_lan_url
    admin_token_file: str = "~/.dark/dark-admin.token"
    agent_token_file: str = "~/.dark/dark-agent.token"
    backend: str = "proxmox"  # the compute plane: "proxmox", "docker" or "lxc"
    proxmox: str = "cpu-host"
    # docker backend (dark/docker.py): the image a sandbox is created from,
    # the network it joins, and the limits its container gets. "" = no limit.
    sandbox_image: str = "dark-sandbox"
    sandbox_network: str = "dark"
    sandbox_cpus: str = "2"
    sandbox_memory: str = "2g"
    sandbox_pids: int = 512
    # lxc backend (dark/lxc.py): the container a sandbox is a full clone of,
    # the snapshot it is cloned from, and the bridge its net0 joins. The
    # container and snapshot have no default; preflight refuses them empty.
    sandbox_container: str = ""
    sandbox_snapshot: str = ""
    sandbox_bridge: str = "vmbr0"
    # the Proxmox pool a clone is placed in, so the throwaways are grouped and
    # never mixed with the containers that serve; "" = no pool
    sandbox_pool: str = ""
    # the image a user-arm sandbox is created from (runner/sandbox/Dockerfile.browser):
    # the sandbox image plus Chromium and Playwright
    browser_image: str = "dark-sandbox-browser"
    # where a user-arm sandbox may reach the application it checks, besides
    # the service host: a second, non-internal docker network it joins
    # (docker backend), or one address its firewall lets out to (Proxmox).
    # "" = nothing more than any other sandbox.
    target_network: str = ""
    target_host: str = ""
    ntfy_url: str = ""
    state_dir: str = "~/.dark"
    templates_dir: str = "~/dark-templates"
    bench_dir: str = "~/dark-bench"
    # the task path: one task-set root, or several joined with os.pathsep
    # (PATH style). "" = the bench_dir above. Each root holds tasks/.
    task_dirs: str = ""
    wake_timeout: int = 420
    power_cpu_host: str = ""  # ssh target with the RAPL package counter (the Proxmox host); "" = no CPU metering
    power_gpu_host: str = ""  # ssh target where nvidia-smi sees the GPUs (the model VM); "" = no GPU metering
    agent_user: str = "dark-agent"  # the Gitea user the executor VMs push as
    records_org: str = "dark-records"  # one repo per shift; session-arm and review-mode transcripts
    admin_token: str = ""
    agent_token: str = ""

    def __post_init__(self):
        for f in ("state_dir", "templates_dir", "bench_dir", "admin_token_file", "agent_token_file"):
            setattr(self, f, os.path.expanduser(getattr(self, f)))
        self.wake_timeout = int(self.wake_timeout)
        self.work_org = self.work_org or self.org
        self.sandbox_cpus = "" if self.sandbox_cpus in (None, "") else str(self.sandbox_cpus)
        self.sandbox_memory = "" if self.sandbox_memory in (None, "") else str(self.sandbox_memory)
        self.sandbox_container = "" if self.sandbox_container in (None, "") else str(self.sandbox_container)
        self.sandbox_pool = "" if self.sandbox_pool in (None, "") else str(self.sandbox_pool)
        try:
            self.sandbox_pids = int(self.sandbox_pids or 0)
        except (TypeError, ValueError):
            self.sandbox_pids = 0

    @property
    def ledger_path(self):
        return os.path.join(self.state_dir, "ledger.jsonl")

    def task_path(self):
        """Where the task set is read from: DARK_TASKS when set, else the
        bench checkout. One root or several joined with os.pathsep."""
        return self.task_dirs or self.bench_dir

    @property
    def abort_dir(self):
        return os.path.join(self.state_dir, "abort")

    @property
    def scratch_dir(self):
        return os.path.join(self.state_dir, "scratch")


BACKENDS = ("proxmox", "docker", "lxc")  # implementations of the dark/sandbox.Sandbox protocol


def check_backend(name):
    """Refuse a compute-plane backend this build does not implement, naming
    it in one line. dark/sandbox.Sandbox is the seam every backend fills."""
    if name not in BACKENDS:
        raise ConfigError(f'host backend {name!r}: supported values are {", ".join(BACKENDS)}')


def load_host(path, environ=os.environ):
    d = _read(path).get("host") or {}
    vals = {}
    for key, env in HOST_ENV.items():
        if env in environ:
            vals[key] = environ[env]
        elif key in d:
            vals[key] = d[key]
    host = Host(**vals)
    check_backend(host.backend)
    return host
