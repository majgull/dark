#!/usr/bin/env python3
"""dark session - the executor for a session arm (a coding-agent CLI solving
the task as a free session). Runs INSIDE the same throwaway VM the pipeline
uses, injected alone via cloud-init with /opt/task.json. Stdlib only, like
dark/agent.py, and self-contained for the same reason.

What differs from dark/agent.py is one thing: instead of the FILE:/DELETE:
protocol loop, the model runs as a free agent session driven by pi (a
coding-agent CLI) with its own tools, in the work tree, and hands in
whatever it ends with. Everything else is held the same on purpose, because
the arm is the one variable of a comparison: same VM image, same starting
tree, same envelope, same model endpoint, same progress and verdict comments
on the same issue, same branch push, and the hidden acceptance judged
afterwards in the same fresh staging VM.

The VM has no node and its egress reaches the service host only, so the
runtime (node plus the pi package) is fetched from Gitea like everything
else the VM needs, and unpacked under /opt/rt.

Three numbers this arm can measure that a chat harness could not. pi writes
one JSON object per line on stdout, so the model calls, the tool calls and
the provider's own token usage are counted from the stream as it happens,
not scraped from an audit trail afterwards. The call envelope is enforced
the same way the pipeline enforces it: when the count of model calls passes
the envelope, the session is killed and the run ends fail:budget on calls.

The stream is also kept, not just counted: every line pi
emits is written to stream.jsonl, and at the end of the run — pass, fail or
killed at the envelope alike — it is pushed together with the brief and the
(token-scrubbed) task.json to a records repository, so a session-arm run
leaves a transcript that survives the VM. A push failure is reported, never
fatal: it cannot change the run's outcome.
"""

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request


def _load_task():
    try:
        with open(os.environ.get("DARK_TASK", "/opt/task.json")) as f:
            return json.load(f)
    except OSError:
        return {}


TASK = _load_task()
API = f"{TASK.get('gitea', '')}/api/v1"
REPO = TASK.get("repo", "")
WORK = os.environ.get("DARK_WORK", "/opt/work")
# several repositories (task "repos"): each is cloned to MULTI_WORK/<name>
# and the session runs in MULTI_WORK, above them all
MULTI_WORK = os.environ.get("DARK_MULTI_WORK", "/work")
RT = os.environ.get("DARK_RT", "/opt/rt")
SESSION_DIR = "/opt/pisession"
PIHOME = os.environ.get("DARK_PIHOME", "/opt/pihome")
MAX_CALLS = int(TASK.get("max_calls", 6))
HEARTBEAT = int(TASK.get("heartbeat_seconds", 60))
SPEC_TEXT = TASK.get("spec", "")
RUN_ID = TASK.get("run", "run")
RECORDS_DIR = os.environ.get("DARK_RECORDS", "/opt/records")
RECORDS_WORK = os.environ.get("DARK_RECORDS_WORK", "/opt/records-work")
STREAM_PATH = os.path.join(RECORDS_DIR, "stream.jsonl")
T0 = time.time()

BRIEF = """You are working alone in the git work tree at {work} (a {lang} project). Implement the task below end to end. The gate is `bash .dark/verify.sh` at the repo root: it must be green when you finish. Commit your work with git when done (any message). Do NOT push, do NOT create branches, do NOT edit .dark/verify.sh or anything under .gitea/, and do not touch files outside this directory.

{grant}
TASK:
{spec}
"""

# several repositories: one work tree per repo under {work}, one branch
# pushed in each at the end
MULTI_BRIEF = """You are working alone in {work}, which holds one git work tree per repository: {names}. Implement the task below across them end to end. Where a repository has .dark/verify.sh, `bash .dark/verify.sh` at its root must be green when you finish. Commit your work with git in each repository you change (any message). Do NOT push, do NOT create branches, do NOT edit .dark/verify.sh or anything under .gitea/, and do not touch files outside {work}.

{grant}
TASK:
{spec}
"""

# review mode: no repo, no branch, no verify.sh - the files
# to read are already staged under {work}, and the only deliverable is
# report.md there.
REVIEW_BRIEF = """You are reviewing material in the directory {work}. Read the files listed below, already present in that directory, and write your findings to report.md in {work}. That file is your only deliverable: do not modify any other file, and do not create a git repository.

Files to read: {files}
{judge}
BRIEF:
{spec}
"""

# review mode as a judge: the branches under review are cloned into {work}
# before the session starts, and report.md's last line is the verdict
REVIEW_JUDGE = """
You are the judge of the work on these branches, each already cloned into its own directory under {work}: {branches}. Read them; do not modify them and do not push. The LAST line of report.md must be exactly `VERDICT: pass — <one line of reason>` or `VERDICT: fail — <one line of reason>`; anything else counts as no verdict.
"""


# --- reporting: the same two comments the pipeline writes ---------------------
def gitea(path, method="GET", data=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"token {TASK['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "{}")


def scrub(body):
    return body.replace(TASK.get("token", "\0"), "***")


def comment(body):
    try:
        return gitea(f"/repos/{REPO}/issues/{TASK['issue']}/comments", "POST",
                     {"body": scrub(body)}).get("id")
    except Exception as e:  # noqa: BLE001 — reporting must never kill the run
        print(f"comment failed: {e}", file=sys.stderr)
        return None


def tag(ev, **kw):
    return "DARK:" + json.dumps({"v": 2, "ev": ev, **kw}, sort_keys=True)


# tool_calls counts tool calls that RAN (pi's tool_execution_start); asked
# counts the ones the model produced. They are the same number unless
# something refused one, so both are counted and a difference is said out
# loud rather than averaged away.
STATS = {"calls": 0, "tool_calls": 0, "asked": 0, "tokens_in": 0, "tokens_out": 0,
         "reasoning_chars": 0, "requests": 0}


class Progress:
    """The one progress comment, PATCHed on a timer, exactly as the
    pipeline's executor does it: the runner's watchdog reads updated_at and
    kills a VM that goes silent, and it must not be able to tell the arms
    apart by their heartbeat."""

    def __init__(self):
        self.cid = None
        self.header = ""
        self.lines = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def start(self, header):
        self.header = header
        self.cid = comment(self.body())
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stopped.set()

    def body(self):
        return "\n".join([self.header, *self.lines,
                          tag("beat", at=int(time.time()), calls=STATS["calls"],
                              started=STATS["requests"])])

    def push(self):
        if self.cid is None:
            return
        try:
            gitea(f"/repos/{REPO}/issues/comments/{self.cid}", "PATCH", {"body": scrub(self.body())})
        except Exception as e:  # noqa: BLE001
            print(f"progress patch failed: {e}", file=sys.stderr)

    def add(self, line):
        with self.lock:
            self.lines.append(line)
            self.push()

    def _loop(self):
        while not self.stopped.wait(HEARTBEAT):
            with self.lock:
                self.push()


PROGRESS = Progress()


def done(outcome, kind=None, **kw):
    fields = dict(outcome=outcome, calls=STATS["calls"], tool_calls=STATS["tool_calls"],
                  tokens_in=STATS["tokens_in"], tokens_out=STATS["tokens_out"],
                  reasoning_chars=STATS["reasoning_chars"], requests=STATS["requests"],
                  seconds=int(time.time() - T0), **kw)
    if kind:
        fields["kind"] = kind
    return tag("done", **fields)


def fail(kind, text, **kw):
    comment(f"AGENT-DONE fail ({kind}): {text}\n" + done("fail", kind, **kw, **records_kw()))
    return 1


def sh(*cmd, **kw):
    kw.setdefault("cwd", WORK)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def with_token(url):
    """An http(s) URL with the agent token in it; any other URL as it is."""
    if url.startswith("http"):
        return url.replace("://", f"://dark-agent:{TASK['token']}@", 1)
    return url


def clone_url(repo=None):
    base = TASK.get("git_url") or TASK["gitea"]
    return f"{with_token(base)}/{repo or REPO}.git"


# --- records: pi's stream, the brief and task.json, kept ----------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


PUSH_ATTEMPTS = 6
RETRY_PAUSE = time.sleep  # module attribute so a test can set it to a no-op


def push_records(extra_files=None, extra_paths=None, timeout=None, run_id=None, records_work=None):
    """Clone the records repo, add stream.jsonl / brief.md / task.json (and,
    in review mode, report.md) under RUN_ID/, commit and push. Never force:
    other runs of the same shift have their own RUN_ID directory in the same
    repo. `extra_paths` is {name: local file or directory} copied as it is
    (the user arm's steps.jsonl and screenshots). `timeout` (seconds) caps
    every git call at what is left of it; when it runs out the result is
    (False, "timed out after <n> s"). `run_id` and `records_work` default to
    the module's RUN_ID and RECORDS_WORK; a test overrides them to push
    several runs against the same repo at once without the calls stepping on
    each other's clone. (ok, path-or-error).

    Only the newest commit and its trees are fetched, never the blobs of the
    earlier runs, and nothing is checked out: the clone is shallow and
    blobless. The index comes from that commit, so the run's own files can be
    staged alone, and the commit is written with `write-tree --missing-ok`
    because `git commit` verifies that every referenced blob exists and would
    fetch every earlier run's files.

    Every git step that changes the index, the ref or the commit is checked:
    a non-zero exit returns False with the command's name in the reason,
    instead of risking a push that silently lands the wrong tree or a HEAD
    that never moved.

    A rejected push means another run landed first: fetch the new tip the
    same way, move the branch to it without touching files, and push again.
    No pull --rebase: there is no checked-out tree to rebase. Up to
    PUSH_ATTEMPTS pushes are tried in all, each but the first after a short
    random pause, before giving up with the last rejection as the reason."""
    repo = TASK.get("records_repo")
    if not repo:
        return False, "no records_repo in task.json"
    run_id = RUN_ID if run_id is None else run_id
    records_work = RECORDS_WORK if records_work is None else records_work
    deadline = None if timeout is None else time.monotonic() + timeout

    class _TimedOut(Exception):
        pass

    def run_git(args, cwd=None):
        kwargs = {"capture_output": True, "text": True}
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _TimedOut
            kwargs["timeout"] = left
        try:
            return subprocess.run(args, cwd=cwd, **kwargs)
        except subprocess.TimeoutExpired:
            raise _TimedOut from None

    def failed(name, result):
        return False, f"{name} failed: " + scrub((result.stdout + result.stderr)[-300:])

    try:
        shutil.rmtree(records_work, ignore_errors=True)
        r = run_git(["git", "clone", "-q", "--depth", "1", "--filter=blob:none",
                     "--no-checkout", clone_url(repo), records_work])
        if r.returncode != 0:
            return False, scrub(r.stderr[-300:])

        def rsh(*cmd):
            return run_git(["git", *cmd], cwd=records_work)

        def commit(parent, message):
            """(ok, commit-sha-or-reason)."""
            tree = rsh("write-tree", "--missing-ok")
            if tree.returncode != 0:
                return failed("write-tree", tree)
            cmd = ["commit-tree", tree.stdout.strip()]
            if parent:
                cmd += ["-p", parent]
            c = rsh(*(cmd + ["-m", message]))
            if c.returncode != 0:
                return failed("commit-tree", c)
            u = rsh("update-ref", "HEAD", c.stdout.strip())
            if u.returncode != 0:
                return failed("update-ref", u)
            return True, c.stdout.strip()

        def add_run():
            ad = rsh("add", "--", run_id)
            if ad.returncode != 0:
                return failed("add", ad)
            return True, None

        rsh("config", "user.name", "dark-session")
        rsh("config", "user.email", "dark-session@localhost")
        head = rsh("rev-parse", "--verify", "-q", "HEAD")
        if head.returncode == 0:
            rt = rsh("read-tree", "HEAD")
            if rt.returncode != 0:
                return failed("read-tree", rt)
        else:
            # a repository created empty has no commit: only point HEAD at main
            sr = rsh("symbolic-ref", "HEAD", "refs/heads/main")
            if sr.returncode != 0:
                return failed("symbolic-ref", sr)
        run_dir = os.path.join(records_work, run_id)
        os.makedirs(run_dir, exist_ok=True)
        if os.path.exists(STREAM_PATH):
            shutil.copyfile(STREAM_PATH, os.path.join(run_dir, "stream.jsonl"))
        else:
            open(os.path.join(run_dir, "stream.jsonl"), "w").close()
        with open(os.path.join(run_dir, "brief.md"), "w") as f:
            f.write(SPEC_TEXT)
        with open(os.path.join(run_dir, "task.json"), "w") as f:
            f.write(scrub(json.dumps(TASK, indent=1, sort_keys=True)))
        for name, content in (extra_files or {}).items():
            with open(os.path.join(run_dir, name), "w") as f:
                f.write(content)
        for name, src in (extra_paths or {}).items():
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(run_dir, name), dirs_exist_ok=True)
            elif os.path.exists(src):
                shutil.copyfile(src, os.path.join(run_dir, name))
        ok, reason = add_run()
        if not ok:
            return ok, reason
        ok, result = commit("HEAD" if head.returncode == 0 else None, f"records: {run_id}")
        if not ok:
            return ok, result

        reason = None
        for attempt in range(PUSH_ATTEMPTS):
            push = rsh("push", "-q", "origin", "HEAD:main")
            if push.returncode == 0:
                return True, f"{repo}/{run_id}"
            reason = scrub((push.stdout + push.stderr)[-300:])
            if attempt == PUSH_ATTEMPTS - 1:
                break
            RETRY_PAUSE(random.uniform(0.2, 1.0))
            # another run pushed first: fetch the new tip the same way, move the
            # branch to it without touching files, and push again. No pull
            # --rebase: there is no checked-out tree to rebase.
            f = rsh("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "main")
            if f.returncode != 0:
                return False, f"push rejected: {reason}; fetch failed: " + scrub(f.stderr[-300:])
            rs = rsh("reset", "-q", "--soft", "FETCH_HEAD")
            if rs.returncode != 0:
                return failed("reset --soft", rs)
            rt = rsh("read-tree", "FETCH_HEAD")
            if rt.returncode != 0:
                return failed("read-tree", rt)
            ok, reason2 = add_run()
            if not ok:
                return ok, reason2
            ok, result = commit("FETCH_HEAD", f"records: {run_id}")
            if not ok:
                return ok, result
        return False, f"push rejected: {reason}"
    except _TimedOut:
        return False, f"timed out after {timeout} s"


def records_kw(extra_files=None, extra_paths=None, timeout=None):
    """{"records": ..., "records_sha256": ...} for a done/fail tag. `timeout`
    caps the push and is reported like any other push failure. A push
    failure never raises and never changes the run's outcome; it only says
    so in the field the runner reads."""
    try:
        ok, info = push_records(extra_files, extra_paths, timeout)
    except Exception as e:  # noqa: BLE001 — records must never crash the run
        ok, info = False, f"{type(e).__name__}: {e}"
    if not ok:
        return {"records": f"PUSH FAILED: {info}"}
    try:
        # a run that failed before pi started has no stream; push_records
        # kept an empty one, so the sum is the empty file's
        sha = (sha256_file(STREAM_PATH) if os.path.exists(STREAM_PATH)
               else hashlib.sha256(b"").hexdigest())
    except Exception:  # noqa: BLE001 — records must never crash the run
        return {"records": info}
    return {"records": info, "records_sha256": sha}


def crash_text(e):
    """The AGENT-DONE line for an exception nothing else caught. An OSError's
    repr leaves out the path, and the path is what says what was missing."""
    fn = getattr(e, "filename", None)
    return f"AGENT-DONE fail (crash): {e!r}" + (f" (file: {fn})" if fn else "")


# --- the runtime the VM cannot fetch for itself -------------------------------
def fetch_runtime():
    """Unpack node and pi under RT. The tarball is a file in a Gitea repo:
    the same host, the same token, the same one hop the work repo uses."""
    url = TASK["runtime_url"]
    os.makedirs(RT, exist_ok=True)
    req = urllib.request.Request(url, headers={"Authorization": f"token {TASK['token']}"})
    tmp = "/opt/runtime.tar.gz"
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    with tarfile.open(tmp) as t:
        t.extractall(RT)          # noqa: S202 — our own artefact, built by ops
    os.unlink(tmp)
    node = os.path.join(RT, "node/bin/node")
    cli = os.path.join(RT, "pi/dist/cli.js")
    for p in (node, cli):
        if not os.path.exists(p):
            raise OSError(f"runtime is missing {p}")
    return node, cli


def write_models_json(home):
    """pi's catalog, pointing at the same endpoint dark/agent.py posts to.
    The key is the placeholder pi's documentation asks for on a server that
    ignores it; no credential of any kind reaches this VM."""
    d = os.path.join(home, ".pi", "agent")
    os.makedirs(d, exist_ok=True)
    model = {"id": TASK["llm_model"], "name": TASK["llm_model"],
             "reasoning": bool(TASK.get("thinking_tokens")),
             "contextWindow": int(TASK.get("ctx") or 32768),
             "maxTokens": int(TASK.get("max_tokens") or 8192),
             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    doc = {"providers": {"dark": {
        "baseUrl": TASK["llm_url"], "api": "openai-completions", "apiKey": "unused",
        "compat": {"supportsDeveloperRole": False,
                   "supportsReasoningEffort": TASK.get("think_api") == "reasoning_effort"},
        "models": [model]}}}
    with open(os.path.join(d, "models.json"), "w") as f:
        json.dump(doc, f, indent=1)


# dark's thinking levels against pi's own names. dark cuts thinking by
# characters and pi asks the provider for an effort, so this is the nearest
# thing pi understands, the same compromise dark/agent.py makes with
# reasoning_effort. A level dark does not set leaves pi's default alone.
THINK = {"none": "off", "low": "low", "medium": "medium", "high": "high"}

# what the reduced tool set (the default) takes away from pi; a task with
# tools = "full" runs pi as shipped, and online
REDUCED_FLAGS = ["--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates"]

# a tier written "claude:<model-id>" (dark/config.py's own is_claude_tier)
# runs Claude Code in place of pi; session.py is stdlib-only and cannot
# import that module, so the same prefix is read here from the string
# dark/run.py already put in TASK["llm_model"].
CLAUDE_PREFIX = "claude:"

# Claude Code's own settings for the calls it makes beside the API itself
# (code.claude.com/docs/en/network-config, "Network access requirements",
# fetched 2026-09-30): CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC turns off
# both Datadog telemetry/error-report hosts, and ENABLE_CLAUDEAI_MCP_SERVERS
# turns off the claude.ai MCP connector fetch through mcp-proxy.anthropic.com
# (on by default for a claude.ai-authenticated token, which a setup-token
# credential is). Turned off so this sandbox needs no host beyond
# api.anthropic.com, reached through claude_base_url (the claude gate) and
# never directly. The same page names platform.claude.com for the
# credential's OAuth exchange, refresh and revocation; a sandbox has no
# route to it, and a long-lived token runs without it.
CLAUDE_QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

# What Claude Code is given as its token. It is not one: the claude gate
# replaces the Authorization header of every request with the real token,
# which only the gate can read. Claude Code needs some value to start in
# this mode, and sent to Anthropic unchanged this one is refused (401).
CLAUDE_TOKEN_PLACEHOLDER = "held-by-the-claude-gate"


def claude_model_id():
    """The model id after "claude:" in this run's tier, or None: dark/run.py
    decides what a tier means, session.py only reads the string."""
    m = TASK.get("llm_model") or ""
    return m[len(CLAUDE_PREFIX):] if m.startswith(CLAUDE_PREFIX) else None


def read_stream(proc, deadline, stream_path=None):
    """Count what pi reports as it reports it, and stop the session when it
    passes the call envelope. Every raw line is written to `stream_path` as
    it arrives, so a kill at the envelope loses nothing
    already emitted. Returns (killed_for, tail)."""
    killed_for = None
    tail = []
    stream_f = open(stream_path, "w") if stream_path else None
    try:
        for line in proc.stdout:
            if stream_f:
                stream_f.write(line if line.endswith("\n") else line + "\n")
                stream_f.flush()
            tail.append(line[:400])
            del tail[:-40]
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "message_start" and (ev.get("message") or {}).get("role") == "assistant":
                STATS["calls"] += 1
                STATS["requests"] += 1
                PROGRESS.add(tag("verify", ok=False, iter=STATS["calls"], calls=STATS["calls"]))
            elif t == "tool_execution_start":
                STATS["tool_calls"] += 1
            elif t == "message_end":
                m = ev.get("message") or {}
                u = m.get("usage") or {}
                # pi reports usage per assistant message, so the run's total is
                # the sum over the messages
                STATS["tokens_in"] += int(u.get("input") or 0)
                STATS["tokens_out"] += int(u.get("output") or 0)
                for c in m.get("content") or []:
                    if c.get("type") == "thinking":
                        STATS["reasoning_chars"] += len(c.get("thinking") or "")
                    elif c.get("type") in ("toolCall", "tool_call", "toolUse", "tool_use"):
                        STATS["asked"] += 1
            if STATS["calls"] > MAX_CALLS:
                killed_for = "calls"
                break
            if time.time() > deadline:
                killed_for = "seconds"
                break
    finally:
        if stream_f:
            stream_f.close()
    if killed_for:
        proc.kill()
    return killed_for, "".join(tail)


def build_brief(work, review=False):
    """The one prompt text a session gets, for pi and for Claude Code alike:
    review mode, several repositories, or the plain single-repo grant."""
    grant = TASK.get("may_edit") or []
    grant = ("Existing files you may rewrite: " + ", ".join(grant) + ". Any other existing file "
             "must stay as it is.\n" if grant else "Add new files only; do not rewrite an "
             "existing file unless the task says so.\n")
    if review:
        judged = TASK.get("review_branches") or []
        judge = REVIEW_JUDGE.format(work=work, branches=", ".join(
            f"{b['name']}/ ({b['branch']})" for b in judged)) if judged else ""
        return REVIEW_BRIEF.format(work=work, spec=SPEC_TEXT, judge=judge,
                                   files=", ".join(TASK.get("review_files") or []))
    if TASK.get("repos"):
        return MULTI_BRIEF.format(work=work, names=", ".join(r["name"] for r in TASK["repos"]),
                                  spec=SPEC_TEXT, grant=grant)
    return BRIEF.format(work=work, lang=TASK.get("lang") or "software", spec=SPEC_TEXT, grant=grant)


def read_claude_stream(proc, deadline, stream_path=None):
    """Count Claude Code's own stream-json the same way read_stream() counts
    pi's, so the runner cannot tell the two brains apart from the outside:
    one "assistant" line is one call, its usage is that call's tokens
    (Claude Code reports usage per turn, not cumulative), a tool_use is asked
    and its answering tool_result in the next "user" line is one run. Every
    raw line is written to `stream_path` as it arrives, so a kill at the
    envelope loses nothing already emitted. Returns (killed_for, tail)."""
    killed_for = None
    tail = []
    seen = set()
    base_in, base_out = STATS["tokens_in"], STATS["tokens_out"]
    stream_f = open(stream_path, "w") if stream_path else None
    try:
        for line in proc.stdout:
            if stream_f:
                stream_f.write(line if line.endswith("\n") else line + "\n")
                stream_f.flush()
            tail.append(line[:400])
            del tail[:-40]
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "assistant":
                m = ev.get("message") or {}
                u = m.get("usage") or {}
                # Claude Code writes one line per content block, each with
                # the same message id and the usage the call started with:
                # a call is one id, counted on its first line
                if m.get("id") is None or m.get("id") not in seen:
                    seen.add(m.get("id"))
                    STATS["calls"] += 1
                    STATS["requests"] += 1
                    PROGRESS.add(tag("verify", ok=False, iter=STATS["calls"], calls=STATS["calls"]))
                    STATS["tokens_in"] += _claude_tokens_in(u)
                    STATS["tokens_out"] += int(u.get("output_tokens") or 0)
                for c in m.get("content") or []:
                    if c.get("type") == "thinking":
                        STATS["reasoning_chars"] += len(c.get("thinking") or "")
                    elif c.get("type") == "tool_use":
                        STATS["asked"] += 1
            elif t == "user":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_result":
                        STATS["tool_calls"] += 1
            elif t == "result" and isinstance(ev.get("usage"), dict):
                # the per-line usage is what each call started with (output
                # near zero); the result line carries the session's totals
                STATS["tokens_in"] = base_in + _claude_tokens_in(ev["usage"])
                STATS["tokens_out"] = base_out + int(ev["usage"].get("output_tokens") or 0)
            if STATS["calls"] > MAX_CALLS:
                killed_for = "calls"
                break
            if time.time() > deadline:
                killed_for = "seconds"
                break
    finally:
        if stream_f:
            stream_f.close()
    if killed_for:
        proc.kill()
    return killed_for, "".join(tail)


def _claude_tokens_in(u):
    return (int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0))


def run_claude(model_id, deadline, stream_path, review=False, work=None):
    """Claude Code in print mode, stream-json out, permission prompts
    skipped (safe only because the sandbox is the boundary), in place of pi.
    Same brief, same envelope, same heartbeat and done tag: only the brain
    changes for the arm this run is. No real token is in this sandbox:
    Claude Code gets a placeholder, and its requests go to the claude gate
    (TASK["claude_base_url"]), which puts the real one on each of them."""
    work = work or WORK
    brief = build_brief(work, review)
    # the sandbox runs as root, and Claude Code refuses
    # --dangerously-skip-permissions for root unless told it is in a sandbox
    env = dict(os.environ, IS_SANDBOX="1", CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_TOKEN_PLACEHOLDER,
               **CLAUDE_QUIET_ENV)
    if TASK.get("claude_base_url"):
        env["ANTHROPIC_BASE_URL"] = TASK["claude_base_url"]
    cmd = ["claude", "--model", model_id, "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions", "-p", brief]
    proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    killed_for, tail = read_claude_stream(proc, deadline, stream_path)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = (proc.stderr.read() or "")[-1200:] if proc.stderr else ""
    return killed_for, tail, err, proc.returncode


def run_session(node, cli, home, deadline, stream_path, review=False, work=None):
    work = work or WORK
    full = TASK.get("tools") == "full"
    env = dict(os.environ, HOME=home, NO_COLOR="1", TERM="dumb")
    if full:
        env.pop("PI_OFFLINE", None)
    else:
        env["PI_OFFLINE"] = "1"
    brief = build_brief(work, review)
    cmd = [node, cli, "--provider", "dark", "--model", TASK["llm_model"], "--api-key", "unused",
           "--mode", "json", "--session-dir", SESSION_DIR]
    if not full:
        # the default, reduced tool set: pi's own tools and nothing it would load
        cmd += REDUCED_FLAGS
    cmd += ["--approve", "-p", brief]
    lvl = THINK.get(TASK.get("think") or "")
    if lvl:
        cmd += ["--thinking", lvl]
    proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    killed_for, tail = read_stream(proc, deadline, stream_path)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = (proc.stderr.read() or "")[-1200:] if proc.stderr else ""
    return killed_for, tail, err, proc.returncode


def verify(work=None, fallback=True):
    """verify.sh in `work`, else (with `fallback`) the unittest default. A
    repository of several that has no verify.sh is not gated: (True, "")."""
    work = work or WORK
    if os.path.exists(f"{work}/.dark/verify.sh"):
        r = sh("bash", ".dark/verify.sh", cwd=work)
    elif fallback:
        r = sh("python3", "-m", "unittest", "discover", "-v", cwd=work)
    else:
        return True, ""
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def clone_trees():
    """[(name, work tree)] for this run, cloned, or (None, why) when a clone
    failed. One repository (`repo`) is cloned to WORK; several (`repos`, a
    list of {name, url, base}) each to MULTI_WORK/<name> on its base."""
    repos = TASK.get("repos") or []
    if not repos:
        r = subprocess.run(["git", "clone", "-q", clone_url(), WORK], capture_output=True, text=True)
        if r.returncode != 0:
            return None, "clone failed: " + scrub(r.stderr[-300:])
        return [(REPO, WORK)], ""
    os.makedirs(MULTI_WORK, exist_ok=True)
    trees = []
    for entry in repos:
        d = os.path.join(MULTI_WORK, entry["name"])
        r = subprocess.run(["git", "clone", "-q", "--branch", entry["base"], with_token(entry["url"]), d],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, f"clone {entry['name']} failed: " + scrub(r.stderr[-300:])
        trees.append((entry["name"], d))
    return trees, ""


def main():
    try:
        return _main()
    finally:
        PROGRESS.stop()


def _main():
    if shutil.which("git") is None:
        return fail("env", "no `git` in this VM image (wrong template?)")
    if TASK.get("mode", "task") == "review":
        return _main_review()
    return _main_task()


def _main_task():
    trees, why = clone_trees()
    if trees is None:
        return fail("env", why)
    bases = {}
    for name, d in trees:
        sh("git", "config", "user.name", "dark-session", cwd=d)
        sh("git", "config", "user.email", "dark-session@localhost", cwd=d)
        sh("git", "switch", "-qc", TASK["branch"], cwd=d)
        bases[name] = sh("git", "rev-parse", "HEAD", cwd=d).stdout.strip()
    work = MULTI_WORK if TASK.get("repos") else WORK

    PROGRESS.start(f"AGENT-ALIVE run {TASK.get('run')} model {TASK['llm_model']} "
                   f"envelope {MAX_CALLS} calls (session arm)\n"
                   + tag("start", model=TASK["llm_model"], calls_max=MAX_CALLS))
    home = PIHOME
    os.makedirs(home, exist_ok=True)
    claude = claude_model_id()
    if not claude:
        try:
            node, cli = fetch_runtime()
        except (urllib.error.URLError, OSError, tarfile.TarError) as e:
            return fail("env", f"runtime: {scrub(str(e))[:300]}")
        write_models_json(home)

    os.makedirs(RECORDS_DIR, exist_ok=True)
    deadline = T0 + int(TASK.get("max_seconds") or 900)
    try:
        if claude:
            killed_for, tail, err, rc = run_claude(claude, deadline, STREAM_PATH, work=work)
        else:
            killed_for, tail, err, rc = run_session(node, cli, home, deadline, STREAM_PATH, work=work)
    except OSError as e:
        return fail("env", f"{'claude' if claude else 'pi'} did not start: {e}")
    ok, out = True, ""
    for name, d in trees:
        # one repository keeps the unittest default; of several, only the
        # ones that carry a verify.sh are gated
        ok, out = verify(d, fallback=not TASK.get("repos"))
        if not ok:
            out = f"{name}: {out}" if TASK.get("repos") else out
            break
    PROGRESS.add(tag("verify", ok=ok, iter=STATS["calls"], calls=STATS["calls"]))

    # whatever the session left is the delivery, exactly as a chat arm hands
    # in its work tree: there is no reply protocol to stage by, so add
    # everything. The brief asks the session to commit, and it usually has, so
    # this commit is for what it left uncommitted and a "nothing to commit"
    # here is not a session that did nothing: only a HEAD that never moved is.
    heads = {}
    for name, d in trees:
        sh("git", "add", "-A", cwd=d)
        sh("git", "commit", "-qm",
           f"dark session: {TASK.get('task', 'task')} ({TASK.get('run', 'run')})", cwd=d)
        heads[name] = sh("git", "rev-parse", "HEAD", cwd=d).stdout.strip()
    if killed_for == "calls":
        return fail("calls", f"call envelope ({MAX_CALLS}) spent\n```\n{tail[-800:]}\n```")
    if killed_for == "seconds":
        return fail("seconds", f"wall envelope spent\n```\n{tail[-800:]}\n```")
    if STATS["calls"] == 0:
        return fail("llm", f"{'claude' if claude else 'pi'} made no model call (rc {rc})\n```\n{err[-600:]}\n```")
    if not ok:
        return fail("calls", f"verify.sh red when the session ended\n```\n{out[-800:]}\n```")
    if heads == bases:
        return fail("no_blocks", "the session left no commit and no change")
    pushed = []
    for name, d in trees:
        sh("git", "push", "-q", "origin", "--delete", TASK["branch"], cwd=d)
        push = sh("git", "push", "-q", "origin", f"HEAD:{TASK['branch']}", cwd=d)
        if push.returncode != 0:
            return fail("push", f"push {name} failed: " + scrub(push.stderr[-600:]), branches=pushed)
        pushed.append({"repo": name, "branch": TASK["branch"]})
    gap = ("" if STATS["asked"] == STATS["tool_calls"]
           else f" (asked {STATS['asked']}, ran {STATS['tool_calls']})")
    comment(f"AGENT-DONE ok calls={STATS['calls']} tool_calls={STATS['tool_calls']}{gap} "
            f"branch={TASK['branch']}\n"
            + done("ok", branch=TASK["branch"], branches=pushed, **records_kw()))
    return 0


def read_report():
    """report.md from the work tree, or "" if it was never written. Review
    mode has no other deliverable."""
    path = os.path.join(WORK, "report.md")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def review_outcome(report):
    """(ok, kind, text) — review mode's only acceptance rule: delivered iff
    report.md is non-empty, fail:structural (kind "no_report") otherwise.
    No hidden acceptance, no verify.sh."""
    if report.strip():
        return True, None, ""
    return False, "no_report", "report.md missing or empty"


VERDICT_LINE = re.compile(r"^VERDICT: (pass|fail)\s*(?:—|–|-|:)\s*(\S.*)$")


def review_verdict(report):
    """(verdict, reason) from report.md's last line, which must be
    `VERDICT: pass — <reason>` or `VERDICT: fail — <reason>`; a missing
    report, a missing line or a malformed one is (None, why)."""
    lines = [l.strip() for l in report.splitlines() if l.strip()]
    if not lines:
        return None, "report.md missing or empty: no VERDICT line"
    m = VERDICT_LINE.match(lines[-1])
    if not m:
        return None, f"the last line of report.md is not a verdict: {lines[-1][:120]!r}"
    return m.group(1), m.group(2).strip()


def clone_review_branches():
    """Each of `review_branches` ({name, url, branch}) cloned to WORK/<name>
    before the session starts. "" on success, else why."""
    for b in TASK.get("review_branches") or []:
        d = os.path.join(WORK, b["name"])
        r = subprocess.run(["git", "clone", "-q", "--branch", b["branch"], "--single-branch",
                            with_token(b["url"]), d], capture_output=True, text=True)
        if r.returncode != 0:
            return f"clone {b['name']} ({b['branch']}) failed: " + scrub(r.stderr[-300:])
    return ""


def _main_review():
    os.makedirs(WORK, exist_ok=True)
    why = clone_review_branches()
    if why:
        return fail("env", why)
    PROGRESS.start(f"AGENT-ALIVE run {TASK.get('run')} model {TASK['llm_model']} "
                   f"envelope {MAX_CALLS} calls (review mode)\n"
                   + tag("start", model=TASK["llm_model"], calls_max=MAX_CALLS))
    home = PIHOME
    os.makedirs(home, exist_ok=True)
    claude = claude_model_id()
    if not claude:
        try:
            node, cli = fetch_runtime()
        except (urllib.error.URLError, OSError, tarfile.TarError) as e:
            return fail("env", f"runtime: {scrub(str(e))[:300]}")
        write_models_json(home)

    os.makedirs(RECORDS_DIR, exist_ok=True)
    deadline = T0 + int(TASK.get("max_seconds") or 900)
    try:
        if claude:
            killed_for, tail, err, rc = run_claude(claude, deadline, STREAM_PATH, review=True)
        else:
            killed_for, tail, err, rc = run_session(node, cli, home, deadline, STREAM_PATH, review=True)
    except OSError as e:
        return fail("env", f"{'claude' if claude else 'pi'} did not start: {e}")
    if killed_for == "calls":
        return fail("calls", f"call envelope ({MAX_CALLS}) spent\n```\n{tail[-800:]}\n```")
    if killed_for == "seconds":
        return fail("seconds", f"wall envelope spent\n```\n{tail[-800:]}\n```")
    if STATS["calls"] == 0:
        return fail("llm", f"{'claude' if claude else 'pi'} made no model call (rc {rc})\n```\n{err[-600:]}\n```")

    report = read_report()
    if TASK.get("review_branches"):
        # a judge: the outcome is the verdict on report.md's last line
        verdict, reason = review_verdict(report)
        if verdict is None:
            return fail("no-verdict", reason)
        if verdict == "fail":
            comment(f"AGENT-DONE fail (verdict): {reason}\n"
                    + done("fail", "verdict", verdict="fail", verdict_reason=reason,
                           **records_kw({"report.md": report})))
            return 1
        comment(f"AGENT-DONE ok verdict=pass calls={STATS['calls']}: {reason}\n"
                + done("ok", verdict="pass", verdict_reason=reason, **records_kw({"report.md": report})))
        return 0
    ok, kind, text = review_outcome(report)
    if not ok:
        return fail(kind, text)
    gap = ("" if STATS["asked"] == STATS["tool_calls"]
           else f" (asked {STATS['asked']}, ran {STATS['tool_calls']})")
    comment(f"AGENT-DONE ok calls={STATS['calls']} tool_calls={STATS['tool_calls']}{gap}\n"
            + done("ok", **records_kw({"report.md": report})))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — always leave a trace on the issue
        comment(crash_text(e) + "\n" + done("fail", "crash", error=type(e).__name__))
        raise
