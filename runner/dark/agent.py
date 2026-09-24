#!/usr/bin/env python3
"""dark agent — the executor. Runs INSIDE a throwaway VM, injected alone via
cloud-init with /opt/task.json. Stdlib only, self-contained by design.

Loop: clone the work repo, ask an OpenAI-compatible model for complete
replacement files (FILE:/DELETE: protocol), run the repo's own verify
script as the feedback loop, iterate within the call envelope, stage only
the paths the reply named, push the work branch. Progress goes to ONE
issue comment (AGENT-ALIVE ..., PATCHed with DARK: lines and a heartbeat
from a background thread); the verdict is ONE final AGENT-DONE comment
whose DARK: done tag the runner classifies. This script never decides an
outcome: it reports what happened and a failure kind.

The executor contract (v1 agent.py, two adversarial reviews in thread 948)
is kept verbatim where it matters: _clean_path, parse_files/parse_deletes,
guard_writes with the realpath check, tracked() via ls-files -z,
side_effects() and reply-only staging.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
TRANSCRIPT = os.environ.get("DARK_TRANSCRIPT", "/root/transcript.jsonl")
MAX_CALLS = int(TASK.get("max_calls", 6))
MAX_REASONING = int(TASK.get("max_reasoning_chars", 0) or 0)  # 0 = uncapped
MAX_STALL = int(TASK.get("max_stall", 0) or 0)  # 0 = off
HEARTBEAT = int(TASK.get("heartbeat_seconds", 60))
MAX_CONTINUATIONS = 4
SRC_EXT = (".go", ".py", ".mod", ".sum", ".md", ".txt", ".sh", ".toml",
           ".yaml", ".yml", ".json", ".cfg", ".ini")
T0 = time.time()

SYSTEM_PROMPT = """You are a careful software engineer working alone on a small repo.
You get a task and the current files. Implement exactly what the task asks,
in the repo's existing language and style, and add or update tests for the
new behavior.

Respond ONLY with the complete new content of every file you change, in
this exact format, repeated per file, with nothing else before/after:

FILE: <relative/path>
```
<entire new file content>
```

To DELETE a file, output a line on its own, outside any code block:
DELETE: <relative/path>
A MOVE is that DELETE line plus a FILE block at the new path.

Rules: output WHOLE files, never diffs or fragments. Keep existing
behavior and tests passing. Never modify anything under .gitea/ or .git/,
and never edit .dark/verify.sh. If you add a file, include it in full.
Never DELETE anything the task did not ask you to remove.
You may add NEW files freely. You may rewrite an EXISTING file only when
the task names it on a `may edit:` line — the harness refuses every
other existing-file rewrite or delete, and your attempt is sent back."""


# --- reporting ----------------------------------------------------------------
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


class Progress:
    """The one progress comment: header + DARK: lines + a heartbeat line.
    A background thread re-PATCHes it every HEARTBEAT seconds so a long
    model call still shows life; the runner reads updated_at."""

    def __init__(self):
        self.cid = None
        self.header = ""
        self.lines = []
        self.calls = 0     # replies received
        self.started = 0   # requests issued (a call in flight when the runner kills us was still served)
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def start(self, header):
        self.header = header
        self.cid = comment(self.body())
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.stopped.set()

    def body(self):
        return "\n".join([self.header, *self.lines,
                          tag("beat", at=int(time.time()), calls=self.calls, started=self.started)])

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


def record(*msgs):
    try:
        with open(TRANSCRIPT, "a") as f:
            for m in msgs:
                f.write(json.dumps(m, sort_keys=True) + "\n")
    except OSError as e:
        print(f"transcript write failed: {e}", file=sys.stderr)


def upload_transcript():
    """Attach the transcript to the issue (as .txt: Gitea's default
    ATTACHMENT.ALLOWED_TYPES rejects .jsonl). Best-effort."""
    try:
        with open(TRANSCRIPT, "rb") as f:
            body = f.read()
    except OSError:
        return
    if not body:
        return
    try:
        boundary = "darkagent" + str(len(body))
        name = f"transcript-{TASK.get('run', 'run')}.txt"
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; "
                   f'name="attachment"; filename="{name}"\r\n'
                   "Content-Type: text/plain\r\n\r\n").encode() + body + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{API}/repos/{REPO}/issues/{TASK['issue']}/assets", method="POST", data=payload,
            headers={"Authorization": f"token {TASK['token']}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"transcript upload failed: {e}", file=sys.stderr)


# --- the model ----------------------------------------------------------------
STATS = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "reasoning_chars": 0, "truncated": 0,
         "cuts": 0, "requests": 0, "distinct_calls": 0, "repeat_calls": 0, "stall_max": 0}
_sent = 0
# stall tracking (independent of MAX_STALL: always computed, whatever the cap):
# a reply is a repeat when its hash was already seen anywhere earlier in this
# run — the harness has already written that exact file set and fed back its
# verify result, so nothing new can reach the model from a call identical to it.
_STALL = {"seen": set(), "last_hash": None, "streak": 0}


def _track_stall(reply):
    """Update distinct_calls/repeat_calls/stall_max for one reply (the fully
    reassembled text the main loop parses, after continuation reassembly)."""
    h = hashlib.sha256(reply.encode()).hexdigest()
    _STALL["streak"] = _STALL["streak"] + 1 if h == _STALL["last_hash"] else 1
    STATS["stall_max"] = max(STATS["stall_max"], _STALL["streak"])
    _STALL["last_hash"] = h
    if h in _STALL["seen"]:
        STATS["repeat_calls"] += 1
    else:
        STATS["distinct_calls"] += 1
        _STALL["seen"].add(h)


class LLMFailure(Exception):
    pass


class ReasoningOver(Exception):
    pass


LAST_RESPONSE = [0.0]  # epoch of the last completed model response (see _llm_call)


def _read_stream(r, cut_at=0):
    """Reassemble an SSE chat stream into the non-streaming reply shape.
    Through the gate the executor streams (task.json llm_stream): a call that
    sits silent for its whole generation came back to a connection the gate
    found closed, twice in one run (35B round, 2026-09-02 13:06 and 13:15),
    while bytes that keep moving do not. Reasoning arrives in its own delta
    field on llama.cpp and ollama; usage in a final chunk when asked for.

    `cut_at` (characters, 0 = never) is dark's thinking level: once the
    visible thinking passes it before any answer text, reading stops (the
    closed connection cancels the generation upstream) and the reply is
    marked cut; the caller then asks for the answer with the thinking so
    far. The same cut for every provider is what makes the levels
    comparable (decision 13)."""
    content, reasoning, finish, usage, cut = [], [], None, {}, False
    n_reasoning = 0
    for raw in r:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            piece = delta.get("reasoning") or delta.get("reasoning_content")
            if piece:
                reasoning.append(piece)
                n_reasoning += len(piece)
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
        if cut_at and n_reasoning > cut_at and not content:
            cut = True
            break
    msg = {"content": "".join(content)}
    if reasoning:
        msg["reasoning"] = "".join(reasoning)
    return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage, "cut": cut}


def _hints(body, level):
    """Tell the provider dark's level in the one shape it understands
    (task.json think_api). The cut is dark's own either way; the hint only
    lets a model that can pace itself do so."""
    api = TASK.get("think_api") or "none"
    if api == "reasoning_effort":       # ollama's OpenAI endpoint: none/low/medium/high
        body["reasoning_effort"] = level
    elif api == "chat_template":        # llama.cpp: on or off, no level
        body["chat_template_kwargs"] = {"enable_thinking": level != "none"}


def _llm_call(messages):
    """One model turn: the request with dark's level (or the provider's
    default), and, when the thinking was cut at the level, one more request
    for the answer with the thinking so far. One call in the envelope, two
    provider requests, both counted."""
    level = TASK.get("think")
    response = int(TASK.get("max_tokens", 8192))
    # max_tokens is the response budget; thinking gets its own budget on top
    # because every provider counts thinking inside max_tokens and one shared
    # number starved the answer (v1's mistake, repeated until 2026-09-02: the
    # 9B's pilot replies all ended on "length")
    # a level may name its own provider model (llama.cpp preset with a native
    # reasoning budget); the re-ask uses the same one so nothing reloads
    model_id = (TASK.get("think_presets") or {}).get(level) or TASK.get("llm_model")
    body = {"model": model_id, "messages": messages}
    if level:
        chars = int(TASK.get("think_chars") or 0)
        body["max_tokens"] = response + -(-chars // int(TASK.get("chars_per_token") or 3))
        _hints(body, level)
    else:
        body["max_tokens"] = response + int(TASK.get("thinking_tokens") or 0)
    if TASK.get("temperature") is not None:
        body["temperature"] = TASK["temperature"]
    if TASK.get("llm_stream"):
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    d = _post(body, int(TASK.get("think_chars") or 0) if (level and TASK.get("llm_stream")) else 0)
    if d.get("cut"):
        partial = d["choices"][0]["message"].get("reasoning") or ""
        STATS["cuts"] += 1
        STATS["reasoning_chars"] += len(partial)
        STATS["tokens_out"] += len(partial) // int(TASK.get("chars_per_token") or 3)  # no usage on a cut stream
        note = (f"Your thinking was cut at the budget of {len(partial)} characters. Your reasoning so far:\n"
                f"<reasoning>\n{partial}\n</reasoning>\n"
                "Do not think further. Give your final answer now, in the required format.")
        body = {"model": model_id, "messages": messages + [{"role": "user", "content": note}],
                "max_tokens": response}
        _hints(body, "none")
        if TASK.get("temperature") is not None:
            body["temperature"] = TASK["temperature"]
        if TASK.get("llm_stream"):
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        d = _post(body, 0)
    return _account(d)


def _post(body, cut_at):
    req = urllib.request.Request(
        f"{TASK['llm_url']}/chat/completions", method="POST",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    tmo = int(TASK.get("llm_timeout", 600))
    last = None
    # Never open the next request within a second of closing the last one.
    # Three times on 2026-09-02 (03:28, 12:28, 12:38) the gate in front of
    # the local models answered a request that arrived milliseconds after
    # the previous response with "client gone mid-response -> cancelling",
    # llama-swap saw the upstream connection drop (502, context canceled)
    # and this executor sat in urlopen until its timeout.
    since = time.time() - LAST_RESPONSE[0]
    if since < 1.0:
        time.sleep(1.0 - since)
    for attempt in (1, 2):
        with PROGRESS.lock:
            PROGRESS.started += 1
            PROGRESS.push()
        try:
            with urllib.request.urlopen(req, timeout=tmo) as r:
                d = _read_stream(r, cut_at) if body.get("stream") else json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # one retry on a 5xx: ollama-cloud answered a third of the
            # afternoon's requests with 502 on 2026-09-02, each one a run
            # lost as fail:structural (llm)
            if e.code >= 500 and attempt == 1:
                print(f"llm HTTP {e.code}, retrying once in 5s: {detail[:120]}", file=sys.stderr)
                time.sleep(5)
                continue
            raise LLMFailure(f"HTTP {e.code}: {detail}") from None
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            last = e
            if attempt == 2 or not isinstance(getattr(e, "reason", e), TimeoutError):
                raise LLMFailure(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None
            print(f"llm timeout after {tmo}s, retrying once", file=sys.stderr)
    else:
        raise LLMFailure(repr(last))
    LAST_RESPONSE[0] = time.time()
    STATS["requests"] += 1
    return d


def _account(d):
    try:
        choice = d["choices"][0]
        m = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMFailure("reply without choices[0].message") from None
    usage = d.get("usage") or {}
    STATS["calls"] += 1
    STATS["tokens_in"] += usage.get("prompt_tokens") or 0
    STATS["tokens_out"] += usage.get("completion_tokens") or 0
    STATS["reasoning_chars"] += len(m.get("reasoning") or m.get("reasoning_content") or "")
    PROGRESS.calls = STATS["calls"]
    # the cap is an envelope, checked at the call that breaches it, not after a
    # continuation chain (949 review 2.1)
    if MAX_REASONING and STATS["reasoning_chars"] > MAX_REASONING:
        raise ReasoningOver(f"{STATS['reasoning_chars']} reasoning chars > cap {MAX_REASONING}")
    if choice.get("finish_reason") == "length":
        STATS["truncated"] += 1
    return m.get("content") or "", choice.get("finish_reason")


_LAST_REPLY = None  # the full text llm() last returned; the caller appends it
# back onto messages before its next call, so the tail-record below must skip
# it to avoid writing the same assistant reply twice


def llm(messages):
    """One turn, reassembled across continuations when the reply hit
    max_tokens. Every call is counted; the reasoning cap is checked after
    every call."""
    global _sent, _LAST_REPLY
    tail = messages[_sent:]
    if tail and tail[0].get("role") == "assistant" and tail[0].get("content") == _LAST_REPLY:
        tail = tail[1:]
    record(*tail)
    _sent = len(messages)
    content, fr = _llm_call(messages)
    record({"role": "assistant", "content": content})
    parts = [content]
    convo = list(messages)
    for cont in range(1, MAX_CONTINUATIONS + 1):
        if fr != "length" or STATS["calls"] >= MAX_CALLS:
            break
        convo = convo + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Your previous message hit the token limit and was "
             "cut off. Continue from EXACTLY where you stopped — no repetition, do not "
             "restart a file, resume mid-line if needed. End every file with its closing ``` fence."}]
        content, fr = _llm_call(convo)
        record({"role": "assistant", "content": content, "continuation": cont})
        parts.append(content)
    if MAX_REASONING and STATS["reasoning_chars"] > MAX_REASONING:
        raise ReasoningOver(f"{STATS['reasoning_chars']} reasoning chars > cap {MAX_REASONING}")
    _LAST_REPLY = "".join(parts)
    return _LAST_REPLY


# --- the executor contract (v1, reviewed) -------------------------------------
def sh(*cmd, **kw):
    return subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, **kw)


def _clean_path(path):
    """Strip leading ./ (not lstrip: a char SET), then normpath (no 'dir//x' aliasing)."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return os.path.normpath(p)


def _banned(p):
    # ':' would reach git as pathspec magic (:(glob)..., :/...), not as a path
    return p.startswith((".gitea", ".git", "/", ":")) or ".." in p or p == ".dark/verify.sh"


def parse_files(reply):
    out = {}
    for path, body in re.findall(r"FILE:\s*(\S+)\s*\n```[a-zA-Z0-9]*\n(.*?)```", reply, re.DOTALL):
        p = _clean_path(path)
        if _banned(p):
            continue
        out[p] = body if body.endswith("\n") else body + "\n"
    return out


def parse_deletes(reply):
    out = []
    for m in re.findall(r"^DELETE:\s*(\S+)\s*$", reply, re.MULTILINE):
        p = _clean_path(m)
        if not _banned(p):
            out.append(p)
    return out


def guard_writes(files, deletes, existing, grant, root=None):
    """Rewrite/delete an existing file only under the may_edit grant; with
    root, symlink paths are refused too. Returns (files, deletes, refused)."""
    grant = set(grant or [])
    ref = [p for p in files if p in existing and p not in grant]
    ref += [p for p in deletes if p in existing and p not in grant]
    ref += [p for p in [*files, *deletes] if root and p not in ref and
            os.path.realpath(f"{root}/{p}") != f"{os.path.realpath(root)}/{p}"]
    if ref:
        return {}, [], ref
    return files, deletes, []


def tracked():
    return set(sh("git", "ls-files", "-z").stdout.split("\0")) - {""}


def current_files():
    names = [n for n in sorted(tracked()) if n.endswith(SRC_EXT) and not n.startswith(".git")]
    blocks = []
    for n in names:
        try:
            with open(f"{WORK}/{n}") as f:
                blocks.append(f"FILE: {n}\n```\n{f.read()}```")
        except (OSError, UnicodeDecodeError):
            pass
    return "\n\n".join(blocks)


def side_effects(files, deletes):
    """Tracked changes the reply did not name (verify.sh ran model-written
    tests with write access): staged only by name, anything else fails loud."""
    st = sh("git", "status", "--porcelain", "-uno", "-z", "--no-renames").stdout
    return [l[3:] for l in st.split("\0") if l and l[3:] not in files and l[3:] not in deletes]


def verify():
    script = f"{WORK}/.dark/verify.sh"
    if os.path.exists(script):
        r = sh("bash", ".dark/verify.sh")
    else:
        r = sh("python3", "-m", "unittest", "discover", "-v")
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


# --- the run ------------------------------------------------------------------
def done(outcome, kind=None, **kw):
    fields = dict(outcome=outcome, calls=STATS["calls"], tokens_in=STATS["tokens_in"],
                  tokens_out=STATS["tokens_out"], reasoning_chars=STATS["reasoning_chars"],
                  seconds=int(time.time() - T0), truncated=STATS["truncated"], cuts=STATS["cuts"],
                  requests=STATS["requests"], distinct_calls=STATS["distinct_calls"],
                  repeat_calls=STATS["repeat_calls"], stall_max=STATS["stall_max"], **kw)
    if kind:
        fields["kind"] = kind
    return tag("done", **fields)


def fail(kind, text, **kw):
    comment(f"AGENT-DONE fail ({kind}): {text}\n" + done("fail", kind, **kw))
    return 1


def clone_url():
    """Where to clone/push: task.git_url (default: the Gitea base), with the
    agent token injected for http(s)."""
    base = TASK.get("git_url") or TASK["gitea"]
    if base.startswith("http"):
        base = base.replace("://", f"://dark-agent:{TASK['token']}@", 1)
    return f"{base}/{REPO}.git"


def main():
    try:
        return _main()
    finally:
        PROGRESS.stop()


def _main():
    if shutil.which("git") is None:
        return fail("env", "no `git` in this VM image (wrong template?)")
    r = subprocess.run(["git", "clone", "-q", clone_url(), WORK], capture_output=True, text=True)
    if r.returncode != 0:
        return fail("env", "clone failed: " + scrub(r.stderr[-300:]))
    sh("git", "config", "user.name", "dark-agent")
    sh("git", "config", "user.email", "dark-agent@localhost")
    sh("git", "switch", "-qc", TASK["branch"])

    PROGRESS.start(f"AGENT-ALIVE run {TASK.get('run')} model {TASK['llm_model']} "
                   f"envelope {MAX_CALLS} calls\n"
                   + tag("start", model=TASK["llm_model"], calls_max=MAX_CALLS))
    grant = TASK.get("may_edit") or []
    user = f"Task:\n{TASK.get('spec', '')}\n\n"
    if grant:
        user += "may edit: " + ", ".join(grant) + "\n\n"
    user += f"Current files:\n\n{current_files()}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    it = 0
    last_fail = "no verify run"
    parsed_any = False
    # every path written or deleted over ALL iterations: the reply that turns
    # verify.sh green names only what it changed last (semver-go on dsf, 949
    # pilot: iteration 2 sent semver_test.go alone, semver.go from iteration 1
    # never reached the branch and staging saw "undefined: CompareSemver")
    written, deleted = {}, set()
    while STATS["calls"] < MAX_CALLS:
        it += 1
        try:
            reply = llm(messages)
        except ReasoningOver as e:
            return fail("reasoning", str(e), iter=it)
        except LLMFailure as e:
            return fail("llm", str(e), iter=it, error=str(e)[:200])
        _track_stall(reply)
        if MAX_STALL and STATS["repeat_calls"] >= MAX_STALL:
            return fail("stall", f"reply repeated one already seen ({STATS['repeat_calls']} "
                        f"repeat(s), cap {MAX_STALL}): no new information could reach the model",
                        iter=it)
        files = parse_files(reply)
        deletes = parse_deletes(reply)
        parsed_any = parsed_any or bool(files or deletes)
        files, deletes, refused = guard_writes(files, deletes, tracked(), grant, WORK)
        if refused:
            PROGRESS.add(tag("refused", paths=refused, iter=it))
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content":
                          "Refused rewrite/delete of existing file(s) the task did not grant: "
                          + ", ".join(refused) + ". Only a file named on a `may edit:` line may "
                          "be rewritten. Output corrected files."}]
            last_fail = f"ungranted existing-file write refused: {refused}"
            continue
        if not files and not deletes:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "No FILE blocks found. Use the exact format."}]
            last_fail = "reply had no FILE blocks"
            continue
        # deletes first, and never of a path the same reply also writes: a
        # "DELETE: x" + "FILE: x" pair is a rewrite (qwen-3.6-35b on
        # csvstat-python, 949 bench: the delete ran after the write, popped
        # the path from `written`, and the commit found nothing staged)
        for path in deletes:
            if path in files:
                continue
            sh("git", "rm", "-q", "--ignore-unmatch", path)
            deleted.add(path)
            written.pop(path, None)
        for path, body in files.items():
            os.makedirs(os.path.dirname(f"{WORK}/{path}") or WORK, exist_ok=True)
            with open(f"{WORK}/{path}", "w") as f:
                f.write(body)
            written[path] = True
            deleted.discard(path)
        ok, out = verify()
        PROGRESS.add(tag("verify", ok=ok, iter=it, calls=STATS["calls"]))
        if not ok:
            last_fail = out
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": f"verify.sh failed:\n```\n{out}\n```\n"
                          "Fix and output complete corrected files, same format."}]
            continue
        if written:
            add = sh("git", "add", "--", *sorted(written))  # never -A: build products and test side effects stay out
            if add.returncode != 0:
                return fail("env", "git add refused: " + add.stderr[-300:], iter=it)
        dirty = side_effects(written, deleted)
        if dirty:
            return fail("side-effect", f"verify.sh changed tracked file(s) the replies did not name: {dirty}",
                        iter=it)
        commit = sh("git", "commit", "-qm", f"dark: {TASK.get('task', 'task')} ({TASK.get('run', 'run')})")
        if commit.returncode != 0:
            return fail("env", "git commit failed: " + (commit.stdout + commit.stderr)[-300:], iter=it)
        sh("git", "push", "-q", "origin", "--delete", TASK["branch"])
        push = sh("git", "push", "-q", "origin", f"HEAD:{TASK['branch']}")
        if push.returncode != 0:
            return fail("push", "push failed: " + scrub(push.stderr[-600:]), iter=it)
        comment(f"AGENT-DONE ok iter={it} calls={STATS['calls']} files={','.join(sorted(written))} "
                f"deletes={','.join(sorted(deleted))} branch={TASK['branch']}\n"
                + done("ok", iter=it, files=sorted(written), deletes=sorted(deleted), branch=TASK["branch"]))
        return 0
    return fail("calls" if parsed_any else "no_blocks",
                f"call envelope ({MAX_CALLS}) spent\n```\n{last_fail[-800:]}\n```", iter=it)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — always leave a trace on the issue
        fn = getattr(e, "filename", None)
        comment(f"AGENT-DONE fail (crash): {e!r}" + (f" (file: {fn})" if fn else "") + "\n"
                + done("fail", "crash", error=type(e).__name__))
        raise
    finally:
        upload_transcript()
