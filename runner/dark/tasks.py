"""dark/tasks.py — task records on disk and their materialisation as a
work repo (design §2, §7).

    tasks/<id>/task.toml        id, title, class, lang, spec, may_edit
    tasks/<id>/start/           overlay on the language template (optional)
    tasks/<id>/acceptance/      hidden: run.sh plus fixtures; never in the executor VM
    tasks/<id>/oracle/          reference overlay, used only to validate acceptance

The work repo for a task is dark/t-<id>: the template for its language plus
start/, force-pushed to main before every run so each run starts from the
same tree. Run branches are run/<run id>.

Chains (decision 17, 950): a task may name the task it follows (`after`).
Its starting tree is then the tree that task delivered (a run branch, when
the shift has a pass for it) or that task's oracle tree, plus its own
start/. The chain's order is the sorted order of ids, so `after` must sort
before the task. oracle/.delete lists paths the reference solution removes.
"""

import base64
import io
import os
import re
import shutil
import subprocess
import tarfile
import time
import tomllib
from dataclasses import dataclass

from . import spec

LANGS = ("go", "python")
# the work repo of task <id> is <REPO_PREFIX><id>. Two arms on the same
# task at once force-push each other's starting tree (the bench's session
# arms and a shift share dark/t-<id>), so an arm that runs beside a shift
# names its own prefix: DARK_REPO_PREFIX=session-dsf- (chain.sh does).
REPO_PREFIX = os.environ.get("DARK_REPO_PREFIX", "t-")


class TaskError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    cls: str
    lang: str
    spec: str
    may_edit: tuple
    dir: str
    stage_timeout: int
    after: str | None = None

    @property
    def repo_name(self):
        return f"{REPO_PREFIX}{self.id}"

    @property
    def acceptance_dir(self):
        return os.path.join(self.dir, "acceptance")

    @property
    def start_dir(self):
        return os.path.join(self.dir, "start")

    @property
    def oracle_dir(self):
        return os.path.join(self.dir, "oracle")


def load_task(path):
    path = os.path.abspath(path)
    tpath = os.path.join(path, "task.toml")
    try:
        with open(tpath, "rb") as f:
            d = tomllib.load(f)
    except OSError as e:
        raise TaskError(f"{tpath}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise TaskError(f"{tpath}: not valid TOML: {e}") from None
    tid = d.get("id")
    if tid != os.path.basename(path):
        raise TaskError(f"{tpath}: id {tid!r} must equal the directory name {os.path.basename(path)!r}")
    if not all(c.isalnum() or c in "-." for c in tid) or tid.startswith("-"):
        raise TaskError(f"{tpath}: id must be [A-Za-z0-9.-]+")
    cls = d.get("class")
    if cls not in spec.EXEC_CLASSES:
        raise TaskError(f"{tpath}: class must be one of {spec.EXEC_CLASSES}")
    lang = d.get("lang")
    if lang not in LANGS:
        raise TaskError(f"{tpath}: lang must be one of {LANGS}")
    text = d.get("spec")
    if not isinstance(text, str) or not text.strip():
        raise TaskError(f"{tpath}: spec must be a non-empty string")
    may_edit = d.get("may_edit", [])
    if not isinstance(may_edit, list) or not all(isinstance(x, str) for x in may_edit):
        raise TaskError(f"{tpath}: may_edit must be a list of paths")
    if cls == "additive" and may_edit:
        raise TaskError(f"{tpath}: an additive task grants no existing file (may_edit must be empty)")
    if not os.path.exists(os.path.join(path, "acceptance", "run.sh")):
        raise TaskError(f"{path}: acceptance/run.sh missing")
    st = d.get("stage_timeout", 600)
    if isinstance(st, bool) or not isinstance(st, int) or st <= 0:
        raise TaskError(f"{tpath}: stage_timeout must be a positive integer")
    after = d.get("after")
    if after is not None:
        if not isinstance(after, str) or not after:
            raise TaskError(f"{tpath}: after must be a task id")
        if not after < tid:
            raise TaskError(f"{tpath}: after {after!r} must sort before {tid!r} (chains run in id order)")
        sib = os.path.join(os.path.dirname(path), after)
        if not os.path.isfile(os.path.join(sib, "task.toml")):
            raise TaskError(f"{tpath}: after {after!r}: no such task next to {tid!r}")
        try:
            prev = load_task(sib)  # the whole chain behind it is validated the same way
        except TaskError as e:
            raise TaskError(f"{tpath}: after {after!r}: {e}") from None
        if prev.lang != lang:
            raise TaskError(f"{tpath}: after {after!r} is {prev.lang}, this task is {lang} (a chain keeps one language)")
        if not os.path.isdir(prev.oracle_dir) or not os.listdir(prev.oracle_dir):
            raise TaskError(f"{tpath}: after {after!r} has no oracle/ to fall back to when it fails")
    return Task(id=tid, title=str(d.get("title") or tid), cls=cls, lang=lang, spec=text,
                may_edit=tuple(may_edit), dir=path, stage_timeout=st, after=after)


def predecessors(task):
    """The chain before `task`, root first (loaded from disk; `after` sorts
    strictly before its task, so the walk ends)."""
    out = []
    t = task
    while t.after:
        t = load_task(os.path.join(os.path.dirname(t.dir), t.after))
        out.insert(0, t)
    return out


def tests_version(bench_dir):
    """The bench checkout's commit, which is the version of the hidden
    acceptance that judges a run. Recorded on every run.end so a table can
    name the test version of each row instead of the reader assuming one
    (950: the September rows were judged by an earlier version and nothing
    in the ledger said so). None when the bench is not a git checkout."""
    # the last commit that touched tasks/, not HEAD: the runner host's bench
    # checkout carries a digest commit per shift on top of the pushed head,
    # and a digest is not a test version
    try:
        r = subprocess.run(["git", "-C", bench_dir, "log", "-1", "--format=%h", "--", "tasks"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


def load_tasks(bench_dir, only=None):
    root = os.path.join(bench_dir, "tasks")
    if not os.path.isdir(root):
        raise TaskError(f"{root}: no tasks directory")
    out = []
    for name in sorted(os.listdir(root)):
        if only and name not in only:
            continue
        if os.path.isfile(os.path.join(root, name, "task.toml")):
            out.append(load_task(os.path.join(root, name)))
    missing = set(only or []) - {t.id for t in out}
    if missing:
        raise TaskError(f"unknown task(s): {sorted(missing)}")
    return out


def _tree(root):
    """{relative path: bytes} of every file under root (no .git). A symlink
    anywhere is refused: a delivered branch is model-written, and open() on
    a link would read the runner host's own files into the next step's tree
    (opus review 1.1, 2026-09-03)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git"]
        for name in dns + fns:
            p = os.path.join(dp, name)
            if os.path.islink(p):
                raise TaskError(f"{os.path.relpath(p, root)}: a symlink in a work tree is refused")
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def acceptance_tar_b64(task):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(task.acceptance_dir, arcname=".")
    return base64.b64encode(buf.getvalue()).decode()


def _overlay(tree, d):
    """Lay directory d over tree; a .delete file in d lists paths removed first."""
    dele = os.path.join(d, ".delete")
    if os.path.isfile(dele):
        with open(dele) as f:
            for line in f:
                p = line.strip().rstrip("/")
                if not p or p.startswith("#"):
                    continue
                for k in [k for k in tree if k == p or k.startswith(p + "/")]:  # a file, or a whole directory
                    tree.pop(k)
    files = _tree(d)
    files.pop(".delete", None)
    tree.update(files)


def work_tree(task, templates_dir, overlay=None, base_tree=None):
    """The task's starting tree as {path: bytes}: the template for its
    language (or `base_tree`, the tree the step before it left) plus start/,
    plus an optional overlay such as the oracle."""
    if base_tree is not None:
        tree = dict(base_tree)
    else:
        tdir = os.path.join(templates_dir, task.lang)
        if not os.path.isdir(tdir):
            raise TaskError(f"{tdir}: no template for lang {task.lang!r}")
        tree = _tree(tdir)
    if os.path.isdir(task.start_dir):
        _overlay(tree, task.start_dir)
    if overlay and os.path.isdir(overlay):
        _overlay(tree, overlay)
    if ".dark/verify.sh" not in tree:
        raise TaskError(f"{task.id}: the work tree has no .dark/verify.sh")
    return tree


def oracle_tree(task, templates_dir):
    """The tree the reference solution leaves after `task`: the template,
    then every step of the chain up to and including this one (start/, then
    oracle/) in order. What a follow-up starts from when the step before it
    did not pass."""
    tree = None
    for t in predecessors(task) + [task]:
        tree = work_tree(t, templates_dir, overlay=t.oracle_dir, base_tree=tree)
    return tree


def fetch(url, branch, scratch, name):
    """Clone `branch` of url into scratch/<name> (history kept) and return
    (clone dir, its tree). What a delivered base is read from."""
    work = os.path.join(scratch, name)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    _git("clone", "-q", "--branch", branch, "--single-branch", url, work, cwd=scratch)
    modes = {l.split()[0] for l in _git("ls-tree", "-r", "HEAD", cwd=work).splitlines() if l.strip()}
    if "120000" in modes:
        raise TaskError(f"{branch}: the delivered tree holds a symlink; refused")
    return work, _tree(work)


def write_tree(tree, dest):
    for rel, data in tree.items():
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if rel.endswith(".sh"):
            os.chmod(p, 0o755)


_CRED = re.compile(r"://[^/@\s]+@")


def redact(text):
    """Credentials out of any URL in an error: a failed materialize push on
    2026-09-02 put the admin token into the shift log, the ledger's block
    event and, had the shift ended, the digest committed to the bench."""
    return _CRED.sub("://***@", text)


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise TaskError(redact(f"git {' '.join(args)}: {r.stderr.strip()[-300:]}"))
    return r.stdout


def materialize(task, tree, push_url, scratch, branch="main", base=None):
    """Commit `tree` as the tip of `branch` and force-push it to push_url:
    the single commit of a fresh history, or one commit on top of `base` (a
    clone from fetch(), so a follow-up carries the earlier steps' history).
    Returns the commit sha. The caller made sure the repo exists."""
    # named by the repo, not the task: two arms materialising the same task
    # at once (two chains, 2026-09-03 14:04) wiped each other's work dir
    work = os.path.join(scratch, f"mat-{task.repo_name}-{os.getpid()}")  # and by pid: two default-prefix shifts
    shutil.rmtree(work, ignore_errors=True)
    if base:
        shutil.copytree(base, work, symlinks=True)
        for name in os.listdir(work):
            if name != ".git":
                p = os.path.join(work, name)
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        _git("checkout", "-q", "-B", branch, cwd=work)
    else:
        os.makedirs(work)
        _git("init", "-q", "-b", branch, cwd=work)
    write_tree(tree, work)
    # -f: a .gitignore in the base (model-written on the chained path) must
    # not decide which of the follow-up's own files reach main (review 1.3)
    _git("add", "-A", "-f", cwd=work)
    # --allow-empty: a follow-up without start/ starts from exactly the tree
    # the step before it delivered, so the commit on top of the base is
    # empty (the first chain run, 2026-09-03 14:05, lost every delivered
    # base to "nothing to commit")
    _git("-c", "user.name=dark-runner", "-c", "user.email=dark-runner@localhost",
         "commit", "-q", "--allow-empty", "-m",
         f"task {task.id}: starting tree" + (f" (after {task.after})" if task.after else ""), cwd=work)
    try:
        _git("push", "-q", "--force", push_url, f"HEAD:{branch}", cwd=work)
    except TaskError:
        # two shifts materialising the same task at once (round 2 and the
        # variance subset both opened on csvline-python): one push loses the
        # race and the task was blocked for the whole shift. Once more.
        time.sleep(3)
        _git("push", "-q", "--force", push_url, f"HEAD:{branch}", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work).strip()
    shutil.rmtree(work, ignore_errors=True)
    if base:
        shutil.rmtree(base, ignore_errors=True)  # the fetched clone is consumed here (review 3.3)
    return sha
