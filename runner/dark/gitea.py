"""dark/gitea.py — the runner's Gitea client: the queue and the audit
trail (issues, comments), repos and branches. Token from a file on the
runner host; never from the environment of a VM (the VM gets the
lower-scoped agent token inside task.json).
"""

import datetime as _dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class GiteaError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"gitea {code}: {detail}")
        self.code = code
        self.detail = detail


def read_token(path):
    """The token file's first line, or "" (preflight reports the miss)."""
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def parse_time(s):
    """Gitea timestamps (RFC 3339, with Z or an offset) -> epoch float."""
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Gitea:
    def __init__(self, url, token, lan_url=None, timeout=30):
        self.url = url.rstrip("/")
        self.lan_url = (lan_url or url).rstrip("/")
        self.token = token
        self.timeout = timeout

    def api(self, path, method="GET", data=None):
        req = urllib.request.Request(
            f"{self.url}/api/v1{path}", method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"token {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise GiteaError(e.code, e.read().decode(errors="replace")[:300]) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GiteaError(0, f"{method} {path}: {getattr(e, 'reason', e)}") from None

    # --- health ----------------------------------------------------------------
    def version(self):
        return self.api("/version").get("version", "")

    def whoami(self):
        return self.api("/user").get("login", "")

    # --- orgs and repos --------------------------------------------------------
    def org_exists(self, org):
        try:
            self.api(f"/orgs/{org}")
            return True
        except GiteaError as e:
            if e.code == 404:
                return False
            raise

    def create_org(self, org):
        return self.api("/orgs", "POST", {"username": org, "visibility": "public"})

    def write_team(self, org, user):
        """(ok, detail): does a team in `org` give `user` write access to
        every repository of the org, the ones created later included?

        The executor VM pushes its run branch with the agent token, and the
        work repos are created per run, so without such a team every run of
        a fresh work org gets through the model call and dies on the push.
        That is what happened on the first smoke run against the new
        `dark-runs` org (2026-09-03): "User permission denied for writing"
        after a green verify."""
        try:
            teams = self.api(f"/orgs/{org}/teams")
        except GiteaError as e:
            return False, f"teams of {org}: {e}"
        for t in teams:
            # a team with per-unit permissions reports permission "none" and
            # carries the real grant in units_map; the push needs repo.code
            perm = t.get("permission")
            if perm in (None, "none", "read"):
                perm = (t.get("units_map") or {}).get("repo.code")
            if perm not in ("write", "admin", "owner") or not t.get("includes_all_repositories"):
                continue
            try:
                members = [m.get("login") for m in self.api(f"/teams/{t['id']}/members")]
            except GiteaError:
                continue
            if user in members:
                return True, f"team {t.get('name')!r} gives {user} {perm} on the code of every repo"
        return False, (f"no team in {org} gives {user} write on every repo of it "
                       f"(ops/gitea-bootstrap.sh --work-org {org})")

    def repo_exists(self, full):
        try:
            self.api(f"/repos/{full}")
            return True
        except GiteaError as e:
            if e.code == 404:
                return False
            raise

    def repo_here(self, full):
        """(ok, why): is `full` a live repository of that org right now?

        Not the same question as "does the API answer on this path". A repo
        moved out by `dark archive-work` leaves a redirect behind, the API
        follows it, and the runner read that as "the repo is there": the next
        round materialized nothing and pushed into the archived copy, so
        every task was blocked with a 403 (2026-09-03). `why` is missing,
        moved, or archived; creating over a redirect is allowed and drops
        it, creating over an archived repo of the same name is not."""
        try:
            r = self.api(f"/repos/{full}")
        except GiteaError as e:
            if e.code == 404:
                return False, "missing"
            raise
        if (r.get("full_name") or "").lower() != full.lower():
            return False, "moved"
        if r.get("archived"):
            return False, "archived"
        return True, ""

    def create_repo(self, org, name, default_branch="main", description=""):
        return self.api(f"/orgs/{org}/repos", "POST",
                        {"name": name, "default_branch": default_branch,
                         "description": description, "auto_init": False})

    def push_url(self, full, user="dark-admin"):
        return f"{self.url.replace('://', f'://{user}:{self.token}@', 1)}/{full}.git"

    def branch_exists(self, full, branch):
        try:
            self.api(f"/repos/{full}/branches/{urllib.parse.quote(branch, safe='')}")
            return True
        except GiteaError as e:
            if e.code == 404:
                return False
            raise

    def delete_branch(self, full, branch):
        try:
            self.api(f"/repos/{full}/branches/{urllib.parse.quote(branch, safe='')}", "DELETE")
        except GiteaError as e:
            if e.code != 404:
                raise

    # --- issues and comments ---------------------------------------------------
    def issue_create(self, full, title, body):
        return self.api(f"/repos/{full}/issues", "POST", {"title": title, "body": body})["number"]

    def issue_close(self, full, n, comment=None):
        if comment:
            self.comment(full, n, comment)
        self.api(f"/repos/{full}/issues/{n}", "PATCH", {"state": "closed"})

    def comments(self, full, n):
        out = []
        page = 1
        while True:
            batch = self.api(f"/repos/{full}/issues/{n}/comments?page={page}&limit=50")
            if not batch:
                return out
            out.extend(batch)
            if len(batch) < 50:
                return out
            page += 1

    def comment(self, full, n, body):
        return self.api(f"/repos/{full}/issues/{n}/comments", "POST", {"body": body})["id"]

    def edit_comment(self, full, cid, body):
        self.api(f"/repos/{full}/issues/comments/{cid}", "PATCH", {"body": body})

    def open_issues(self, full, labels=None):
        q = "state=open&limit=50" + (f"&labels={urllib.parse.quote(','.join(labels))}" if labels else "")
        out, page = [], 1
        while True:
            batch = self.api(f"/repos/{full}/issues?{q}&page={page}")
            if not batch:
                return out
            out.extend(batch)
            if len(batch) < 50:
                return out
            page += 1

    def archive_repo(self, full):
        self.api(f"/repos/{full}", "PATCH", {"archived": True})

    def repos_of_org(self, org):
        """Every repo name in `org`, paged."""
        out, page = [], 1
        while True:
            batch = self.api(f"/orgs/{org}/repos?limit=50&page={page}")
            out.extend(r["name"] for r in batch)
            if len(batch) < 50:
                return out
            page += 1

    def transfer_repo(self, full, new_owner):
        """Move `full` to `new_owner`; a transfer Gitea leaves pending (202)
        is accepted at once with the same admin token."""
        r = self.api(f"/repos/{full}/transfer", "POST", {"new_owner": new_owner})
        name = full.split("/", 1)[1]
        if isinstance(r, dict) and r.get("repo_transfer") and r["repo_transfer"].get("doer") is not None:
            self.api(f"/repos/{new_owner}/{name}/transfer/accept", "POST", {})
        return f"{new_owner}/{name}"
