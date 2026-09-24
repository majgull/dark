"""The run driver end to end: a real agent.py and stager.py run as
subprocesses (the 'VM'), against the in-memory Gitea, a scripted model
server and a bare origin over file://. Real time, short watchdog numbers."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from dark import budget, config, gate, spec
from dark import ledger as L
from dark import run as R
from dark import vm
from dark import gitea as G
from tests import fakes
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_tasks import VERIFY, make_task, make_templates

FAST_BUDGETS = BUDGETS.replace("heartbeat_seconds = 10", "heartbeat_seconds = 1") \
    .replace("silent_kill_seconds = 30", "silent_kill_seconds = 4") \
    .replace("poll_seconds = 1", "poll_seconds = 0.2")
FILE_HELLO = "FILE: hello.txt\n```\nhello world\n```\n"


class FakeMeter:
    """dark/power.py's shape with fixed readings: 0.5 Wh package, 1.5 Wh GPUs."""
    started = 0

    def start(self):
        FakeMeter.started += 1
        return self

    def stop(self):
        return {"wh_cpu": 0.5, "wh_gpu": 1.5, "wh": 2.0, "wh_overhead": None,
                "power": {"cpu_host": "px", "gpu_host": "vm", "gpu_samples": 4, "gpu_devices": 2}}


class DeadGpuMeter(FakeMeter):
    """The GPU sampler never produced two samples: wh_gpu is null."""

    def stop(self):
        return {"wh_cpu": 0.5, "wh_gpu": None, "wh": 0.5, "wh_overhead": None,
                "power": {"cpu_host": "px", "gpu_host": "vm", "gpu_samples": 0, "gpu_devices": 0,
                          "errors": ["gpu: vm: fewer than two samples"]}}


class LocalRunner(R.Runner):
    """launch() runs the injected script as a local subprocess."""

    def __init__(self, *a, **kw):
        self.holds = []  # keep-awake leases asked for (seconds), never a real gate
        kw.setdefault("hold", lambda s: (self.holds.append(s), (True, "fake"))[1])
        kw.setdefault("meter", FakeMeter)
        super().__init__(*a, **kw)
        self.procs = {}
        self.launched = []
        self.skip = set()  # names that never start (silent VM)

    def launch(self, vmid, name, files, runcmd):
        self.launched.append((vmid, name))
        if name in self.skip:
            return
        d = tempfile.mkdtemp(prefix=f"vm-{name}-")
        env = dict(os.environ)
        for path, (content, _) in files.items():
            p = os.path.join(d, os.path.basename(path))
            with open(p, "w") as f:
                f.write(content)
        env["DARK_TASK"] = os.path.join(d, "task.json")
        env["DARK_WORK"] = os.path.join(d, "work")
        env["DARK_TRANSCRIPT"] = os.path.join(d, "transcript.jsonl")
        script = "agent.py" if "/opt/agent.py" in files else "stager.py"
        self.procs[name] = subprocess.Popen([sys.executable, "-W", "ignore", os.path.join(d, script)], env=env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def net_ip(self, vmid):
        return "10.0.0.1"

    def reap(self, vmid, name):
        p = self.procs.pop(name, None)
        if p is not None:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        return True


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gitea_fake = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.gitea_fake.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, FAST_BUDGETS))
        self.repos = os.path.join(self.tmp, "repos")
        self.full = "dark/t-hello"
        self.gitea_fake.repos[self.full] = {"archived": False, "branches": {"main"}}
        self.gitea_fake.issues[self.full] = {}
        self.git_url = fakes.make_origin(self.repos, self.full, {".dark/verify.sh": VERIFY, "README.md": "# t\n"})
        self.host = config.Host(org="dark", gitea_url=self.gitea_fake.url, gitea_lan_url=self.gitea_fake.url,
                                git_lan_url=self.git_url, state_dir=os.path.join(self.tmp, "state"),
                                templates_dir=make_templates(os.path.join(self.tmp, "templates")),
                                admin_token="good", agent_token="agent-tok")
        os.makedirs(self.host.abort_dir)
        self.led = L.Ledger(self.host.ledger_path)
        self.gitea = G.Gitea(self.host.gitea_url, "good")
        self.task = None
        self.llm = None

    def tearDown(self):
        if self.llm:
            self.llm.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, replies, tier="local-a", acceptance=None, task_kw=None):
        self.llm = fakes.FakeLLM(replies)
        # point the tier's provider at the scripted server
        prov = self.cat.providers[self.cat.model(tier).provider]
        self.cat.providers[prov.name] = config.Provider(prov.name, f"{self.llm.url}/v1", prov.catalog, prov.health, prov.wake, prov.window)
        kw = dict(acceptance=acceptance) if acceptance else {}
        kw.update(task_kw or {})
        from dark import tasks
        self.task = tasks.load_task(make_task(os.path.join(self.tmp, "bench"), "hello", **kw))
        return LocalRunner(self.cat, self.bud, self.host, self.led, self.gitea, None, log=lambda *a: None, shift="s1")

    def env(self, **over):
        e = budget.envelope(self.bud, self.led, "additive")
        d = e.as_dict()
        d.update(over)
        d.pop("basis")
        return budget.Envelope(**d, basis="test")

    def transitions(self, run_id):
        return [(e["frm"], e["to"]) for e in self.led.events("run.transition") if e["run"] == run_id]


class Outcomes(Base):
    def test_pass_end_to_end(self):
        r = self.runner([{"content": FILE_HELLO}])
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertEqual((res.checks_ok, res.checks_total, res.calls), (1, 1, 1))
        self.assertEqual(self.transitions(res.run), [("queued", "preflight"), ("preflight", "ready"), ("ready", "executing"),
                                                     ("executing", "verifying"), ("verifying", "staging"), ("staging", "pass")])
        end = self.led.last("run.end")
        self.assertEqual((end["outcome"], end["tier"], end["paid"], end["watts_class"], end["cls"]), ("pass", "local-a", False, "low", "additive"))
        self.assertGreaterEqual(end["wall_seconds"], end["seconds"])
        self.assertEqual(end["tokens_in"], 10)
        # the measured energy over the window rides on the run.end; overhead is null (NOT MEASURED)
        self.assertEqual((end["wh_cpu"], end["wh_gpu"], end["wh"], end["wh_overhead"]), (0.5, 1.5, 2.0, None))
        self.assertEqual(end["power"]["gpu_devices"], 2)
        self.assertIsNone(end["restaged_from"])
        self.assertEqual(self.led.last("run.start")["envelope"]["calls"], 6)
        self.assertEqual([n for _, n in r.launched], ["dark-x0", "dark-s0"])
        self.assertEqual(self.gitea_fake.issues[self.full][res.issue]["state"], "closed")
        self.assertTrue(any(b.startswith("RUN-END pass") for b in self.gitea_fake.bodies(self.full, res.issue)))
        self.assertIn("hello.txt", fakes.branch_files(self.repos, self.full, res.branch))

    def test_delete_plus_file_of_one_path_is_a_rewrite(self):
        # a reply can emit "DELETE: x" right before "FILE: x"; the delete
        # must not un-stage the write
        r = self.runner([{"content": "DELETE: hello.txt\n" + FILE_HELLO}])
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertIn("hello.txt", fakes.branch_files(self.repos, self.full, res.branch))

    def test_acceptance_red_is_capability(self):
        r = self.runner([{"content": "FILE: hello.txt\n```\nnope\n```\n"}])
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "fail:capability")
        self.assertEqual((res.checks_ok, res.checks_total), (0, 1))
        self.assertEqual(self.transitions(res.run)[-1], ("staging", "fail:capability"))
        self.assertIsNone(res.fail_kind)

    def test_calls_exhausted_is_capability_from_executing(self):
        bad = "FILE: other.txt\n```\nx\n```\n"
        r = self.runner([{"content": bad}] * 6)
        res = r.run(self.task, "local-a", self.env(calls=2))
        self.assertEqual((res.outcome, res.fail_kind, res.calls), ("fail:capability", "calls", 2))
        tr = self.transitions(res.run)
        self.assertIn(("executing", "verifying"), tr)
        self.assertIn(("verifying", "executing"), tr)
        self.assertEqual(tr[-1], ("executing", "fail:capability"))
        self.assertEqual(len([n for _, n in r.launched]), 1)  # no staging VM

    def test_side_effect_is_structural_from_verifying(self):
        verify = VERIFY.replace("echo verify OK", "echo touched >> README.md; echo verify OK")
        with open(os.path.join(self.host.templates_dir, "python", ".dark", "verify.sh"), "w") as f:
            f.write(verify)
        fakes.git("push", "-q", "--delete", "origin", "main", cwd=os.path.join(self.repos, "seed-dark-t-hello"), check=False)
        self.git_url = fakes.make_origin(os.path.join(self.tmp, "repos2"), self.full, {".dark/verify.sh": verify, "README.md": "# t\n"})
        self.host.git_lan_url = self.git_url
        r = self.runner([{"content": FILE_HELLO}])
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "side-effect"))
        self.assertEqual(self.transitions(res.run)[-1], ("verifying", "fail:structural"))

    def test_reasoning_cap_is_budget(self):
        r = self.runner([{"content": FILE_HELLO, "reasoning": "x" * 100}])
        res = r.run(self.task, "local-a", self.env(max_reasoning_chars=50))
        self.assertEqual((res.outcome, res.fail_kind), ("fail:budget", "reasoning"))
        self.assertEqual(res.reasoning_chars, 100)

    def test_llm_failure_is_structural_and_429_cools_the_tier(self):
        r = self.runner([{"status": 429, "content": "slow down"}], tier="cloud-x")
        res = r.run(self.task, "cloud-x", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "llm"))
        self.assertEqual(self.led.last("tier.cooldown")["tier"], "cloud-x")
        self.assertEqual(self.led.last("window.exhausted")["provider"], "cloud")
        self.assertTrue(self.led.last("run.end")["paid"])
        self.assertIsNone(self.led.last("run.end")["watts_class"])

    def test_silent_vm_is_killed_structural(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.skip.add("dark-x0")
        t0 = time.time()
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "silent"))
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(self.led.last("watchdog.kill")["run"], res.run)
        self.assertEqual(self.transitions(res.run)[-1], ("executing", "fail:structural"))

    def test_envelope_seconds_with_live_heartbeat_is_budget(self):
        r = self.runner([{"content": FILE_HELLO, "sleep": 3}])
        res = r.run(self.task, "local-a", self.env(seconds=1))
        self.assertEqual((res.outcome, res.fail_kind), ("fail:budget", "seconds"))

    def test_abort_marker(self):
        r = self.runner([{"content": FILE_HELLO, "sleep": 3}])
        with open(os.path.join(self.host.abort_dir, "all"), "w") as f:
            f.write("x")
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "abort")
        self.assertEqual(self.led.last("abort")["run"], res.run)
        self.assertEqual(self.transitions(res.run)[-1], ("executing", "abort"))
        os.remove(os.path.join(self.host.abort_dir, "all"))

    def test_stager_silent_is_structural(self):
        r = self.runner([{"content": FILE_HELLO}], task_kw={"extra": "stage_timeout = 1\n"})
        r.skip.add("dark-s0")
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "stage"))
        self.assertEqual(self.transitions(res.run)[-1], ("staging", "fail:structural"))

    def test_vm_start_failure(self):
        r = self.runner([{"content": FILE_HELLO}])

        def boom(vmid, name, files, runcmd):
            raise vm.VMError("qm clone failed")
        r.launch = boom
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "env"))
        self.assertEqual(self.transitions(res.run)[-1], ("ready", "fail:structural"))

    def test_a_run_records_what_it_checked_about_its_own_machinery(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.run(self.task, "local-a", self.env())
        a = self.led.last("run.end")["asserts"]
        self.assertEqual(a["sensors"], {"cpu": True, "gpu": True})
        self.assertTrue(a["sensors_alive"])
        self.assertTrue(a["vms_destroyed"])
        self.assertEqual(sorted(a["vms"]), ["dark-s0", "dark-x0"])

    def test_a_dead_sensor_is_recorded_as_a_miss_not_as_a_number(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.meter = DeadGpuMeter
        r.run(self.task, "local-a", self.env())
        rec = self.led.last("run.end")
        self.assertIsNone(rec["wh_gpu"])
        self.assertEqual(rec["asserts"]["sensors"], {"cpu": True, "gpu": False})
        self.assertFalse(rec["asserts"]["sensors_alive"])

    def test_a_vm_that_survived_its_reap_is_recorded(self):
        r = self.runner([{"content": FILE_HELLO}])
        real_reap = r.reap
        r.reap = lambda vmid, name: False if name == "dark-x0" else real_reap(vmid, name)
        r.run(self.task, "local-a", self.env())
        a = self.led.last("run.end")["asserts"]
        self.assertFalse(a["vms_destroyed"])
        self.assertFalse(a["vms"]["dark-x0"])

    def test_a_run_that_launched_no_vm_asserts_nothing_about_vms(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.gitea = G.Gitea("http://127.0.0.1:1", "x")
        r.run(self.task, "local-a", self.env())
        self.assertIsNone(self.led.last("run.end")["asserts"]["vms_destroyed"])

    def test_gitea_down_is_a_structural_record_with_a_named_reason(self):
        # it happens before the model's first call and is not the tier's
        # doing; as a refusal it left no record at all
        r = self.runner([{"content": FILE_HELLO}])
        r.gitea = G.Gitea("http://127.0.0.1:1", "x")
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "gitea"))
        rec = self.led.last("run.end")
        self.assertEqual(rec["outcome"], "fail:structural")
        self.assertEqual(rec["reason"], spec.STRUCTURAL_REASONS["gitea"])


class SessionArm(Base):
    def push_solution(self, branch, files):
        work = os.path.join(self.tmp, "session-work")
        fakes.git("clone", "-q", f"{self.git_url}/{self.full}.git", work)
        for rel, content in files.items():
            with open(os.path.join(work, rel), "w") as f:
                f.write(content)
        fakes.git("add", "-A", cwd=work)
        fakes.git("-c", "user.name=s", "-c", "user.email=s@x", "commit", "-qm", "session solution", cwd=work)
        fakes.git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)
        self.gitea_fake.repos[self.full]["branches"].add(branch)

    def test_stage_only_pass_with_unmeasured_tokens(self):
        r = self.runner([])
        self.push_solution("run/session-1", {"hello.txt": "hello from a session\n"})
        res = r.stage_only(self.task, "run/session-1", "cloud-x", "session-x", {"seconds": 42})
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertEqual(self.transitions(res.run), [("queued", "preflight"), ("preflight", "ready"), ("ready", "executing"),
                                                     ("executing", "verifying"), ("verifying", "staging"), ("staging", "pass")])
        end = self.led.last("run.end")
        self.assertEqual((end["arm"], end["tier"], end["paid"], end["seconds"]), ("session-x", "cloud-x", True, 42))
        self.assertIsNone(end["tokens_in"])
        self.assertIsNone(end["calls"])
        self.assertEqual([n for _, n in r.launched], ["dark-s0"])
        self.assertEqual(len(self.led.runs(arm="session-x")), 1)
        # the digest shows NOT MEASURED as n/a, never a number
        from dark import digest
        text, _ = digest.render(self.led, self.cat, self.bud, "s1", time.time())
        self.assertIn("| hello | additive | cloud-x (session-x) | pass |  | n/a (n/a) | n/a/n/a |", text)

    def test_stage_only_rejudging_a_branch_copies_the_original_run_facts(self):
        # a delivered branch re-staged against a tightened acceptance: the
        # verdict is new, calls/tokens/think are the original run's, and the
        # record names that run; no meter runs for a staging (wh is null)
        r = self.runner([])
        self.push_solution("run/session-1", {"hello.txt": "hello from a session\n"})
        first = r.stage_only(self.task, "run/session-1", "cloud-x", "session-x",
                             {"seconds": 42, "calls": 7, "tokens_in": 100, "tokens_out": 50, "reasoning_chars": 9})
        self.assertEqual(first.outcome, "pass", first.detail)
        again = r.stage_only(self.task, "run/session-1", "cloud-x", "session-x", {"seconds": None, "calls": None})
        self.assertEqual(again.outcome, "pass", again.detail)
        end = self.led.last("run.end")
        self.assertEqual((end["restaged_from"], end["calls"], end["seconds"], end["wh"]), (first.run, 7, 42, None))
        # a first staging (no earlier run on that branch) copies nothing and names no run
        self.assertIsNone(self.led.runs(arm="session-x")[0]["restaged_from"])

    def test_stage_only_missing_branch_is_structural(self):
        # after the session's last push and not its doing: the branch it
        # delivered never reached the work repo
        r = self.runner([])
        res = r.stage_only(self.task, "run/nope", "cloud-x", "session-x")
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "push"))
        self.assertEqual(self.led.last("run.end")["reason"], spec.STRUCTURAL_REASONS["push"])

    def test_stage_only_acceptance_red(self):
        r = self.runner([])
        self.push_solution("run/session-2", {"hello.txt": "wrong\n"})
        res = r.stage_only(self.task, "run/session-2", "cloud-x", "session-x")
        self.assertEqual((res.outcome, res.checks_ok, res.checks_total), ("fail:capability", 0, 1))


class ThinkingBudget(Base):
    """The request carries response + thinking budget, and a reply that hit
    the ceiling is counted in the ledger."""

    def test_budget_and_truncation_count(self):
        import dataclasses
        r = self.runner([{"content": "FILE: hello.txt\n```\nhello", "finish_reason": "length"},
                         {"content": " world\n```\n"}])
        self.cat.models["local-a"] = dataclasses.replace(self.cat.models["local-a"], thinking_tokens=1000)
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "pass", res.detail)
        bodies = self.llm.requests
        self.assertEqual(bodies[0]["max_tokens"], 2048 + 1000)   # provider default thinking: the allowance
        for k in ("reasoning_effort", "chat_template_kwargs"):
            self.assertNotIn(k, bodies[0])
        end = self.led.last("run.end")
        self.assertEqual((end["calls"], end["truncated"], end["requests"], end["cuts"]), (2, 1, 2, 0))

    def test_no_think_field_by_default(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.run(self.task, "local-a", self.env())
        body = self.llm.requests[0]
        self.assertEqual(body["max_tokens"], 2048)
        for k in ("think", "reasoning_effort", "chat_template_kwargs", "_cut_at"):
            self.assertNotIn(k, body)
        end = self.led.last("run.end")
        self.assertEqual((end["truncated"], end["cuts"], end["requests"], end["think"]), (0, 0, 1, None))

    def with_api(self, r, api):
        import dataclasses
        prov = self.cat.providers[self.cat.model("local-a").provider]
        self.cat.providers[prov.name] = dataclasses.replace(prov, think_api=api, stream=True)
        return r

    def test_level_cuts_thinking_and_asks_for_the_answer(self):
        # the executor cuts the visible thinking at dark's level and asks for
        # the answer with the thinking so far; one call in the envelope, two
        # provider requests, the same for every provider
        r = self.with_api(self.runner([{"content": "", "reasoning": "think " * 60},
                                       {"content": FILE_HELLO, "reasoning": "brief"}]), "chat_template")
        r.budgets.think["levels"]["low"] = 100
        res = r.run(self.task, "local-a", self.env(), think="low")
        self.assertEqual(res.outcome, "pass", res.detail)
        first, second = self.llm.requests
        self.assertEqual(first["max_tokens"], 2048 + 34)          # response + ceil(100 / 3)
        self.assertEqual(first["chat_template_kwargs"], {"enable_thinking": True})
        self.assertNotIn("_cut_at", first)
        self.assertEqual(second["max_tokens"], 2048)
        self.assertEqual(second["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIn("cut at the budget of 360 characters", second["messages"][-1]["content"])
        self.assertIn("think think", second["messages"][-1]["content"])
        end = self.led.last("run.end")
        self.assertEqual((end["calls"], end["cuts"], end["requests"], end["think"], end["think_api"]),
                         (1, 1, 2, "low", "chat_template"))
        self.assertEqual(end["reasoning_chars"], 360 + 5)
        start = self.led.last("run.start")
        self.assertEqual((start["envelope"]["think"], start["envelope"]["think_chars"]), ("low", 100))

    def test_level_preset_is_requested_for_both_provider_requests(self):
        # a level's own router preset (native reasoning budget) serves the call
        # and the cut re-ask alike, so the router never swaps instances mid-call
        import dataclasses
        self.cat.models["local-a"] = dataclasses.replace(self.cat.model("local-a"), think_presets=(("low", "local-a-low"),))
        r = self.with_api(self.runner([{"content": "", "reasoning": "think " * 60},
                                       {"content": FILE_HELLO}]), "chat_template")
        r.budgets.think["levels"]["low"] = 100
        res = r.run(self.task, "local-a", self.env(), think="low")
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertEqual([b["model"] for b in self.llm.requests], ["local-a-low", "local-a-low"])

    def test_level_without_a_preset_requests_the_tier_itself(self):
        import dataclasses
        self.cat.models["local-a"] = dataclasses.replace(self.cat.model("local-a"), think_presets=(("low", "local-a-low"),))
        r = self.with_api(self.runner([{"content": FILE_HELLO}]), "chat_template")
        r.run(self.task, "local-a", self.env(), think="medium")
        self.assertEqual(self.llm.requests[-1]["model"], "local-a")

    def test_level_hint_for_an_effort_provider(self):
        r = self.with_api(self.runner([{"content": FILE_HELLO}]), "reasoning_effort")
        res = r.run(self.task, "local-a", self.env(), think="none")
        self.assertEqual(res.outcome, "pass", res.detail)
        body = self.llm.requests[0]
        self.assertEqual((body["reasoning_effort"], body["max_tokens"]), ("none", 2048))
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(self.led.last("run.end")["cuts"], 0)

    def test_level_sets_the_runs_cumulative_cap(self):
        # 3 calls at "low" = 100 chars each: 60 + 60 + 60 chars pass under
        # 300, the fourth call's thinking takes the run over the level's total
        replies = [{"content": "no file yet", "reasoning": "r" * 60}] * 4 + [{"content": FILE_HELLO}]
        r = self.with_api(self.runner(replies), "reasoning_effort")
        r.budgets.think["levels"]["low"] = 100
        res = r.run(self.task, "local-a", self.env(calls=3), think="low")
        self.assertEqual(res.outcome, "fail:capability", res.detail)   # calls spent, never over the cap
        self.assertEqual(self.led.last("run.end")["reasoning_chars"], 180)


class Network(Base):
    """A VM that boots without an address is reaped and cloned once more."""

    def net_runner(self, ips):
        r = self.runner([{"content": FILE_HELLO}])
        r.budgets.watchdog["net_wait_seconds"] = 0.3
        r.net_ip = lambda vmid: ips(r)
        return r

    def test_silent_boot_is_retried_once(self):
        r = self.net_runner(lambda r: None if len(r.launched) == 1 else "10.0.0.7")
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertEqual([n for _, n in r.launched], ["dark-x0", "dark-x0", "dark-s0"])
        trig = [e["trigger"] for e in self.led.events("run.transition") if e["to"] == "executing"]
        self.assertIn("10.0.0.7", trig[0])

    def test_two_silent_boots_are_structural(self):
        r = self.net_runner(lambda r: None)
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "env"))
        self.assertIn("no address", res.detail)
        self.assertEqual(len(r.launched), 2)


class Tags(unittest.TestCase):
    def test_parse_tags(self):
        body = "AGENT-ALIVE\nDARK:{\"ev\": \"start\"}\nnot a tag\nDARK:{broken\nDARK:{\"ev\": \"beat\", \"at\": 1}\n"
        self.assertEqual([t["ev"] for t in R.parse_tags(body)], ["start", "beat"])


class ReviewMode(Base):
    """A session-arm run with a brief and files in, judged only by whether
    report.md landed. The VM is stood in for: launch()
    posts the AGENT-ALIVE/AGENT-DONE comments a real session.py would have
    posted, straight onto the issue review() opened."""

    def review_runner(self, done_fields):
        r = LocalRunner(self.cat, self.bud, self.host, self.led, self.gitea, None,
                        log=lambda *a: None, shift="s1")

        def launch(vmid, name, files, runcmd):
            task = json.loads(files["/opt/task.json"][0])
            full, issue = task["repo"], task["issue"]
            r.gitea.comment(full, issue, "AGENT-ALIVE\n" + spec.TAG_PREFIX +
                            json.dumps({"v": 2, "ev": "start", "model": task["llm_model"],
                                       "calls_max": task["max_calls"]}))
            r.gitea.comment(full, issue, "AGENT-DONE\n" + spec.TAG_PREFIX +
                            json.dumps({"v": 2, "ev": "done", "calls": 1, "tokens_in": 10,
                                       "tokens_out": 5, "reasoning_chars": 0, "seconds": 3, **done_fields}))
        r.launch = launch
        r.net_ip = lambda vmid: "10.0.0.1"
        return r

    def test_delivered_when_the_session_reports_ok(self):
        r = self.review_runner({"outcome": "ok", "records": "dark-records/s1/review-x-1",
                                "records_sha256": "deadbeef"})
        res = r.review("read this and report", {"a.md": "hi"}, "local-a", "review-x", shift="s1")
        self.assertEqual(res.outcome, "delivered", res.detail)
        self.assertEqual((res.records, res.records_sha256), ("dark-records/s1/review-x-1", "deadbeef"))
        self.assertIn("dark-records/s1", self.gitea_fake.repos)
        end = self.led.last("run.end")
        self.assertEqual((end["cls"], end["arm"], end["records"], end["records_sha256"]),
                         ("review", "review-x", "dark-records/s1/review-x-1", "deadbeef"))

    def test_a_push_failure_does_not_change_the_outcome(self):
        r = self.review_runner({"outcome": "ok", "records": "PUSH FAILED: no route to host"})
        res = r.review("read this", {}, "local-a", "review-x", shift="s1")
        self.assertEqual(res.outcome, "delivered")
        self.assertEqual(res.records, "PUSH FAILED: no route to host")
        self.assertIsNone(res.records_sha256)

    def test_a_missing_report_is_structural_not_capability(self):
        r = self.review_runner({"outcome": "fail", "kind": "no_report", "error": "report.md missing or empty"})
        res = r.review("read this", {}, "local-a", "review-x", shift="s1")
        self.assertEqual((res.outcome, res.fail_kind), ("fail:structural", "no_report"))
        self.assertIn("report.md missing", res.detail)


class KeepAwake(Base):
    """Every run and staging takes a lease that outlives its own envelope, so
    the VM host is not put to sleep under running work (dark/gate.py)."""

    def test_run_and_stage_take_a_lease_that_covers_them(self):
        r = self.runner([{"content": FILE_HELLO}])
        env = self.env()
        r.run(self.task, "local-a", env)
        self.assertEqual(len(r.holds), 1)
        self.assertGreaterEqual(r.holds[0], env.seconds + self.task.stage_timeout + gate.MARGIN_SECONDS)
        r.stage_only(self.task, "run/none", "cloud-x", "session-x")
        self.assertEqual(len(r.holds), 2)
        self.assertGreaterEqual(r.holds[1], self.task.stage_timeout + gate.MARGIN_SECONDS)

    def test_control_base_is_the_waking_provider_without_v1(self):
        base = gate.control_base(self.cat)
        waking = [p for p in self.cat.providers.values() if p.wake]
        if waking:
            self.assertTrue(waking[0].url.startswith(base))
            self.assertFalse(base.endswith("/v1"))
        else:
            self.assertIsNone(base)
        self.assertFalse(gate.hold("http://127.0.0.1:9", 10, timeout=1)[0])  # unreachable gate: (False, why)


if __name__ == "__main__":
    unittest.main()


class ClassLevel(Base):
    """A shift that sets no level runs each class at the level budgets.toml
    gives it; the run records that level, and the tier's own default is only
    the fallback when the class has none."""

    def test_class_level_reaches_the_run(self):
        import dataclasses
        r = self.runner([{"content": FILE_HELLO}])
        r.budgets.classes[self.task.cls] = dataclasses.replace(r.budgets.cls(self.task.cls), think="low")
        res = r.run(self.task, "local-a", self.env())
        self.assertEqual(res.outcome, "pass", res.detail)
        self.assertEqual(self.led.last("run.start")["envelope"]["think"], "low")
        self.assertEqual(self.led.last("run.end")["think"], "low")
        # (no level anywhere -> None on the record: test_no_think_field_by_default)


class ChainBase(SessionArm):
    """The run.end says what a chained step started from."""

    def test_base_reaches_the_record(self):
        r = self.runner([{"content": FILE_HELLO}])
        res = r.run(self.task, "local-a", self.env(), base="delivered")
        self.assertEqual(res.outcome, "pass", res.detail)
        end = self.led.last("run.end")
        self.assertEqual((end["base"], end["after"]), ("delivered", None))

    def test_no_base_by_default(self):
        r = self.runner([{"content": FILE_HELLO}])
        r.run(self.task, "local-a", self.env())
        self.assertIsNone(self.led.last("run.end")["base"])

    def test_stage_only_never_returns_without_its_record(self):
        # the LedgerError clause was on run() only
        from dark import ledger as L
        r = self.runner([])
        self.push_solution("run/session-9", {"hello.txt": "hello\n"})
        real = r.ledger.emit

        def emit(kind, **fields):
            if kind == "run.end":
                raise L.LedgerError("run.end: undeclared field(s) ['x']")
            return real(kind, **fields)
        r.ledger.emit = emit
        with self.assertRaises(L.LedgerError):
            r.stage_only(self.task, "run/session-9", "cloud-x", "session-x", base="delivered")

    def test_a_chained_stage_needs_a_base_and_records_the_chain(self):
        # a chained session-arm run must carry a base and a chain.base
        make_task(os.path.join(self.tmp, "bench"), "aaa", oracle={"hello.txt": "hello\n"})
        r = self.runner([], task_kw={"extra": 'after = "aaa"\n'})
        self.push_solution("run/session-8", {"hello.txt": "hello\n"})
        res = r.stage_only(self.task, "run/session-8", "cloud-x", "session-x")
        self.assertEqual(res.outcome, "refused")
        self.assertIn("--base", res.detail)
        self.assertEqual(self.led.events("chain.base"), [])
        res = r.stage_only(self.task, "run/session-8", "cloud-x", "session-x", base="delivered", after_branch="run/prev")
        self.assertEqual(res.outcome, "pass", res.detail)
        ev = self.led.events("chain.base")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["task"], ev[0]["after"], ev[0]["base"], ev[0]["branch"], ev[0]["repo"]),
                         ("hello", "aaa", "delivered", "run/prev", "dark/t-aaa"))
        end = self.led.last("run.end")
        self.assertEqual((end["after"], end["base"], end["repo"]), ("aaa", "delivered", "dark/t-hello"))

    def test_a_refused_run_end_is_never_swallowed(self):
        # a run.end the ledger refuses (an unregistered field) must not be
        # swallowed: run() would return "pass" with no record behind it
        from dark import ledger as L
        r = self.runner([{"content": FILE_HELLO}])
        real = r.ledger.emit

        def emit(kind, **fields):
            if kind == "run.end":
                raise L.LedgerError("run.end: undeclared field(s) ['x']")
            return real(kind, **fields)
        r.ledger.emit = emit
        with self.assertRaises(L.LedgerError):
            r.run(self.task, "local-a", self.env())
        self.assertEqual([n for _, n in r.launched], ["dark-x0", "dark-s0"])
