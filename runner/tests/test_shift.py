import os
import tempfile
import unittest

from dark import config, digest, shift
from dark import gitea as G
from dark import ledger as L
from dark.run import RunResult
from tests import fakes
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_ledger import run_end
from tests.test_tasks import make_task, make_templates


class _Flow(unittest.TestCase):
    """The shift fixture: a fake Gitea, a scripted runner, no VMs."""

    @classmethod
    def setUpClass(cls):
        cls.gfake = fakes.FakeGitea()
        cls.gfake.orgs.add("dark")

    @classmethod
    def tearDownClass(cls):
        cls.gfake.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))
        self.host = config.Host(org="dark", gitea_url=self.gfake.url, state_dir=os.path.join(self.tmp, "state"),
                                templates_dir=make_templates(os.path.join(self.tmp, "templates")),
                                admin_token="good", agent_token="agent-tok")
        self.led = L.Ledger(self.host.ledger_path)
        self.gitea = G.Gitea(self.host.gitea_url, "good")
        from dark import tasks
        self.bench = os.path.join(self.tmp, "bench")
        self.tasks = [tasks.load_task(make_task(self.bench, "add-1")),
                      tasks.load_task(make_task(self.bench, "fix-1", cls="repair", may_edit=("main.py",)))]
        self.log = []

    def wide(self):
        """The fixture's cloud window (1000 tokens) cannot hold one reservation; widen it."""
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS.replace("daily_tokens = 1000", "daily_tokens = 10000000")))

    def make(self, outcomes, tier=None, resume=None, launch=None):
        """A Shift whose runner returns scripted outcomes per (task, tier)."""
        sh = shift.Shift(self.cat, self.bud, self.host, self.led, self.gitea, None, log=self.log.append,
                         resume=resume, launch=launch)
        sh.pre.run = lambda need_vm=True: []
        bases = []
        sh.ensure_repo = lambda task, base=("none", None): bases.append((task.id, base)) or "sha"
        calls = []

        def run(task, tier, env, slot=0, think=None, base=None):
            calls.append((task.id, tier))
            outcome, kind = outcomes.get((task.id, tier), outcomes.get((task.id, "*"), ("pass", None)))
            self.led.emit("run.end", **run_end(task=task.id, run=f"{task.id}-{tier}-{len(calls)}", cls=task.cls,
                                               tier=tier, outcome=outcome, fail_kind=kind, shift=sh.id,
                                               paid=self.cat.model(tier).paid,
                                               watts_class=self.cat.model(tier).watts, base=base,
                                               branch=f"run/{task.id}-{tier}-{len(calls)}"))
            return RunResult(run=f"{task.id}-{tier}-{len(calls)}", task=task.id, cls=task.cls, tier=tier,
                             outcome=outcome, fail_kind=kind, detail=kind or "",
                             branch=f"run/{task.id}-{tier}-{len(calls)}")
        sh.runner.run = run
        sh.calls = calls
        sh.bases = bases
        return sh

    def kinds(self, kind):
        return [(e["task"], e.get("reason") or e.get("to")) for e in self.led.events(kind)]


class ShiftFlow(_Flow):
    def test_pass_path_picks_first_open_admitted(self):
        self.wide()
        sh = self.make({})
        self.assertTrue(sh.run(self.tasks))
        self.assertEqual(sh.calls, [("add-1", "local-a"), ("fix-1", "cloud-x")])  # repair: the seed list starts at cloud-x
        self.assertEqual(self.led.last("shift.end")["passes"], 2)
        self.assertEqual(self.led.last("admission")["shift"], sh.id)
        self.assertEqual(sh.parked, [])
        self.assertEqual(sh.blocked, [])

    def test_closed_first_tier_falls_through_to_the_next_open(self):
        sh = self.make({})
        sh.run([self.tasks[1]])  # repair: cloud-x first, but its window cannot hold the reservation
        self.assertEqual(sh.calls, [("fix-1", "local-b")])
        self.assertEqual(sh.parked, [])

    def test_capability_escalates_once(self):
        self.wide()
        sh = self.make({("add-1", "local-a"): ("fail:capability", "calls"),
                        ("add-1", "cloud-x"): ("fail:capability", "calls")})
        sh.run([self.tasks[0]])
        self.assertEqual(sh.calls, [("add-1", "local-a"), ("add-1", "cloud-x")])  # not local-b: one extra run only
        self.assertEqual(self.kinds("escalate"), [("add-1", "cloud-x")])

    def test_escalation_skips_a_closed_tier(self):
        sh = self.make({("add-1", "local-a"): ("fail:capability", "calls")})
        sh.run([self.tasks[0]])  # cloud-x closed by its window -> local-b takes the single escalation
        self.assertEqual(sh.calls, [("add-1", "local-a"), ("add-1", "local-b")])

    def test_structural_blocks_and_never_escalates(self):
        sh = self.make({("add-1", "local-a"): ("fail:structural", "push")})
        sh.run([self.tasks[0]])
        self.assertEqual(sh.calls, [("add-1", "local-a")])
        self.assertEqual(self.kinds("block"), [("add-1", "push: push")])
        self.assertEqual(self.kinds("escalate"), [])

    def test_budget_escalates_once_then_parks(self):
        # a run's budget failure escalates like a capability one;
        # the task parks only when the escalation fails on budget too
        sh = self.make({("add-1", "local-a"): ("fail:budget", "seconds"),
                        ("add-1", "local-b"): ("fail:budget", "reasoning")})
        sh.run([self.tasks[0]])
        self.assertEqual(sh.calls, [("add-1", "local-a"), ("add-1", "local-b")])
        self.assertEqual(len(self.kinds("escalate")), 1)
        self.assertEqual(self.kinds("park"), [("add-1", "reasoning: reasoning")])

    def test_forced_tier_skips_admission_and_escalation(self):
        self.wide()
        sh = self.make({("add-1", "cloud-x"): ("fail:capability", "calls")})
        sh.run(self.tasks, tier="cloud-x")
        self.assertEqual(sh.calls, [("add-1", "cloud-x"), ("fix-1", "cloud-x")])
        self.assertEqual(self.kinds("escalate"), [])

    def test_nothing_open_parks_with_the_reasons(self):
        self.led.emit("run.end", **run_end(tier="local-b", watts_class="high", seconds=3600))  # watts spent
        sh = self.make({})
        sh.run([self.tasks[1]])  # repair: cloud-x window too small, local-b watts exhausted
        self.assertEqual(sh.calls, [])
        task, reason = self.kinds("park")[0]
        self.assertEqual(task, "fix-1")
        self.assertIn("cloud-x: window cloud", reason)
        self.assertIn("local-b: watts", reason)

    def test_run_cap_parks_the_rest(self):
        sh = self.make({})
        sh.run(self.tasks, max_runs=1)
        self.assertEqual(len(sh.calls), 1)
        self.assertEqual(self.kinds("park"), [("fix-1", "shift run cap")])

    def test_refused_preflight_ends_the_shift(self):
        sh = self.make({})
        sh.pre.run = lambda need_vm=True: [__import__("dark.preflight", fromlist=["Check"]).Check("gitea", False, "down")]
        self.assertFalse(sh.run(self.tasks))
        self.assertEqual(sh.calls, [])
        self.assertEqual(self.led.last("preflight.refused")["check"], "gitea")
        self.assertEqual(self.led.last("shift.end")["runs"], 0)

    def test_cooling_tier_is_skipped(self):
        self.led.emit("tier.cooldown", tier="local-a", seconds=10000, reason="429")
        sh = self.make({})
        sh.run([self.tasks[0]])
        self.assertEqual(sh.calls, [("add-1", "local-b")])

    def test_materialize_failure_blocks(self):
        sh = self.make({})
        from dark import tasks

        def bad(task, base=("none", None)):
            raise tasks.TaskError("no template")
        sh.ensure_repo = bad
        sh.run([self.tasks[0]])
        self.assertEqual(sh.calls, [])
        self.assertEqual(self.kinds("block"), [("add-1", "materialize: no template")])

    def test_report_and_publish(self):
        self.wide()
        sh = self.make({("add-1", "local-a"): ("fail:capability", "calls")})
        sh.run(self.tasks)
        text, one = sh.report()
        self.assertIn(f"# dark shift {sh.id}", text)
        self.assertIn("| add-1 | additive | local-a | fail:capability | calls |", text)
        self.assertIn("| fix-1 | repair | cloud-x |", text)
        self.assertIn("| add-1 | additive | cloud-x | pass |", text)
        self.assertTrue(one.startswith(f"dark {sh.id}: 2 pass / 1 fail"), one)
        self.assertIn("cloud", one)
        self.assertIn("watts", one)
        rel = digest.publish(text, sh.id, self.host.ledger_path, self.bench, log=self.log.append)
        self.assertTrue(os.path.exists(os.path.join(self.bench, rel)))
        self.assertTrue(os.path.exists(os.path.join(self.bench, "digest", "latest.md")))
        self.assertTrue(os.path.exists(os.path.join(self.bench, "ledger", "ledger.jsonl")))

    def test_publish_commits_when_bench_is_a_repo(self):
        fakes.git("init", "-q", "-b", "main", self.bench)
        sh = self.make({})
        sh.run(self.tasks)
        text, _ = sh.report()
        digest.publish(text, sh.id, self.host.ledger_path, self.bench, log=self.log.append)
        log = fakes.git("log", "--oneline", cwd=self.bench).stdout
        self.assertIn(f"digest: shift {sh.id}", log)
        self.assertEqual([l for l in self.log if l.startswith("digest publish")], [])


class Chain(_Flow):
    """A follower starts from the delivered tree of a pass in
    this shift, else from the oracle; the base is in the ledger."""

    def setUp(self):
        super().setUp()
        self.wide()
        from dark import tasks
        make_task(self.bench, "obs-01", oracle={"hello.txt": "hello\n"})
        make_task(self.bench, "obs-02", extra='after = "obs-01"\n')
        self.chain = tasks.load_tasks(self.bench, only=["obs-01", "obs-02"])

    def bases(self):
        return [(e["task"], e["base"], e["branch"]) for e in self.led.events("chain.base")]

    def test_pass_delivers_its_branch_to_the_follower(self):
        sh = self.make({})
        sh.run(self.chain)
        self.assertEqual(sh.calls, [("obs-01", "local-a"), ("obs-02", "local-a")])
        self.assertEqual(self.bases(), [("obs-02", "delivered", "run/obs-01-local-a-1")])
        self.assertEqual(sh.bases, [("obs-01", ("none", None)), ("obs-02", ("delivered", "run/obs-01-local-a-1"))])
        self.assertEqual([e.get("base") for e in self.led.events("run.end")], [None, "delivered"])

    def test_escalations_pass_is_the_delivered_base(self):
        sh = self.make({("obs-01", "local-a"): ("fail:capability", None)})
        sh.run(self.chain)
        self.assertEqual([t for t, _ in sh.calls], ["obs-01", "obs-01", "obs-02"])
        self.assertEqual(sh.calls[0][1], "local-a")
        second = sh.calls[1][1]
        self.assertNotEqual(second, "local-a")
        self.assertEqual(self.bases(), [("obs-02", "delivered", f"run/obs-01-{second}-2")])

    def test_failed_step_hands_the_oracle_to_the_follower(self):
        sh = self.make({("obs-01", "*"): ("fail:capability", None)})
        sh.run(self.chain)
        self.assertEqual(self.bases(), [("obs-02", "oracle", None)])
        self.assertEqual(sh.bases[-1], ("obs-02", ("oracle", None)))
        self.assertEqual(self.led.last("run.end")["base"], "oracle")

    def test_chain_base_is_recorded_once_after_the_base_is_in_place(self):
        # exactly one chain.base, written after the base is in place, with the
        # repo on the record
        sh = self.make({("obs-02", "local-a"): ("fail:capability", None)})
        sh.run(self.chain)
        self.assertEqual([t for t, _ in sh.calls], ["obs-01", "obs-02", "obs-02"])
        ev = self.led.events("chain.base")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["task"], ev[0]["base"], ev[0]["repo"]), ("obs-02", "delivered", "dark/t-obs-01"))

    def test_no_chain_base_for_a_step_that_never_ran_and_an_oserror_blocks_only_that_task(self):
        # chain.base must come after the work; an OSError blocks only its own
        # task
        sh = self.make({})

        def bad(task, base=("none", None)):
            if task.after:
                raise OSError("scratch dir gone")
            return "sha"
        sh.ensure_repo = bad
        sh.run(self.chain)
        self.assertEqual(self.led.events("chain.base"), [])
        self.assertEqual(self.kinds("block"), [("obs-02", "materialize: scratch dir gone")])
        self.assertIsNotNone(self.led.last("shift.end"))

    def test_a_follower_run_alone_starts_from_the_oracle(self):
        sh = self.make({})
        sh.run(self.chain[1:])
        self.assertEqual(self.bases(), [("obs-02", "oracle", None)])
        self.assertIn("obs-02: follows obs-01, starts from its oracle tree", self.log)


class ArchivedWorkRepo(_Flow):
    """After `dark archive-work` the old path is a redirect, and the API
    answers on it; the runner must not read that as "the repo is there" and
    push the starting tree into the archived copy."""

    def task_for(self, tid):
        from dark import tasks
        return tasks.load_task(make_task(self.bench, tid))

    def test_a_repo_moved_out_is_created_again_in_the_work_org(self):
        task = self.task_for("arch-moved")
        full = f"{self.host.work_org}/{task.repo_name}"
        self.gitea.create_repo(self.host.work_org, task.repo_name)
        self.gfake.orgs.add("dark-archive")
        self.gitea.transfer_repo(full, "dark-archive")
        self.assertEqual(self.gitea.repo_here(full), (False, "moved"))
        self.assertEqual(shift.Shift.ensure_work_repo(self.make({}), task), full)
        self.assertEqual(self.gitea.repo_here(full), (True, ""))

    def test_a_repo_archived_in_place_blocks_the_task_with_a_named_reason(self):
        task = self.task_for("arch-here")
        full = f"{self.host.work_org}/{task.repo_name}"
        self.gitea.create_repo(self.host.work_org, task.repo_name)
        self.gitea.archive_repo(full)
        self.assertEqual(self.gitea.repo_here(full), (False, "archived"))
        with self.assertRaises(Exception) as cm:
            shift.Shift.ensure_work_repo(self.make({}), task)
        self.assertIn("archived", str(cm.exception))


class Resume(_Flow):
    """--resume: the same shift id continues where it broke. A task with a
    valid pass in that shift is not run again, and a chained task starts
    from the branch that pass delivered."""

    def setUp(self):
        super().setUp()
        self.wide()
        from dark import tasks
        make_task(self.bench, "obs-01", oracle={"hello.txt": "hello\n"})
        make_task(self.bench, "obs-02", extra='after = "obs-01"\n')
        self.chain = tasks.load_tasks(self.bench, only=["obs-01", "obs-02"])

    def broken(self):
        """A first attempt that passed obs-01 and never reached obs-02."""
        sh = self.make({})
        sh.run(self.chain[:1])
        return sh.id

    def test_resume_reuses_the_id_and_skips_the_passed_task(self):
        sid = self.broken()
        sh = self.make({}, resume=sid)
        self.assertEqual(sh.id, sid)
        sh.run(self.chain)
        self.assertEqual(sh.calls, [("obs-02", "local-a")])  # obs-01 not run again
        ev = self.led.last("shift.resume")
        self.assertEqual((ev["shift"], ev["runs"], ev["skipped"]), (sid, 1, ["obs-01"]))

    def test_the_follower_starts_from_the_branch_the_earlier_attempt_delivered(self):
        sid = self.broken()
        delivered = self.led.last("run.end", task="obs-01")["branch"]
        sh = self.make({}, resume=sid)
        sh.run(self.chain)
        self.assertEqual([(e["task"], e["base"], e["branch"]) for e in self.led.events("chain.base")],
                         [("obs-02", "delivered", delivered)])
        self.assertEqual(sh.bases, [("obs-02", ("delivered", delivered))])

    def test_the_launch_arguments_are_recorded_and_read_back(self):
        launch = {"tasks": ["obs-01"], "task_dir": None, "tier": "local-a", "think": "none",
                  "arm": "smoke-local", "slot": 0, "work_org": "dark-runs"}
        sh = self.make({}, launch=launch)
        sh.run(self.chain[:1])
        self.assertEqual(self.led.last("shift.start")["launch"], launch)
        self.assertEqual(shift.launch_of(self.led, sh.id), launch)

    def test_a_shift_with_no_launch_record_reads_back_as_none(self):
        sh = self.make({})
        sh.run(self.chain[:1])
        self.assertIsNone(shift.launch_of(self.led, sh.id))

    def test_resume_args_fills_in_what_the_command_line_left_off(self):
        rec = {"tier": "local-a", "think": "none", "arm": "a", "slot": 0,
               "tasks": ["obs-01"], "task_dir": None, "work_org": "dark-runs"}
        merged, why = shift.resume_args(rec, {k: None for k in shift.LAUNCH_KEYS})
        self.assertIsNone(why)
        # a launch recorded before a key existed reads back as None for it, and
        # None is what the runner treats as "not set", so the resume repeats it
        self.assertEqual(merged, dict(rec, frozen=None, no_adapt=None, executor=None, max_stall=None))

    def test_resume_args_refuses_a_differing_value_and_names_both(self):
        rec = {"tier": "local-a", "think": "none", "arm": "a", "slot": 0,
               "tasks": ["obs-01"], "task_dir": None, "work_org": "dark-runs"}
        given = {k: None for k in shift.LAUNCH_KEYS}
        given["think"] = "high"
        merged, why = shift.resume_args(rec, given)
        self.assertIsNone(merged)
        self.assertIn("'high'", why)
        self.assertIn("'none'", why)

    def test_a_failed_task_is_run_again_on_resume(self):
        sh = self.make({("obs-01", "*"): ("fail:capability", None)})
        sh.run(self.chain[:1])
        sh2 = self.make({}, resume=sh.id)
        sh2.run(self.chain[:1])
        self.assertEqual([t for t, _ in sh2.calls], ["obs-01"])
        self.assertEqual(self.led.last("shift.resume")["skipped"], [])

    def test_a_voided_pass_is_not_a_valid_pass(self):
        sid = self.broken()
        run = self.led.last("run.end", task="obs-01")["run"]
        self.led.emit("run.void", task="obs-01", run=run, reason="harness fault")
        sh = self.make({}, resume=sid)
        sh.run(self.chain[:1])
        self.assertEqual([t for t, _ in sh.calls], ["obs-01"])

    def test_a_pass_of_another_shift_is_not_resumed(self):
        self.broken()  # its own id
        sh = self.make({}, resume="20260101-000000-factory")
        sh.run(self.chain[:1])
        self.assertEqual([t for t, _ in sh.calls], ["obs-01"])
        self.assertEqual(self.led.last("shift.resume")["runs"], 0)

    def test_a_fresh_shift_never_reads_earlier_records(self):
        self.broken()
        sh = self.make({})
        sh.run(self.chain[:1])
        self.assertEqual([t for t, _ in sh.calls], ["obs-01"])
        self.assertIsNone(self.led.last("shift.resume"))


if __name__ == "__main__":
    unittest.main()
