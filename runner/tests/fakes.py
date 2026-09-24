"""Test doubles: an in-memory Gitea over HTTP (the API subset the runner,
agent and stager use), a scripted OpenAI-compatible model server, and
git fixtures (bare origins on disk, cloned over file://)."""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Server:
    def __init__(self, handler):
        self.srv = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


class FakeGitea:
    """State lives on the instance; the handler class is made per instance."""

    def __init__(self):
        self.orgs = set()
        self.repos = {}        # full -> {"archived": bool, "branches": set()}
        # old full name -> new one: what a transfer leaves behind, and what
        # the API answers on (urllib follows it, as Gitea sends it)
        self.redirects = {}
        self.issues = {}       # full -> {n: {"title","body","state","comments":[...]}}
        self.assets = []       # (full, n, filename, size)
        self.next_comment = 1
        self.calls = []        # (method, path)
        self.fail_next = None  # (code, body) to fail the next request with
        # org -> [{"id","name","permission","includes_all_repositories","members"}]
        self.teams = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if (self.headers.get("Content-Type") or "").startswith("multipart"):
                    return raw
                return json.loads(raw or b"{}")

            def route(self, method):
                path, _, query = self.path.partition("?")
                fake.calls.append((method, path))
                if fake.fail_next:
                    code, body = fake.fail_next
                    fake.fail_next = None
                    return self._send(code, {"message": body})
                p = path[len("/api/v1"):] if path.startswith("/api/v1") else path
                m = re.match(r"^/orgs/([^/]+)/teams$", p)
                if m and method == "GET":
                    return self._send(200, [{k: v for k, v in t.items() if k != "members"}
                                            for t in fake.teams.get(m.group(1), [])])
                m = re.match(r"^/teams/(\d+)/members$", p)
                if m and method == "GET":
                    tid = int(m.group(1))
                    for ts in fake.teams.values():
                        for t in ts:
                            if t["id"] == tid:
                                return self._send(200, [{"login": u} for u in t.get("members", [])])
                    return self._send(404, {"message": "no team"})
                m = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/comments$", p)
                if m and method == "POST":
                    full, n = m.group(1), int(m.group(2))
                    c = {"id": fake.next_comment, "body": self._body()["body"],
                         "created_at": _iso(), "updated_at": _iso()}
                    fake.next_comment += 1
                    fake.issues.setdefault(full, {}).setdefault(n, {"comments": []})["comments"].append(c)
                    return self._send(201, {"id": c["id"]})
                if m and method == "GET":
                    full, n = m.group(1), int(m.group(2))
                    cs = fake.issues.get(full, {}).get(n, {}).get("comments", [])
                    page = int(dict(x.split("=") for x in query.split("&") if "=" in x).get("page", 1))
                    return self._send(200, cs[(page - 1) * 50: page * 50])
                m = re.match(r"^/repos/([^/]+/[^/]+)/issues/comments/(\d+)$", p)
                if m and method == "PATCH":
                    full, cid = m.group(1), int(m.group(2))
                    for iss in fake.issues.get(full, {}).values():
                        for c in iss["comments"]:
                            if c["id"] == cid:
                                c["body"] = self._body()["body"]
                                c["updated_at"] = _iso()
                                return self._send(200, {"id": cid})
                    return self._send(404, {"message": "no comment"})
                m = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/assets$", p)
                if m and method == "POST":
                    raw = self._body()
                    fn = re.search(rb'filename="([^"]+)"', raw)
                    fake.assets.append((m.group(1), int(m.group(2)), fn.group(1).decode() if fn else "", len(raw)))
                    return self._send(201, {"id": len(fake.assets)})
                m = re.match(r"^/repos/([^/]+/[^/]+)/issues$", p)
                if m and method == "POST":
                    full = m.group(1)
                    d = self._body()
                    iss = fake.issues.setdefault(full, {})
                    n = max(iss, default=0) + 1
                    iss[n] = {"title": d["title"], "body": d.get("body", ""), "state": "open", "comments": []}
                    return self._send(201, {"number": n})
                if m and method == "GET":
                    full = m.group(1)
                    q = dict(x.split("=") for x in query.split("&") if "=" in x)
                    page = int(q.get("page", 1))
                    items = [{"number": n, "title": i["title"], "state": i["state"]}
                             for n, i in sorted(fake.issues.get(full, {}).items())
                             if i.get("state", "open") == q.get("state", "open")]
                    return self._send(200, items[(page - 1) * 50: page * 50])
                m = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)$", p)
                if m and method == "PATCH":
                    full, n = m.group(1), int(m.group(2))
                    fake.issues[full][n]["state"] = self._body().get("state", "open")
                    return self._send(200, {"number": n})
                m = re.match(r"^/repos/([^/]+/[^/]+)/branches/(.+)$", p)
                if m:
                    full, br = m.group(1), m.group(2).replace("%2F", "/")
                    repo = fake.repos.get(full)
                    if repo is None or br not in repo["branches"]:
                        return self._send(404, {"message": "no branch"})
                    if method == "DELETE":
                        repo["branches"].discard(br)
                        return self._send(204)
                    return self._send(200, {"name": br})
                m = re.match(r"^/repos/([^/]+/[^/]+)/transfer$", p)
                if m and method == "POST":
                    old = m.group(1)
                    if old not in fake.repos:
                        return self._send(404, {"message": "no repo"})
                    new = f"{self._body()['new_owner']}/{old.split('/', 1)[1]}"
                    fake.repos[new] = fake.repos.pop(old)
                    if old in fake.issues:
                        fake.issues[new] = fake.issues.pop(old)
                    fake.redirects[old] = new
                    return self._send(202, {"full_name": new})
                m = re.match(r"^/repos/([^/]+/[^/]+)$", p)
                if m and method == "GET":
                    full = fake.redirects.get(m.group(1), m.group(1))
                    if full in fake.repos:
                        return self._send(200, {"full_name": full, **fake.repos[full]
                                                 | {"branches": sorted(fake.repos[full]["branches"])}})
                    return self._send(404, {"message": "no repo"})
                if m and method == "PATCH":
                    fake.repos[m.group(1)].update({k: v for k, v in self._body().items() if k == "archived"})
                    return self._send(200, {})
                m = re.match(r"^/orgs/([^/]+)/repos(?:\?.*)?$", p)
                if m and method == "GET":
                    org = m.group(1)
                    page = int((re.search(r"page=(\d+)", p) or [None, "1"])[1])
                    names = sorted(f for f in fake.repos if f.startswith(org + "/"))
                    return self._send(200, [{"name": f.split("/", 1)[1], "full_name": f}
                                            for f in names[(page - 1) * 50:page * 50]])
                m = re.match(r"^/orgs/([^/]+)/repos$", p)
                if m and method == "POST":
                    d = self._body()
                    full = f"{m.group(1)}/{d['name']}"
                    if full in fake.repos:
                        return self._send(409, {"message": "exists"})
                    fake.redirects.pop(full, None)  # creating the name drops the redirect
                    fake.repos[full] = {"archived": False, "branches": set()}
                    return self._send(201, {"full_name": full})
                m = re.match(r"^/orgs/([^/]+)$", p)
                if m and method == "GET":
                    return self._send(200 if m.group(1) in fake.orgs else 404, {"username": m.group(1)})
                if p == "/orgs" and method == "POST":
                    fake.orgs.add(self._body()["username"])
                    return self._send(201, {})
                if p == "/version":
                    return self._send(200, {"version": "fake-1.26"})
                if p == "/user":
                    auth = self.headers.get("Authorization", "")
                    if auth == "token good":
                        return self._send(200, {"login": "dark-admin"})
                    return self._send(401, {"message": "bad token"})
                return self._send(404, {"message": f"unrouted {method} {p}"})

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

            def do_PATCH(self):
                self.route("PATCH")

            def do_DELETE(self):
                self.route("DELETE")

        self.server = _Server(H)
        self.url = self.server.url

    def close(self):
        self.server.close()

    def comments(self, full, n):
        return self.issues.get(full, {}).get(n, {}).get("comments", [])

    def bodies(self, full, n):
        return [c["body"] for c in self.comments(full, n)]


class FakeLLM:
    """Replies are popped in order: each is a dict with content and optional
    finish_reason, reasoning, tokens_in, tokens_out, status (HTTP code)."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.requests = []
        self.streamed = []  # stream_options of every streamed request
        self.models = ["a", "b"]
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self._send(200, {"data": [{"id": m} for m in fake.models]})
                if self.path.endswith("/healthz"):
                    return self._send(200, {"ok": True, "key": True, "models": fake.models})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append(body)
                if not fake.replies:
                    return self._send(500, {"error": "no scripted reply left"})
                r = fake.replies.pop(0)
                if r.get("status"):
                    return self._send(r["status"], {"error": r.get("content", "scripted failure")})
                if r.get("sleep"):
                    time.sleep(r["sleep"])
                msg = {"content": r.get("content", "")}
                if r.get("reasoning"):
                    msg["reasoning"] = r["reasoning"]
                if body.get("stream"):
                    # the OpenAI SSE shape: content in pieces, reasoning in its
                    # own delta field, finish_reason on the last content chunk,
                    # usage in a final chunk when asked for, then [DONE]
                    fake.streamed.append(body.get("stream_options"))
                    content = msg["content"]
                    chunks = []
                    if msg.get("reasoning"):
                        chunks.append({"choices": [{"delta": {"reasoning": msg["reasoning"]}, "finish_reason": None}]})
                    pieces = [content[i:i + 7] for i in range(0, len(content), 7)] or [""]
                    for i, p in enumerate(pieces):
                        last = i == len(pieces) - 1
                        chunks.append({"choices": [{"delta": {"content": p},
                                                    "finish_reason": r.get("finish_reason", "stop") if last else None}]})
                    chunks.append({"choices": [], "usage": {"prompt_tokens": r.get("tokens_in", 10),
                                                            "completion_tokens": r.get("tokens_out", 5)}})
                    payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                    data = payload.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._send(200, {
                    "choices": [{"message": msg, "finish_reason": r.get("finish_reason", "stop")}],
                    "usage": {"prompt_tokens": r.get("tokens_in", 10),
                              "completion_tokens": r.get("tokens_out", 5)}})

        self.server = _Server(H)
        self.url = self.server.url

    def close(self):
        self.server.close()


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r


def make_origin(base_dir, full, files, branch="main"):
    """A bare repo at <base_dir>/<full>.git seeded with `files` on `branch`.
    Returns the file:// base URL the agent/stager can use as git_url."""
    bare = os.path.join(base_dir, f"{full}.git")
    os.makedirs(os.path.dirname(bare), exist_ok=True)
    git("init", "-q", "--bare", "-b", branch, bare)
    work = os.path.join(base_dir, "seed-" + full.replace("/", "-"))
    git("clone", "-q", bare, work)
    for path, content in files.items():
        os.makedirs(os.path.dirname(os.path.join(work, path)) or work, exist_ok=True)
        with open(os.path.join(work, path), "w") as f:
            f.write(content)
    git("add", "-A", cwd=work)
    git("-c", "user.name=seed", "-c", "user.email=seed@x", "commit", "-qm", "seed", cwd=work)
    git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)
    return f"file://{base_dir}"


def branch_files(base_dir, full, branch):
    bare = os.path.join(base_dir, f"{full}.git")
    r = git("ls-tree", "-r", "--name-only", branch, cwd=bare, check=False)
    return set(r.stdout.split()) if r.returncode == 0 else None


def branch_file(base_dir, full, branch, path):
    bare = os.path.join(base_dir, f"{full}.git")
    return git("show", f"{branch}:{path}", cwd=bare).stdout
