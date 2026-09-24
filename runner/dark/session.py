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
RT = os.environ.get("DARK_RT", "/opt/rt")
SESSION_DIR = "/opt/pisession"
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

# review mode: no repo, no branch, no verify.sh - the files
# to read are already staged under {work}, and the only deliverable is
# report.md there.
REVIEW_BRIEF = """You are reviewing material in the directory {work}. Read the files listed below, already present in that directory, and write your findings to report.md in {work}. That file is your only deliverable: do not modify any other file, and do not create a git repository.

Files to read: {files}

BRIEF:
{spec}
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


def clone_url(repo=None):
    base = TASK.get("git_url") or TASK["gitea"]
    if base.startswith("http"):
        base = base.replace("://", f"://dark-agent:{TASK['token']}@", 1)
    return f"{base}/{repo or REPO}.git"


# --- records: pi's stream, the brief and task.json, kept ----------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def push_records(extra_files=None):
    """Clone the records repo, add stream.jsonl / brief.md / task.json (and,
    in review mode, report.md) under RUN_ID/, commit and push. Never force:
    other runs of the same shift have their own RUN_ID directory in the same
    repo. (ok, path-or-error)."""
    repo = TASK.get("records_repo")
    if not repo:
        return False, "no records_repo in task.json"
    shutil.rmtree(RECORDS_WORK, ignore_errors=True)
    r = subprocess.run(["git", "clone", "-q", clone_url(repo), RECORDS_WORK],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, scrub(r.stderr[-300:])

    def rsh(*cmd):
        return subprocess.run(["git", *cmd], cwd=RECORDS_WORK, capture_output=True, text=True)

    rsh("config", "user.name", "dark-session")
    rsh("config", "user.email", "dark-session@localhost")
    rsh("checkout", "-q", "-B", "main")  # a repo created empty has no branch to land on yet
    run_dir = os.path.join(RECORDS_WORK, RUN_ID)
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
    rsh("add", "-A")
    c = rsh("commit", "-qm", f"records: {RUN_ID}")
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        return False, scrub((c.stdout + c.stderr)[-300:])
    push = rsh("push", "-q", "origin", "HEAD:main")
    if push.returncode != 0:
        rsh("pull", "-q", "--rebase", "origin", "main")
        push = rsh("push", "-q", "origin", "HEAD:main")
    if push.returncode != 0:
        return False, scrub(push.stderr[-300:])
    return True, f"{repo}/{RUN_ID}"


def records_kw(extra_files=None):
    """{"records": ..., "records_sha256": ...} for a done/fail tag. A push
    failure never raises and never changes the run's outcome; it only says
    so in the field the runner reads."""
    try:
        ok, info = push_records(extra_files)
    except Exception as e:  # noqa: BLE001 — records must never crash the run
        ok, info = False, f"{type(e).__name__}: {e}"
    if ok:
        return {"records": info, "records_sha256": sha256_file(STREAM_PATH)}
    return {"records": f"PUSH FAILED: {info}"}


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


def run_session(node, cli, home, deadline, stream_path, review=False):
    env = dict(os.environ, HOME=home, PI_OFFLINE="1", NO_COLOR="1", TERM="dumb")
    if review:
        brief = REVIEW_BRIEF.format(work=WORK, spec=SPEC_TEXT,
                                    files=", ".join(TASK.get("review_files") or []))
    else:
        grant = TASK.get("may_edit") or []
        brief = BRIEF.format(
            work=WORK, lang=TASK.get("lang") or "software", spec=SPEC_TEXT,
            grant=("Existing files you may rewrite: " + ", ".join(grant) + ". Any other existing file "
                   "must stay as it is.\n" if grant else "Add new files only; do not rewrite an "
                   "existing file unless the task says so.\n"))
    cmd = [node, cli, "--provider", "dark", "--model", TASK["llm_model"], "--api-key", "unused",
           "--mode", "json", "--session-dir", SESSION_DIR, "--no-context-files", "--no-extensions",
           "--no-skills", "--no-prompt-templates", "--approve", "-p", brief]
    lvl = THINK.get(TASK.get("think") or "")
    if lvl:
        cmd += ["--thinking", lvl]
    proc = subprocess.Popen(cmd, cwd=WORK, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    killed_for, tail = read_stream(proc, deadline, stream_path)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = (proc.stderr.read() or "")[-1200:] if proc.stderr else ""
    return killed_for, tail, err, proc.returncode


def verify():
    if os.path.exists(f"{WORK}/.dark/verify.sh"):
        r = sh("bash", ".dark/verify.sh")
    else:
        r = sh("python3", "-m", "unittest", "discover", "-v")
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


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
    r = subprocess.run(["git", "clone", "-q", clone_url(), WORK], capture_output=True, text=True)
    if r.returncode != 0:
        return fail("env", "clone failed: " + scrub(r.stderr[-300:]))
    sh("git", "config", "user.name", "dark-session")
    sh("git", "config", "user.email", "dark-session@localhost")
    sh("git", "switch", "-qc", TASK["branch"])
    base_sha = sh("git", "rev-parse", "HEAD").stdout.strip()

    PROGRESS.start(f"AGENT-ALIVE run {TASK.get('run')} model {TASK['llm_model']} "
                   f"envelope {MAX_CALLS} calls (session arm)\n"
                   + tag("start", model=TASK["llm_model"], calls_max=MAX_CALLS))
    home = "/opt/pihome"
    os.makedirs(home, exist_ok=True)
    try:
        node, cli = fetch_runtime()
    except (urllib.error.URLError, OSError, tarfile.TarError) as e:
        return fail("env", f"runtime: {scrub(str(e))[:300]}")
    write_models_json(home)

    os.makedirs(RECORDS_DIR, exist_ok=True)
    deadline = T0 + int(TASK.get("max_seconds") or 900)
    try:
        killed_for, tail, err, rc = run_session(node, cli, home, deadline, STREAM_PATH)
    except OSError as e:
        return fail("env", f"pi did not start: {e}")
    ok, out = verify()
    PROGRESS.add(tag("verify", ok=ok, iter=STATS["calls"], calls=STATS["calls"]))

    # whatever the session left is the delivery, exactly as a chat arm hands
    # in its work tree: there is no reply protocol to stage by, so add
    # everything. The brief asks the session to commit, and it usually has, so
    # this commit is for what it left uncommitted and a "nothing to commit"
    # here is not a session that did nothing: only a HEAD that never moved is.
    sh("git", "add", "-A")
    sh("git", "commit", "-qm",
       f"dark session: {TASK.get('task', 'task')} ({TASK.get('run', 'run')})")
    head_sha = sh("git", "rev-parse", "HEAD").stdout.strip()
    if killed_for == "calls":
        return fail("calls", f"call envelope ({MAX_CALLS}) spent\n```\n{tail[-800:]}\n```")
    if killed_for == "seconds":
        return fail("seconds", f"wall envelope spent\n```\n{tail[-800:]}\n```")
    if STATS["calls"] == 0:
        return fail("llm", f"pi made no model call (rc {rc})\n```\n{err[-600:]}\n```")
    if not ok:
        return fail("calls", f"verify.sh red when the session ended\n```\n{out[-800:]}\n```")
    if head_sha == base_sha:
        return fail("no_blocks", "the session left no commit and no change")
    sh("git", "push", "-q", "origin", "--delete", TASK["branch"])
    push = sh("git", "push", "-q", "origin", f"HEAD:{TASK['branch']}")
    if push.returncode != 0:
        return fail("push", "push failed: " + scrub(push.stderr[-600:]))
    gap = ("" if STATS["asked"] == STATS["tool_calls"]
           else f" (asked {STATS['asked']}, ran {STATS['tool_calls']})")
    comment(f"AGENT-DONE ok calls={STATS['calls']} tool_calls={STATS['tool_calls']}{gap} "
            f"branch={TASK['branch']}\n" + done("ok", branch=TASK["branch"], **records_kw()))
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


def _main_review():
    os.makedirs(WORK, exist_ok=True)
    PROGRESS.start(f"AGENT-ALIVE run {TASK.get('run')} model {TASK['llm_model']} "
                   f"envelope {MAX_CALLS} calls (review mode)\n"
                   + tag("start", model=TASK["llm_model"], calls_max=MAX_CALLS))
    home = "/opt/pihome"
    os.makedirs(home, exist_ok=True)
    try:
        node, cli = fetch_runtime()
    except (urllib.error.URLError, OSError, tarfile.TarError) as e:
        return fail("env", f"runtime: {scrub(str(e))[:300]}")
    write_models_json(home)

    os.makedirs(RECORDS_DIR, exist_ok=True)
    deadline = T0 + int(TASK.get("max_seconds") or 900)
    try:
        killed_for, tail, err, rc = run_session(node, cli, home, deadline, STREAM_PATH, review=True)
    except OSError as e:
        return fail("env", f"pi did not start: {e}")
    if killed_for == "calls":
        return fail("calls", f"call envelope ({MAX_CALLS}) spent\n```\n{tail[-800:]}\n```")
    if killed_for == "seconds":
        return fail("seconds", f"wall envelope spent\n```\n{tail[-800:]}\n```")
    if STATS["calls"] == 0:
        return fail("llm", f"pi made no model call (rc {rc})\n```\n{err[-600:]}\n```")

    report = read_report()
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
        comment(f"AGENT-DONE fail (crash): {e!r}\n" + done("fail", "crash", error=type(e).__name__))
        raise
