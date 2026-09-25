import base64
import io
import os
import subprocess
import tarfile
import tempfile
import unittest

from dark import spec as spec_mod
from dark import tasks
from tests import fakes

VERIFY = "#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/..\"\ntest -f hello.txt\necho verify OK\n"


def make_templates(root):
    for lang in ("go", "python"):
        d = os.path.join(root, lang, ".dark")
        os.makedirs(d)
        with open(os.path.join(d, "verify.sh"), "w") as f:
            f.write(VERIFY)
        with open(os.path.join(root, lang, "README.md"), "w") as f:
            f.write(f"# {lang} template\n")
    return root


def make_task(bench, tid="hello", cls="additive", lang="python", may_edit=(), spec_text="Create hello.txt containing hello",
              start=None, oracle=None, acceptance="grep -q hello hello.txt && echo 'CHECK hello ok' || { echo 'CHECK hello fail'; exit 1; }\n",
              extra=""):
    d = os.path.join(bench, "tasks", tid)
    os.makedirs(os.path.join(d, "acceptance"))
    me = ", ".join(f'"{m}"' for m in may_edit)
    with open(os.path.join(d, "task.toml"), "w") as f:
        f.write(f'id = "{tid}"\ntitle = "{tid} task"\nclass = "{cls}"\nlang = "{lang}"\n'
                f'may_edit = [{me}]\nspec = """{spec_text}"""\n{extra}')
    with open(os.path.join(d, "acceptance", "run.sh"), "w") as f:
        f.write("#!/bin/bash\n" + acceptance)
    for sub, files in (("start", start), ("oracle", oracle)):
        if files:
            for rel, content in files.items():
                p = os.path.join(d, sub, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(content)
    return d


class Load(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bench = os.path.join(self.tmp, "bench")

    def test_load_and_list(self):
        make_task(self.bench, "hello", start={"note.txt": "n\n"})
        make_task(self.bench, "fix-1", cls="repair", may_edit=("main.py",), lang="go")
        ts = tasks.load_tasks(self.bench)
        self.assertEqual([t.id for t in ts], ["fix-1", "hello"])
        t = tasks.load_task(os.path.join(self.bench, "tasks", "hello"))
        self.assertEqual((t.cls, t.lang, t.may_edit, t.repo_name, t.stage_timeout), ("additive", "python", (), "t-hello", 600))
        self.assertEqual(tasks.load_tasks(self.bench, only=["hello"])[0].id, "hello")
        with self.assertRaises(tasks.TaskError):
            tasks.load_tasks(self.bench, only=["ghost"])

    def refuse(self, needle, **kw):
        d = make_task(self.bench, **kw)
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(d)
        self.assertIn(needle, str(cm.exception))

    def test_refusals(self):
        self.refuse("class", tid="a", cls="spec")
        self.refuse("lang", tid="b", lang="rust")
        self.refuse("additive", tid="c", may_edit=("x",))
        self.refuse("stage_timeout", tid="d", extra="stage_timeout = 0\n")
        self.refuse("tools", tid="g", extra='tools = "all"\n')
        d = make_task(self.bench, "e")
        os.remove(os.path.join(d, "acceptance", "run.sh"))
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(d)
        self.assertIn("run.sh", str(cm.exception))
        d = make_task(self.bench, "f")
        with open(os.path.join(d, "task.toml"), "a") as f:
            f.write('id = "other"\n')
        with self.assertRaises(tasks.TaskError):
            tasks.load_task(d)

    def test_no_tasks_dir(self):
        with self.assertRaises(tasks.TaskError):
            tasks.load_tasks(self.tmp)

    def test_repo_prefix_names_an_arms_own_repos(self):
        # a session arm beside a shift must not share dark/t-<id>
        t = tasks.load_task(make_task(self.bench, "hello"))
        self.assertEqual(t.repo_name, "t-hello")
        old = tasks.REPO_PREFIX
        try:
            tasks.REPO_PREFIX = "session-dsf-"
            self.assertEqual(t.repo_name, "session-dsf-hello")
        finally:
            tasks.REPO_PREFIX = old


class Trees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bench = os.path.join(self.tmp, "bench")
        self.templates = make_templates(os.path.join(self.tmp, "templates"))

    def test_work_tree_overlay_order(self):
        d = make_task(self.bench, "hello", start={"README.md": "# start\n", "a.py": "x\n"},
                      oracle={"hello.txt": "hello\n", "a.py": "y\n"})
        t = tasks.load_task(d)
        tree = tasks.work_tree(t, self.templates)
        self.assertEqual(tree["README.md"], b"# start\n")
        self.assertEqual(tree[".dark/verify.sh"], VERIFY.encode())
        self.assertNotIn("hello.txt", tree)
        oracle = tasks.work_tree(t, self.templates, overlay=t.oracle_dir)
        self.assertEqual(oracle["hello.txt"], b"hello\n")
        self.assertEqual(oracle["a.py"], b"y\n")
        with self.assertRaises(tasks.TaskError):
            tasks.work_tree(t, os.path.join(self.tmp, "none"))

    def test_acceptance_tar(self):
        d = make_task(self.bench, "hello")
        with open(os.path.join(d, "acceptance", "fixture.txt"), "w") as f:
            f.write("f\n")
        t = tasks.load_task(d)
        blob = base64.b64decode(tasks.acceptance_tar_b64(t))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            names = {m.name for m in tf.getmembers() if m.isfile()}
        self.assertEqual(names, {"./run.sh", "./fixture.txt"})

    def test_git_errors_never_carry_credentials(self):
        # git errors are redacted: the admin token must never reach a log,
        # the ledger or a digest
        with self.assertRaises(tasks.TaskError) as cm:
            tasks._git("push", "http://dark-admin:0123456789abcdef0123456789abcdef01234567@127.0.0.1:9/dark/t.git",
                       "HEAD:main", cwd=self.tmp)
        self.assertNotIn("0123456789abcdef", str(cm.exception))
        self.assertIn("://***@", str(cm.exception))
        self.assertEqual(tasks.redact("push http://u:p@h/x and http://h/y"), "push http://***@h/x and http://h/y")

    def test_materialize_force_pushes_main(self):
        d = make_task(self.bench, "hello", start={"a.py": "x\n"})
        t = tasks.load_task(d)
        repos = os.path.join(self.tmp, "repos")
        fakes.make_origin(repos, "dark/t-hello", {"old.txt": "old\n"})
        url = f"file://{repos}/dark/t-hello.git"
        sha1 = tasks.materialize(t, tasks.work_tree(t, self.templates), url, os.path.join(self.tmp, "scratch"))
        self.assertEqual(fakes.branch_files(repos, "dark/t-hello", "main"), {".dark/verify.sh", "README.md", "a.py"})
        sha2 = tasks.materialize(t, tasks.work_tree(t, self.templates), url, os.path.join(self.tmp, "scratch"))
        self.assertNotEqual(sha1, "")
        self.assertEqual(fakes.branch_files(repos, "dark/t-hello", "main"), {".dark/verify.sh", "README.md", "a.py"})
        self.assertTrue(sha2)


class Chains(unittest.TestCase):
    """A task that follows another starts from what that task delivered, or
    from its oracle tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bench = os.path.join(self.tmp, "bench")
        self.templates = make_templates(os.path.join(self.tmp, "templates"))
        make_task(self.bench, "obs-01", start={"a.py": "1\n"}, oracle={"hello.txt": "one\n", "b.py": "b1\n"})
        d = make_task(self.bench, "obs-02", start={"c.py": "c\n"}, oracle={"hello.txt": "two\n"},
                      extra='after = "obs-01"\n')
        with open(os.path.join(d, "oracle", ".delete"), "w") as f:
            f.write("b.py\n")

    def test_after_is_validated(self):
        t2 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-02"))
        self.assertEqual(t2.after, "obs-01")
        self.assertEqual([t.id for t in tasks.predecessors(t2)], ["obs-01"])
        self.assertEqual(tasks.predecessors(tasks.load_task(os.path.join(self.bench, "tasks", "obs-01"))), [])
        # the chain runs in id order, so a follower sorts after what it follows
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(make_task(self.bench, "aaa", extra='after = "obs-01"\n'))
        self.assertIn("sort before", str(cm.exception))
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(make_task(self.bench, "obs-03", extra='after = "obs-00"\n'))
        self.assertIn("no such task", str(cm.exception))

    def test_oracle_tree_is_cumulative_and_honours_delete(self):
        t1 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-01"))
        t2 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-02"))
        o1 = tasks.oracle_tree(t1, self.templates)
        self.assertEqual((o1["a.py"], o1["hello.txt"], o1["b.py"]), (b"1\n", b"one\n", b"b1\n"))
        o2 = tasks.oracle_tree(t2, self.templates)
        self.assertEqual((o2["a.py"], o2["c.py"], o2["hello.txt"]), (b"1\n", b"c\n", b"two\n"))
        self.assertNotIn("b.py", o2)
        self.assertNotIn(".delete", o2)
        self.assertIn(".dark/verify.sh", o2)
        # what obs-02 starts from when obs-01 did not pass: obs-01's oracle plus obs-02's start, no obs-02 oracle
        start = tasks.work_tree(t2, self.templates, base_tree=o1)
        self.assertEqual((start["hello.txt"], start["b.py"], start["c.py"]), (b"one\n", b"b1\n", b"c\n"))

    def test_materialize_on_a_delivered_base_keeps_its_history(self):
        t2 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-02"))
        repos = os.path.join(self.tmp, "repos")
        fakes.make_origin(repos, "dark/t-obs-01", {"x.txt": "x\n", ".dark/verify.sh": VERIFY}, branch="run/one")
        fakes.make_origin(repos, "dark/t-obs-02", {"old.txt": "old\n"})
        scratch = os.path.join(self.tmp, "scratch")
        clone, tree = tasks.fetch(f"file://{repos}/dark/t-obs-01.git", "run/one", scratch, "base-obs-02")
        self.assertEqual(tree["x.txt"], b"x\n")
        self.assertNotIn(".git/HEAD", tree)
        final = tasks.work_tree(t2, self.templates, base_tree=tree)
        sha = tasks.materialize(t2, final, f"file://{repos}/dark/t-obs-02.git", scratch, base=clone)
        self.assertTrue(sha)
        self.assertEqual(fakes.branch_files(repos, "dark/t-obs-02", "main"), {".dark/verify.sh", "x.txt", "c.py"})
        bare = os.path.join(repos, "dark/t-obs-02.git")
        self.assertEqual(fakes.git("rev-list", "--count", "main", cwd=bare).stdout.strip(), "2")
        self.assertIn("after obs-01", fakes.git("log", "-1", "--format=%s", "main", cwd=bare).stdout)
        # a fresh materialize (no base) is one commit again
        tasks.materialize(t2, final, f"file://{repos}/dark/t-obs-02.git", scratch)
        self.assertEqual(fakes.git("rev-list", "--count", "main", cwd=bare).stdout.strip(), "1")

    def test_a_follower_without_start_is_an_empty_commit_on_the_base(self):
        # obs-02-count has no start/: its tree IS the delivered tree, and the
        # first chain run lost every such base to git's "nothing to commit"
        make_task(self.bench, "obs-03", extra='after = "obs-02"\n')  # obs-02 has an oracle (setUp)
        t3 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-03"))
        repos = os.path.join(self.tmp, "repos")
        fakes.make_origin(repos, "dark/t-obs-02", {"x.txt": "x\n", ".dark/verify.sh": VERIFY}, branch="run/two")
        fakes.make_origin(repos, "dark/t-obs-03", {"old.txt": "old\n"})
        scratch = os.path.join(self.tmp, "scratch")
        clone, tree = tasks.fetch(f"file://{repos}/dark/t-obs-02.git", "run/two", scratch, "base-obs-03")
        sha = tasks.materialize(t3, tasks.work_tree(t3, self.templates, base_tree=tree),
                                f"file://{repos}/dark/t-obs-03.git", scratch, base=clone)
        self.assertTrue(sha)
        self.assertEqual(fakes.branch_files(repos, "dark/t-obs-03", "main"), {".dark/verify.sh", "x.txt"})
        self.assertEqual(fakes.git("rev-list", "--count", "main", cwd=os.path.join(repos, "dark/t-obs-03.git")).stdout.strip(), "2")

    # --- a delivered tree is not trusted ------------------------------------
    def test_symlinks_are_refused_in_a_tree_and_on_a_delivered_branch(self):
        # a model-written branch could link to a runner-host file and _tree
        # would read it into the next step's tree
        d = os.path.join(self.tmp, "linktree")
        os.makedirs(d)
        with open(os.path.join(self.tmp, "outside.txt"), "w") as f:
            f.write("host-only\n")
        os.symlink(os.path.join(self.tmp, "outside.txt"), os.path.join(d, "leak.txt"))
        with self.assertRaises(tasks.TaskError) as cm:
            tasks._tree(d)
        self.assertIn("symlink", str(cm.exception))
        repos = os.path.join(self.tmp, "repos")
        fakes.make_origin(repos, "dark/t-obs-01", {"x.txt": "x\n"}, branch="run/one")
        work = os.path.join(self.tmp, "w")
        fakes.git("clone", "-q", "-b", "run/one", f"file://{repos}/dark/t-obs-01.git", work)
        os.symlink(os.path.join(self.tmp, "outside.txt"), os.path.join(work, "leak.txt"))
        fakes.git("add", "-A", cwd=work)
        fakes.git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "link", cwd=work)
        fakes.git("push", "-q", "origin", "run/one", cwd=work)
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.fetch(f"file://{repos}/dark/t-obs-01.git", "run/one", os.path.join(self.tmp, "scratch"), "b")
        self.assertIn("symlink", str(cm.exception))

    def test_delete_names_a_directory_and_skips_comments(self):
        # .delete keyed by file path removed nothing for a directory
        tree = {"b/one.py": b"1", "b/two.py": b"2", "bb.py": b"3", "c.py": b"4"}
        d = os.path.join(self.tmp, "ov")
        os.makedirs(d)
        with open(os.path.join(d, ".delete"), "w") as f:
            f.write("b/\n# a comment\n\nc.py\n")
        tasks._overlay(tree, d)
        self.assertEqual(sorted(tree), ["bb.py"])

    def test_a_chain_keeps_one_language_and_needs_an_oracle_behind_it(self):
        # a python step after a go step took the go template; a predecessor
        # without oracle/ handed its unsolved start over as "oracle"
        make_task(self.bench, "go-01", lang="go", oracle={"m.go": "package main\n"})
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(make_task(self.bench, "go-02", extra='after = "go-01"\n'))
        self.assertIn("language", str(cm.exception))
        make_task(self.bench, "no-01")
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(make_task(self.bench, "no-02", extra='after = "no-01"\n'))
        self.assertIn("oracle", str(cm.exception))

    def test_a_gitignore_in_the_base_does_not_drop_the_followers_files(self):
        # git add -A honoured the delivered tree's .gitignore; the fetched
        # clone is consumed
        t2 = tasks.load_task(os.path.join(self.bench, "tasks", "obs-02"))
        repos = os.path.join(self.tmp, "repos")
        fakes.make_origin(repos, "dark/t-obs-01", {".gitignore": "*.log\n", ".dark/verify.sh": VERIFY}, branch="run/one")
        fakes.make_origin(repos, "dark/t-obs-02", {"old.txt": "old\n"})
        scratch = os.path.join(self.tmp, "scratch")
        clone, tree = tasks.fetch(f"file://{repos}/dark/t-obs-01.git", "run/one", scratch, "base-obs-02")
        tree["debug.log"] = b"d\n"
        tasks.materialize(t2, tasks.work_tree(t2, self.templates, base_tree=tree), f"file://{repos}/dark/t-obs-02.git", scratch, base=clone)
        self.assertIn("debug.log", fakes.branch_files(repos, "dark/t-obs-02", "main"))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual([d for d in os.listdir(scratch) if d.startswith("mat-")], [])


class TaskRoots(unittest.TestCase):
    """A task path may name several task-set directories at once, PATH
    style. Names are looked up across them; the same name in two of them is
    refused, never silently shadowed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")

    def test_one_directory_is_unchanged(self):
        make_task(self.a, "hello")
        make_task(self.a, "fix-1", cls="repair", may_edit=("main.py",), lang="go")
        self.assertEqual([t.id for t in tasks.load_tasks(self.a)], ["fix-1", "hello"])
        self.assertEqual([t.id for t in tasks.load_tasks(self.a, only=["hello"])], ["hello"])

    def test_two_directories_are_the_union(self):
        make_task(self.a, "hello")
        make_task(self.b, "other")
        spec = os.pathsep.join([self.a, self.b])
        self.assertEqual([t.id for t in tasks.load_tasks(spec)], ["hello", "other"])
        self.assertEqual(sorted(tasks.task_dir_index(spec)), ["hello", "other"])
        self.assertEqual(tasks.load_tasks(spec, only=["other"])[0].id, "other")

    def test_a_name_in_two_directories_is_refused_naming_both(self):
        make_task(self.a, "hello")
        make_task(self.b, "hello")
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_tasks(os.pathsep.join([self.a, self.b]))
        msg = str(cm.exception)
        self.assertIn(os.path.join(self.a, "tasks", "hello"), msg)
        self.assertIn(os.path.join(self.b, "tasks", "hello"), msg)

    def test_the_example_tasks_are_not_exempt_from_a_duplicate(self):
        # the bench checkout's own examples are a task set like any other: a
        # name they share with another root is refused, naming both, so no
        # root is ever served by a silent first occurrence
        bench = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bench")
        self.assertTrue(os.path.isdir(os.path.join(bench, "tasks", "hello-go")))
        make_task(self.a, "hello-go")
        spec = os.pathsep.join([bench, self.a])
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.task_dirs(spec)
        msg = str(cm.exception)
        self.assertIn(os.path.join(bench, "tasks", "hello-go"), msg)
        self.assertIn(os.path.join(self.a, "tasks", "hello-go"), msg)

    def test_a_missing_directory_is_refused_with_its_path(self):
        missing = os.path.join(self.tmp, "absent")
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_tasks(missing)
        self.assertIn(missing, str(cm.exception))

    def test_the_first_root_is_the_primary_checkout(self):
        self.assertEqual(tasks.primary_root(os.pathsep.join([self.a, self.b])), self.a)
        self.assertEqual(tasks.primary_root(self.a), self.a)


def make_user_task(bench, tid="shop", body=None):
    """A user task: task.toml only, no start/, no acceptance/."""
    d = os.path.join(bench, "tasks", tid)
    os.makedirs(d)
    if body is None:
        body = ('url = "https://app.example.test/"\n'
                'steps = ["Open the sign-in page", "Sign in as the demo user", "Add one item to the cart"]\n'
                'spec = """Check that a visitor can buy one item."""\n')
    with open(os.path.join(d, "task.toml"), "w") as f:
        f.write(f'id = "{tid}"\nclass = "user"\n{body}')
    return d


def make_long_task(bench, tid="long-1", body=None):
    """A long task: task.toml only, repositories and a spec, no start/ and
    no acceptance/."""
    d = os.path.join(bench, "tasks", tid)
    os.makedirs(d)
    if body is None:
        body = ('repos = [\n'
                '  {name = "api", url = "https://git.example.test/dark/api.git", base = "main"},\n'
                '  {name = "web", url = "ssh://git@git.example.test/dark/web.git", base = "main"},\n'
                ']\n'
                'spec = """Move both repositories onto the new schema."""\n')
    with open(os.path.join(d, "task.toml"), "w") as f:
        f.write(f'id = "{tid}"\nclass = "long"\n{body}')
    return d


class LongTasks(unittest.TestCase):
    """class = "long": several repositories and a spec, judged by a reviewer
    session; no hidden acceptance, and the work-repo fields are refused."""

    def setUp(self):
        self.bench = os.path.join(tempfile.mkdtemp(), "bench")

    def refuse(self, needle, tid, body):
        d = make_long_task(self.bench, tid, body)
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(d)
        self.assertIn(needle, str(cm.exception))

    def test_a_long_task_loads_with_its_repositories(self):
        t = tasks.load_task(make_long_task(self.bench))
        self.assertEqual((t.cls, t.lang, t.may_edit, t.spec, t.tools),
                         ("long", "", (), "Move both repositories onto the new schema.", "reduced"))
        self.assertEqual(t.repos, (("api", "https://git.example.test/dark/api.git", "main"),
                                   ("web", "ssh://git@git.example.test/dark/web.git", "main")))
        self.assertEqual(t.stage_timeout, 0)  # nothing is staged
        self.assertFalse(os.path.exists(t.acceptance_dir))

    def test_lang_and_tools_are_optional(self):
        d = make_long_task(self.bench, "long-lang",
                           'repos = [{name = "api", url = "https://x/y.git", base = "main"}]\n'
                           'lang = "python"\ntools = "full"\nspec = "s"\n')
        t = tasks.load_task(d)
        self.assertEqual((t.lang, t.tools), ("python", "full"))
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(make_long_task(self.bench, "long-bad-tools",
                                           'repos = [{name = "api", url = "https://x/y.git", base = "main"}]\n'
                                           'tools = "all"\nspec = "s"\n'))
        self.assertIn("tools", str(cm.exception))

    def test_repos_is_required_and_non_empty(self):
        self.refuse("repos", "long-none", 'spec = "s"\n')
        self.refuse("repos", "long-empty", 'repos = []\nspec = "s"\n')

    def test_a_repo_name_is_one_unique_path_segment(self):
        for i, name in enumerate((".", "..", "a/b")):
            with self.subTest(name=name):
                self.refuse("name", f"long-name{i}",
                            f'repos = [{{name = "{name}", url = "https://x/y.git", base = "main"}}]\nspec = "s"\n')
        self.refuse("repeated", "long-dup",
                    'repos = [{name = "api", url = "https://x/a.git", base = "main"},\n'
                    '         {name = "api", url = "https://x/b.git", base = "main"}]\nspec = "s"\n')

    def test_a_repo_url_scheme_is_checked(self):
        self.refuse("url", "long-url",
                    'repos = [{name = "api", url = "file:///x/y.git", base = "main"}]\nspec = "s"\n')

    def test_a_repo_entry_needs_all_three_keys(self):
        self.refuse("base", "long-base",
                    'repos = [{name = "api", url = "https://x/y.git"}]\nspec = "s"\n')
        self.refuse("exactly", "long-key",
                    'repos = [{name = "api", url = "https://x/y.git", base = "main", extra = "x"}]\nspec = "s"\n')

    def test_a_long_task_has_no_work_repo_or_hidden_test_fields(self):
        base = 'repos = [{name = "api", url = "https://x/y.git", base = "main"}]\nspec = "s"\n'
        for key, value in (("may_edit", '["a.py"]'), ("after", '"other"'),
                           ("url", '"http://x"'), ("steps", '["x"]')):
            with self.subTest(key=key):
                self.refuse(key, f"long-{key}", base + f"{key} = {value}\n")

    def test_acceptance_run_sh_is_refused(self):
        # acceptance/run.sh exists only for hidden tests, which a long task
        # does not have: judge it by a reviewer, not by staging
        d = make_task(self.bench, "long-staged", cls="long", lang="", spec_text="s")
        with open(os.path.join(d, "task.toml"), "a") as f:
            f.write('repos = [{name = "api", url = "https://x/y.git", base = "main"}]\n')
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(d)
        self.assertIn("run.sh", str(cm.exception))


class UserTasks(unittest.TestCase):
    """class = "user": url required, steps numbered in order, and nothing of
    a work-repo task; url and steps refused on every other class."""

    def setUp(self):
        self.bench = os.path.join(tempfile.mkdtemp(), "bench")

    def refuse(self, needle, tid, body):
        d = make_user_task(self.bench, tid, body)
        with self.assertRaises(tasks.TaskError) as cm:
            tasks.load_task(d)
        self.assertIn(needle, str(cm.exception))

    def test_a_user_task_loads_with_its_url_and_steps(self):
        t = tasks.load_task(make_user_task(self.bench))
        self.assertEqual((t.cls, t.url, t.lang, t.may_edit), ("user", "https://app.example.test/", "", ()))
        self.assertEqual(t.steps, ("Open the sign-in page", "Sign in as the demo user",
                                   "Add one item to the cart"))
        self.assertFalse(os.path.exists(t.acceptance_dir))  # no hidden tests to need

    def test_the_url_is_required_for_a_user_task(self):
        self.refuse("url", "a", 'steps = ["x"]\nspec = "s"\n')
        self.refuse("url", "b", 'url = "ftp://x"\nsteps = ["x"]\nspec = "s"\n')

    def test_steps_are_a_non_empty_list_of_texts(self):
        for i, steps in enumerate(("[]", '"one"', '["ok", ""]', "[1]")):
            with self.subTest(steps=steps):
                self.refuse("steps", f"s{i}", f'url = "http://x"\nsteps = {steps}\nspec = "s"\n')

    def test_a_user_task_has_no_work_repo_fields(self):
        base = 'url = "http://x"\nsteps = ["x"]\nspec = "s"\n'
        self.refuse("may_edit", "m", base + 'may_edit = ["a.py"]\n')
        self.refuse("lang", "l", base + 'lang = "python"\n')
        self.refuse("after", "f", base + 'after = "a"\n')

    def test_url_and_steps_are_refused_on_every_other_class(self):
        for cls in spec_mod.EXEC_CLASSES:
            for key, value in (("url", '"http://x"'), ("steps", '["x"]')):
                with self.subTest(cls=cls, key=key):
                    d = make_task(self.bench, f"{cls}-{key}", cls=cls, extra=f"{key} = {value}\n")
                    with self.assertRaises(tasks.TaskError) as cm:
                        tasks.load_task(d)
                    self.assertIn(key, str(cm.exception))


class _NoMeter:
    def start(self):
        return self

    def stop(self):
        return {}


class UserTaskInTheSandbox(unittest.TestCase):
    """What a user-arm sandbox is told: the URL and the steps, and no work
    repository at all, so nothing in it can clone or read the source."""

    def setUp(self):
        from dark import budget, config
        from dark import ledger as L
        from dark import run as R
        from tests.test_config import BUDGETS, MODELS, write_conf
        tmp = tempfile.mkdtemp()
        cat, bud = config.load(write_conf(tmp, MODELS, BUDGETS))
        self.host = config.Host(work_org="dark-runs", records_org="dark-records", state_dir=tmp,
                                agent_token="agent-tok", git_lan_url="http://git.example.test")
        led = L.Ledger(os.path.join(tmp, "ledger.jsonl"))
        self.runner = R.Runner(cat, bud, self.host, led, None, None, log=lambda *a: None,
                               hold=lambda s: (True, ""), meter=_NoMeter)
        self.task = tasks.load_task(make_user_task(os.path.join(tmp, "bench")))
        self.env = budget.envelope(bud, led, "user", "cloud-x")

    def build(self):
        return self.runner.user_task(self.task, "cloud-x", self.env, 7, "shop-1", "dark-records/s1")

    def test_the_task_carries_the_url_and_the_numbered_steps(self):
        at, _ = self.build()
        self.assertEqual(at["url"], "https://app.example.test/")
        self.assertEqual(at["steps"], list(self.task.steps))
        self.assertEqual((at["class"], at["mode"], at["max_calls"]), ("user", "user", self.env.calls))

    def test_no_work_repository_and_nothing_to_clone(self):
        at, _ = self.build()
        # the one repository named is the records repo, never the work org
        self.assertEqual(at["repo"], "dark-records/s1")
        self.assertEqual(at["records_repo"], "dark-records/s1")
        self.assertFalse(any(isinstance(v, str) and v.startswith(self.host.work_org + "/")
                             for v in at.values()))
        for key in ("branch", "may_edit", "lang", "runtime_url", "acceptance_tar_b64"):
            self.assertNotIn(key, at)

    def test_the_sandbox_is_spawned_as_a_user_sandbox(self):
        _, kw = self.build()
        self.assertEqual(kw, {"cls": "user"})               # proxmox: the target firewall rule
        self.host.backend = "docker"
        _, kw = self.build()
        self.assertEqual(kw, {"cls": "user", "image": "dark-sandbox-browser"})

    def test_a_shift_refuses_a_user_task(self):
        class NoGitea:
            def __getattr__(self, name):
                raise AssertionError(f"gitea.{name} called for a user task")
        self.runner.gitea = NoGitea()
        res = self.runner.run(self.task, "cloud-x", self.env)
        self.assertEqual(res.outcome, "refused")
        self.assertIn("dark user", res.detail)


class TestsVersion(unittest.TestCase):
    """Every run.end names the bench commit whose acceptance judged it, so a
    table can print the test version of each row instead of the reader
    assuming one."""

    def git(self, d, *a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", d, *a], check=True,
                       capture_output=True)

    def test_the_version_is_the_last_commit_that_touched_tasks(self):
        d = tempfile.mkdtemp()
        self.git(d, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(d, "tasks"))
        with open(os.path.join(d, "tasks", "t.toml"), "w") as f:
            f.write("x\n")
        self.git(d, "add", "-A")
        self.git(d, "commit", "-q", "-m", "tasks")
        want = subprocess.run(["git", "-C", d, "log", "-1", "--format=%h"], capture_output=True,
                              text=True).stdout.strip()
        # a digest commit on top of it is not a new test version
        with open(os.path.join(d, "digest.md"), "w") as f:
            f.write("d\n")
        self.git(d, "add", "-A")
        self.git(d, "commit", "-q", "-m", "digest")
        self.assertEqual(tasks.tests_version(d), want)

    def test_a_directory_that_is_not_a_checkout_reports_nothing(self):
        self.assertIsNone(tasks.tests_version(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
