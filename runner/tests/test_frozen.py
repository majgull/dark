"""dark/frozen.py: the pinned envelope, and the flag that turns the
ledger-derived adaptations off.

Both exist for one measured failure. On 2026-09-04 three rounds of one tier
on one task set were reported as the same configuration; they were not,
because the soft envelope is computed from the ledger's own recent passes
and the ledger grew between the rounds. The third round was cut off at 450
seconds where the first had 900 and produced three budget failures that read
as the model getting worse.
"""

import os
import tempfile
import unittest

from dark import config, frozen, shift
from dark import gitea as G
from dark import ledger as L
from tests.test_config import BUDGETS, MODELS, write_conf

GOOD = """
set = "rq2-dsf"
note = "RQ2, deepseek both arms, 2026-09-05"

[class.additive]
calls = 6
seconds = 900
max_reasoning_chars = 40000
think = "low"

[class.mechanical]
calls = 4
seconds = 300
max_reasoning_chars = 10000
think = "none"
"""


def write(tmp, text, name="f.toml"):
    p = os.path.join(tmp, name)
    with open(p, "w") as f:
        f.write(text)
    return p


class File(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))

    def test_a_good_file_gives_the_envelope_it_names_on_the_frozen_basis(self):
        fr = frozen.load(write(self.tmp, GOOD))
        env = fr.envelope("additive")
        self.assertEqual((env.calls, env.seconds, env.max_reasoning_chars), (6, 900, 40000))
        self.assertEqual(env.basis, "frozen")
        self.assertEqual(env.runs, 1)          # one tier per task unless the file says otherwise
        self.assertEqual(fr.think("additive"), "low")
        self.assertEqual(fr.set_name, "rq2-dsf")

    def test_the_recorded_hash_is_of_the_file_bytes(self):
        import hashlib
        path = write(self.tmp, GOOD)
        fr = frozen.load(path)
        with open(path, "rb") as f:
            self.assertEqual(fr.sha256, hashlib.sha256(f.read()).hexdigest())
        d = fr.as_dict()
        self.assertEqual((d["set"], d["file"], d["sha256"]), ("rq2-dsf", "f.toml", fr.sha256))

    def test_one_byte_changed_is_a_different_hash(self):
        a = frozen.load(write(self.tmp, GOOD, "a.toml"))
        b = frozen.load(write(self.tmp, GOOD.replace("seconds = 900", "seconds = 901"), "b.toml"))
        self.assertNotEqual(a.sha256, b.sha256)

    def test_a_class_the_file_does_not_pin_is_refused_by_name(self):
        fr = frozen.load(write(self.tmp, GOOD))
        self.assertFalse(fr.has("repair"))
        with self.assertRaises(frozen.FrozenError) as e:
            fr.envelope("repair")
        self.assertIn("repair", str(e.exception))

    def test_covers_names_every_class_the_task_list_needs_and_the_file_lacks(self):
        fr = frozen.load(write(self.tmp, GOOD))

        class T:
            def __init__(self, cls):
                self.cls = cls
        self.assertEqual(frozen.covers(fr, [T("additive"), T("repair"), T("mechanical"), T("repair")]),
                         ["repair"])

    def test_a_level_the_budgets_do_not_define_is_refused(self):
        fr = frozen.load(write(self.tmp, GOOD.replace('think = "low"', 'think = "deep"')))
        why = frozen.check_levels(fr, self.bud)
        self.assertIn("deep", why)
        self.assertIn("additive", why)

    def test_a_defined_level_passes_the_check(self):
        self.assertEqual(frozen.check_levels(frozen.load(write(self.tmp, GOOD)), self.bud), "")

    def test_refusals_name_the_file_and_the_field(self):
        for text, want in [
            (GOOD.replace('set = "rq2-dsf"', ""), "set"),
            (GOOD.replace("calls = 6", "calls = 0"), "calls"),
            (GOOD.replace("calls = 6", 'calls = "six"'), "calls"),
            (GOOD.replace("seconds = 900", "second = 900"), "seconds"),
            (GOOD + "\n[class.repair]\ncalls = 8\nseconds = 900\nmax_reasoning_chars = 40000\nsecods = 1\n",
             "secods"),
            ('set = "x"\n', "class"),
        ]:
            with self.subTest(want=want):
                with self.assertRaises(frozen.FrozenError) as e:
                    frozen.load(write(self.tmp, text))
                self.assertIn(want, str(e.exception))
                self.assertIn("f.toml", str(e.exception))

    def test_a_file_that_is_not_there_is_one_line_not_a_traceback(self):
        with self.assertRaises(frozen.FrozenError) as e:
            frozen.load(os.path.join(self.tmp, "nope.toml"))
        self.assertIn("nope.toml", str(e.exception))


class InAShift(unittest.TestCase):
    """The shift reads the file instead of the ledger, and --no-adapt turns
    admission, cooling and escalation off."""

    @classmethod
    def setUpClass(cls):
        cls.gfake = __import__("tests.fakes", fromlist=["fakes"]).FakeGitea()
        cls.gfake.orgs.add("dark")

    @classmethod
    def tearDownClass(cls):
        cls.gfake.close()

    def setUp(self):
        from tests.test_tasks import make_task, make_templates
        from dark import tasks
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(
            self.tmp, MODELS, BUDGETS.replace("daily_tokens = 1000", "daily_tokens = 10000000")))
        self.host = config.Host(org="dark", gitea_url=self.gfake.url,
                                state_dir=os.path.join(self.tmp, "state"),
                                templates_dir=make_templates(os.path.join(self.tmp, "templates")),
                                admin_token="good", agent_token="agent-tok")
        self.led = L.Ledger(self.host.ledger_path)
        self.gitea = G.Gitea(self.host.gitea_url, "good")
        self.bench = os.path.join(self.tmp, "bench")
        self.tasks = [tasks.load_task(make_task(self.bench, "add-1"))]
        self.log = []
        self.envs = []

    def make(self, outcome=("pass", None), **kw):
        from dark.run import RunResult
        from tests.test_ledger import run_end
        sh = shift.Shift(self.cat, self.bud, self.host, self.led, self.gitea, None,
                         log=self.log.append, **kw)
        sh.pre.run = lambda need_vm=True: []
        sh.ensure_repo = lambda task, base=("none", None): "sha"
        n = [0]

        def run(task, tier, env, slot=0, think=None, base=None):
            n[0] += 1
            self.envs.append((task.id, tier, env, think))
            rid = f"{task.id}-{n[0]}"
            self.led.emit("run.end", **run_end(task=task.id, run=rid, cls=task.cls, tier=tier,
                                               outcome=outcome[0], fail_kind=outcome[1], shift=sh.id,
                                               paid=self.cat.model(tier).paid,
                                               watts_class=self.cat.model(tier).watts,
                                               branch=f"run/{rid}"))
            return RunResult(run=rid, task=task.id, cls=task.cls, tier=tier, outcome=outcome[0],
                             fail_kind=outcome[1], detail="", branch=f"run/{rid}")
        sh.runner.run = run
        return sh

    def frozen_file(self):
        return frozen.load(write(self.tmp, GOOD))

    def test_the_shift_uses_the_pinned_limits_not_the_computed_ones(self):
        fr = frozen.load(write(self.tmp, GOOD.replace("seconds = 900", "seconds = 123")
                                            .replace("calls = 6", "calls = 5")))
        sh = self.make(frozen=fr)
        sh.run(self.tasks, tier="local-a")
        _, _, env, think = self.envs[0]
        self.assertEqual((env.basis, env.calls, env.seconds), ("frozen", 5, 123))
        self.assertEqual(think, "low")   # the level comes from the file too

    def test_the_runner_carries_the_file_name_and_hash_for_every_run_start(self):
        fr = self.frozen_file()
        sh = self.make(frozen=fr)
        self.assertEqual(sh.runner.frozen, {"set": "rq2-dsf", "file": "f.toml", "sha256": fr.sha256})

    def test_without_a_frozen_file_the_runner_records_none(self):
        self.assertIsNone(self.make().runner.frozen)

    def test_the_log_names_the_set_and_the_hash_so_a_round_can_be_traced(self):
        fr = self.frozen_file()
        self.make(frozen=fr).run(self.tasks, tier="local-a")
        line = [x for x in self.log if "frozen envelope" in x]
        self.assertTrue(line)
        self.assertIn("rq2-dsf", line[0])
        self.assertIn(fr.sha256[:12], line[0])

    def test_no_adapt_without_a_tier_is_refused_before_anything_runs(self):
        sh = self.make(no_adapt=True)
        self.assertFalse(sh.run(self.tasks))
        self.assertEqual(self.envs, [])
        self.assertIsNone(self.led.last("shift.start"))
        self.assertIn("--tier", self.led.last("preflight.refused")["detail"])

    def test_no_adapt_writes_no_admission_table(self):
        self.make(no_adapt=True).run(self.tasks, tier="local-a")
        self.assertIsNone(self.led.last("admission"))
        self.assertTrue([x for x in self.log if "adaptation off" in x])

    def test_a_shift_that_adapts_still_writes_one(self):
        self.make().run(self.tasks, tier="local-a")
        self.assertIsNotNone(self.led.last("admission"))

    def test_no_adapt_needs_a_named_tier_and_a_named_tier_never_escalates(self):
        # the two halves of "no escalation" under --no-adapt: the flag refuses
        # to run without --tier, and a forced tier is the path that already
        # takes one run and stops. The clause in shift.run is the third guard.
        sh = self.make(outcome=("fail:capability", None), no_adapt=True)
        self.assertFalse(sh.run(self.tasks))
        sh = self.make(outcome=("fail:capability", None), no_adapt=True)
        sh.run(self.tasks, tier="local-a")
        self.assertEqual(len(self.envs), 1)
        self.assertIsNone(self.led.last("escalate"))

    def test_the_frozen_envelope_allows_one_run_so_nothing_retries_on_it(self):
        # without the file this shift escalates: the fixture class allows two
        # runs and admission offers a second tier
        loose = self.make(outcome=("fail:capability", None))
        loose.run(self.tasks)
        self.assertEqual(len(self.envs), 2)
        self.assertIsNotNone(self.led.last("escalate"))
        self.envs.clear()
        sh = self.make(outcome=("fail:capability", None), frozen=self.frozen_file())
        sh.run(self.tasks)
        self.assertEqual([e.runs for _, _, e, _ in self.envs], [1])
        self.assertEqual(len(self.envs), 1)

    def test_the_frozen_file_and_the_switch_are_repeated_by_a_resume(self):
        self.assertIn("frozen", shift.LAUNCH_KEYS)
        self.assertIn("no_adapt", shift.LAUNCH_KEYS)
        rec = {k: None for k in shift.LAUNCH_KEYS} | {"frozen": "/a/f.toml", "no_adapt": True}
        merged, why = shift.resume_args(rec, {k: None for k in shift.LAUNCH_KEYS})
        self.assertIsNone(why)
        self.assertEqual((merged["frozen"], merged["no_adapt"]), ("/a/f.toml", True))
        _, why = shift.resume_args(rec, {k: None for k in shift.LAUNCH_KEYS} | {"frozen": "/b/g.toml"})
        self.assertIn("g.toml", why)


if __name__ == "__main__":
    unittest.main()
