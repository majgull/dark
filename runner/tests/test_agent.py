import importlib
import json
import os
import shutil
import tempfile
import unittest

from tests import fakes

VERIFY_HELLO = "#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/..\"\ntest -f hello.txt && grep -q hello hello.txt\necho verify OK\n"
FILE_HELLO = "FILE: hello.txt\n```\nhello world\n```\n"


def load_agent(task, work, transcript):
    os.environ["DARK_TASK"] = task
    os.environ["DARK_WORK"] = work
    os.environ["DARK_TRANSCRIPT"] = transcript
    import dark.agent as agent
    return importlib.reload(agent)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent = load_agent(os.path.join(self.tmp, "none.json"), self.tmp, os.path.join(self.tmp, "t.jsonl"))

    def test_clean_path(self):
        a = self.agent
        self.assertEqual(a._clean_path("./a/b.go"), "a/b.go")
        self.assertEqual(a._clean_path("././x"), "x")
        self.assertEqual(a._clean_path(".hidden"), ".hidden")  # not lstrip
        self.assertEqual(a._clean_path("dir//x"), "dir/x")

    def test_parse_files(self):
        reply = ("prose\nFILE: ./a.py\n```python\nprint(1)```\nFILE: .gitea/x\n```\nno\n```\n"
                 "FILE: ../up\n```\nno\n```\nFILE: .factory/verify.sh\n```\nno\n```\n"
                 "FILE: b.txt\n```\nline\n```\n")
        files = self.agent.parse_files(reply)
        self.assertEqual(set(files), {"a.py", "b.txt"})
        self.assertEqual(files["a.py"], "print(1)\n")  # newline added

    def test_parse_deletes(self):
        reply = "DELETE: old.go\nDELETE: .git/config\ntext DELETE: notaline\nDELETE: ./x/y.md\n"
        self.assertEqual(self.agent.parse_deletes(reply), ["old.go", "x/y.md"])

    def test_guard_writes(self):
        g = self.agent.guard_writes
        files, dels, ref = g({"new.go": "x", "old.go": "y"}, [], {"old.go"}, None)
        self.assertEqual((files, dels, ref), ({}, [], ["old.go"]))
        files, dels, ref = g({"new.go": "x", "old.go": "y"}, ["gone.go"], {"old.go", "gone.go"}, ["old.go", "gone.go"])
        self.assertEqual(ref, [])
        self.assertEqual(set(files), {"new.go", "old.go"})

    def test_guard_refuses_symlink_escape(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        os.symlink("/etc", os.path.join(root, "link"))
        _, _, ref = self.agent.guard_writes({"link/passwd": "x"}, [], set(), None, root)
        self.assertEqual(ref, ["link/passwd"])
        _, _, ref = self.agent.guard_writes({"plain.txt": "x"}, [], set(), None, root)
        self.assertEqual(ref, [])

    def test_track_stall_cycle_rule(self):
        # 950 stall-cut brief's worked example: a repeat is "seen anywhere
        # earlier in the run", not a consecutive-streak rule
        a = self.agent
        for r in ["A", "B", "A", "C", "B"]:
            a._track_stall(r)
        self.assertEqual((a.STATS["distinct_calls"], a.STATS["repeat_calls"], a.STATS["stall_max"]),
                          (3, 2, 1))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gitea = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.gitea.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repos = os.path.join(self.tmp, "repos")
        self.full = "dark/t-hello"
        self.gitea.repos[self.full] = {"archived": False, "branches": {"main"}}
        self.gitea.issues[self.full] = {1: {"title": "run", "body": "", "state": "open", "comments": []}}
        self.gitea.assets.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agent(self, replies, files=None, task_extra=None):
        files = files or {".factory/verify.sh": VERIFY_HELLO, "README.md": "# t\n"}
        self.repos = tempfile.mkdtemp(dir=self.tmp)
        self.gitea.issues[self.full][1]["comments"] = []
        git_url = fakes.make_origin(self.repos, self.full, files)
        llm = fakes.FakeLLM(replies)
        task = {"gitea": self.gitea.url, "git_url": git_url, "repo": self.full, "issue": 1,
                "branch": "run/r1", "token": "agent-tok", "llm_url": f"{llm.url}/v1",
                "llm_model": "fake", "max_calls": 3, "max_tokens": 512, "llm_timeout": 5,
                "heartbeat_seconds": 1, "spec": "Create hello.txt containing hello", "run": "r1",
                "task": "hello"}
        task.update(task_extra or {})
        tpath = os.path.join(self.tmp, "task.json")
        with open(tpath, "w") as f:
            json.dump(task, f)
        agent = load_agent(tpath, os.path.join(self.repos, "work"), os.path.join(self.repos, "t.jsonl"))
        try:
            rc = agent.main()
        finally:
            agent.upload_transcript()
            llm.close()
        return rc, llm

    def done_tag(self):
        bodies = self.gitea.bodies(self.full, 1)
        done = [b for b in bodies if b.startswith("AGENT-DONE")]
        self.assertEqual(len(done), 1, bodies)
        line = [l for l in done[0].splitlines() if l.startswith("DARK:")][-1]
        return json.loads(line[len("DARK:"):])

    def progress(self):
        bodies = self.gitea.bodies(self.full, 1)
        alive = [b for b in bodies if b.startswith("AGENT-ALIVE")]
        self.assertEqual(len(alive), 1)
        return [json.loads(l[len("DARK:"):]) for l in alive[0].splitlines() if l.startswith("DARK:")]

    def test_happy_path_after_one_bad_reply(self):
        rc, llm = self.run_agent([{"content": "I would write a file."}, {"content": FILE_HELLO}])
        self.assertEqual(rc, 0)
        tag = self.done_tag()
        self.assertEqual(tag["outcome"], "ok")
        self.assertEqual(tag["calls"], 2)
        self.assertEqual(tag["tokens_in"], 20)
        self.assertEqual(tag["files"], ["hello.txt"])
        self.assertEqual(tag["branch"], "run/r1")
        self.assertIn("seconds", tag)
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "hello.txt"), "hello world\n")
        evs = [p["ev"] for p in self.progress()]
        self.assertEqual(evs[0], "start")
        self.assertIn("verify", evs)
        self.assertEqual(evs[-1], "beat")
        self.assertEqual([p for p in self.progress() if p["ev"] == "verify"][0]["ok"], True)
        self.assertEqual(len(self.gitea.assets), 1)
        self.assertTrue(self.gitea.assets[0][2].endswith(".txt"))
        # the second request carried the "no FILE blocks" feedback
        self.assertIn("No FILE blocks", llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(llm.requests[0]["max_tokens"], 512)

    def test_files_from_every_iteration_reach_the_branch(self):
        # 949 pilot: dsf's second reply named only the test file it fixed; the
        # code from the first reply never got staged and the staging VM saw
        # "undefined: CompareSemver" against a green executor
        verify = "#!/bin/bash\ncd \"$(dirname \"$0\")/..\"\ntest -f hello.txt || exit 1\ntest -f world.txt || exit 1\necho verify OK\n"
        world = "FILE: world.txt\n```\nworld\n```\n"
        rc, llm = self.run_agent([{"content": FILE_HELLO}, {"content": world}],
                                 files={".factory/verify.sh": verify, "README.md": "# t\n"})
        self.assertEqual(rc, 0)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["iter"], tag["files"]), ("ok", 2, ["hello.txt", "world.txt"]))
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "hello.txt"), "hello world\n")
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "world.txt"), "world\n")

    def test_calls_exhausted_is_capability(self):
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, _ = self.run_agent([{"content": bad}] * 3)
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["calls"]), ("fail", "calls", 3))
        self.assertEqual(sum(1 for p in self.progress() if p["ev"] == "verify"), 3)
        self.assertIsNone(fakes.branch_files(self.repos, self.full, "run/r1"))

    def test_never_a_block_is_no_blocks(self):
        rc, _ = self.run_agent([{"content": "chatter"}] * 3)
        self.assertEqual(self.done_tag()["kind"], "no_blocks")

    def test_ungranted_rewrite_refused_then_granted(self):
        rewrite = "FILE: README.md\n```\n# changed\n```\n" + FILE_HELLO
        rc, llm = self.run_agent([{"content": rewrite}, {"content": FILE_HELLO}])
        self.assertEqual(rc, 0)
        refused = [p for p in self.progress() if p["ev"] == "refused"]
        self.assertEqual(refused[0]["paths"], ["README.md"])
        self.assertIn("Refused rewrite", llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "README.md"), "# t\n")
        rc, _ = self.run_agent([{"content": rewrite}], task_extra={"may_edit": ["README.md"]})
        self.assertEqual(rc, 0)
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "README.md"), "# changed\n")

    def test_refused_only_counts_as_capability_not_no_blocks(self):
        rewrite = "FILE: README.md\n```\n# changed\n```\n"
        rc, _ = self.run_agent([{"content": rewrite}] * 3)
        self.assertEqual(self.done_tag()["kind"], "calls")

    def test_side_effect_fails_structural_kind(self):
        verify = VERIFY_HELLO.replace("echo verify OK", "echo touched >> README.md; echo verify OK")
        rc, _ = self.run_agent([{"content": FILE_HELLO}], files={".factory/verify.sh": verify, "README.md": "# t\n"})
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual(tag["kind"], "side-effect")
        self.assertIsNone(fakes.branch_files(self.repos, self.full, "run/r1"))

    def test_reasoning_cap(self):
        rc, _ = self.run_agent([{"content": FILE_HELLO, "reasoning": "x" * 100}],
                               task_extra={"max_reasoning_chars": 50})
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual(tag["kind"], "reasoning")
        self.assertEqual(tag["reasoning_chars"], 100)

    def test_llm_http_failure_is_llm_kind(self):
        # a 5xx is retried once (ollama-cloud's 502 afternoon); two in a row fail the run
        rc, llm = self.run_agent([{"status": 503, "content": "down"}, {"status": 503, "content": "down"}])
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual(tag["kind"], "llm")
        self.assertIn("503", tag["error"])
        self.assertEqual(len(llm.requests), 2)

    def test_streamed_reply_is_reassembled(self):
        # through the gate the executor streams (llm_stream): content pieces,
        # a reasoning delta, finish_reason on the last chunk, usage at the end
        rc, llm = self.run_agent([{"content": FILE_HELLO, "reasoning": "hmm" * 5, "tokens_in": 33, "tokens_out": 44}],
                                 task_extra={"llm_stream": True})
        self.assertEqual(rc, 0)
        tag = self.done_tag()
        self.assertEqual((tag["calls"], tag["tokens_in"], tag["tokens_out"], tag["reasoning_chars"]), (1, 33, 44, 15))
        self.assertEqual(llm.streamed, [{"include_usage": True}])
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", "hello.txt"), "hello world\n")

    def test_one_5xx_is_retried_and_the_run_passes(self):
        rc, llm = self.run_agent([{"status": 502, "content": "upstream"}, {"content": FILE_HELLO}])
        self.assertEqual(rc, 0)
        self.assertEqual(self.done_tag()["calls"], 1)  # the failed request is not a counted call
        self.assertEqual(len(llm.requests), 2)

    def test_continuation_counts_calls(self):
        head, tail = FILE_HELLO[:15], FILE_HELLO[15:]
        rc, llm = self.run_agent([{"content": head, "finish_reason": "length"}, {"content": tail}])
        self.assertEqual(rc, 0)
        self.assertEqual(self.done_tag()["calls"], 2)
        self.assertIn("Continue from EXACTLY", llm.requests[1]["messages"][-1]["content"])

    def test_verify_script_protected_and_gitea_paths_ignored(self):
        reply = "FILE: .factory/verify.sh\n```\nexit 0\n```\n" + FILE_HELLO
        rc, _ = self.run_agent([{"content": reply}])
        self.assertEqual(rc, 0)
        self.assertEqual(fakes.branch_file(self.repos, self.full, "run/r1", ".factory/verify.sh"), VERIFY_HELLO)

    # --- stall cut (950 stall-cut, part B item 1) --------------------------

    def test_stall_one_repeat_does_not_cut(self):
        # A A B with max_stall=2: only one repeat happens (at the second A),
        # so the cut never arms and the run proceeds to the passing B
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, llm = self.run_agent([{"content": bad}, {"content": bad}, {"content": FILE_HELLO}],
                                 task_extra={"max_stall": 2, "max_calls": 5})
        self.assertEqual(rc, 0)
        tag = self.done_tag()
        self.assertEqual(tag["outcome"], "ok")
        self.assertEqual(tag["calls"], 3)
        self.assertEqual((tag["distinct_calls"], tag["repeat_calls"]), (2, 1))
        self.assertEqual(len(llm.requests), 3)

    def test_stall_cuts_on_third_call_not_the_call_ceiling(self):
        # the same reply forever with max_stall=2 must cut at the second
        # repeat (call 3) and never reach a much higher call ceiling
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, llm = self.run_agent([{"content": bad}] * 3, task_extra={"max_stall": 2, "max_calls": 10})
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["calls"]), ("fail", "stall", 3))
        self.assertEqual(tag["repeat_calls"], 2)
        self.assertEqual(len(llm.requests), 3)  # never asked a 4th time

    def test_stall_cut_happens_before_verify_runs(self):
        # a cut that still pays for a verification has not saved anything
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, _ = self.run_agent([{"content": bad}, {"content": bad}],
                               task_extra={"max_stall": 1, "max_calls": 5})
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["calls"]), ("fail", "stall", 2))
        # only the first (non-repeat) call reached verify.sh
        self.assertEqual(sum(1 for p in self.progress() if p["ev"] == "verify"), 1)

    def test_max_stall_zero_never_cuts_but_counters_still_ride_the_done_tag(self):
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, llm = self.run_agent([{"content": bad}] * 5, task_extra={"max_stall": 0, "max_calls": 5})
        self.assertEqual(rc, 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["calls"]), ("fail", "calls", 5))
        # always computed, whether or not the cut is armed
        self.assertEqual((tag["distinct_calls"], tag["repeat_calls"], tag["stall_max"]), (1, 4, 5))

    # --- transcript double-record (950 stall-cut, part B item 2) -----------

    def transcript(self):
        with open(os.path.join(self.repos, "t.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_transcript_has_one_row_per_assistant_reply_over_two_iterations(self):
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, _ = self.run_agent([{"content": bad}, {"content": FILE_HELLO}])
        self.assertEqual(rc, 0)
        assistant = [r["content"] for r in self.transcript() if r["role"] == "assistant"]
        self.assertEqual(assistant, [bad, FILE_HELLO])

    def test_transcript_user_and_system_rows_recorded_once(self):
        bad = "FILE: hello.txt\n```\ngoodbye\n```\n"
        rc, _ = self.run_agent([{"content": bad}, {"content": FILE_HELLO}])
        self.assertEqual(rc, 0)
        rows = self.transcript()
        self.assertEqual(sum(1 for r in rows if r["role"] == "system"), 1)
        # the initial task turn, plus one verify-failed feedback turn
        self.assertEqual(sum(1 for r in rows if r["role"] == "user"), 2)


if __name__ == "__main__":
    unittest.main()
