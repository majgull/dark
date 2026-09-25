import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from dark import __main__ as M
from dark import config, spec

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODELS = """
[defaults]
timeout = 100
max_tokens = 2048
[provider.gate]
url = "http://g/v1/"
catalog = "models"
wake = true
window = ""
[provider.cloud]
url = "http://c/v1"
catalog = "models"
window = "cloud"
[model."local-a"]
provider = "gate"
cost = "local"
speed = "fast"
watts = "low"
[model."local-b"]
provider = "gate"
cost = "local"
speed = "slow"
watts = "high"
timeout = 900
[model."cloud-x"]
provider = "cloud"
cost = "sub-window"
speed = "fast"
role = ["validator"]
rate_limit_signature = "429"
"""

BUDGETS = """
[window.cloud]
daily_calls = 10
daily_tokens = 1000
reserve_for_data_runs = 0.5
[watts]
daily_wh = 100
low = 100
mid = 200
high = 300
[class.additive]
hard = { runs = 2, calls = 6, seconds = 900, max_reasoning_chars = 20000 }
headroom = 0.3
admit_at = 0.7
provisional = ["local-a", "cloud-x", "local-b"]
[class.mechanical]
hard = { runs = 2, calls = 4, seconds = 300, max_reasoning_chars = 5000 }
headroom = 0.3
admit_at = 0.7
provisional = ["local-a"]
[class.repair]
hard = { runs = 2, calls = 8, seconds = 1200, max_reasoning_chars = 60000 }
headroom = 0.5
admit_at = 0.5
provisional = ["cloud-x", "local-b"]
[class.spec]
hard = { runs = 1, calls = 3, seconds = 600, max_reasoning_chars = 80000 }
admit_at = 0.7
provisional = ["cloud-x"]
[class.review]
hard = { runs = 1, calls = 4, seconds = 1800, max_reasoning_chars = 120000 }
admit_at = 0.7
provisional = ["cloud-x"]
[class.long]
think = "medium"
hard = { runs = 1, calls = 2000, seconds = 14400, max_reasoning_chars = 40000 }
admit_at = 0
provisional = ["cloud-x"]
[admission]
min_runs = 3
last_runs = 5
cost_order = ["local/low", "local/high", "sub-window/fast"]
[watchdog]
heartbeat_seconds = 10
silent_kill_seconds = 30
max_denials = 5
max_calls_low_variety = 5
poll_seconds = 1
[shift]
max_runs = 3
concurrency = 1
vm_template = 9001
vmid_base = 9500
"""


def write_conf(tmp, models=MODELS, budgets=BUDGETS):
    with open(os.path.join(tmp, "models.toml"), "w") as f:
        f.write(models)
    with open(os.path.join(tmp, "budgets.toml"), "w") as f:
        f.write(budgets)
    return tmp


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def load(self, models=MODELS, budgets=BUDGETS):
        return config.load(write_conf(self.tmp, models, budgets))


class LoadsFixture(Fixture):
    def test_fixture_loads(self):
        cat, bud = self.load()
        self.assertEqual(set(cat.models), {"local-a", "local-b", "cloud-x"})
        self.assertEqual(cat.providers["gate"].url, "http://g/v1")  # trailing slash stripped
        self.assertTrue(cat.providers["gate"].wake)
        self.assertEqual(cat.model("local-a").timeout, 100)   # default
        self.assertEqual(cat.model("local-b").timeout, 900)   # override
        self.assertEqual(cat.model("local-a").rank_key, "local/low")
        self.assertEqual(cat.model("cloud-x").rank_key, "sub-window/fast")
        self.assertTrue(cat.model("cloud-x").paid)
        self.assertFalse(cat.model("local-a").paid)
        self.assertEqual(cat.window_of("cloud-x"), "cloud")
        self.assertEqual(cat.window_of("local-a"), "")
        self.assertEqual(cat.with_role("validator"), ["cloud-x"])
        self.assertEqual(bud.cls("repair").hard.calls, 8)
        self.assertEqual(bud.cls("spec").headroom, 0.0)
        self.assertEqual(bud.windows["cloud"].reserve_for_data_runs, 0.5)
        self.assertEqual(bud.draw["high"], 300.0)
        self.assertEqual(bud.cost_order[0], "local/low")

    def test_think_levels_and_provider_api(self):
        _, bud = config.load(write_conf(self.tmp, MODELS.replace(
            "[provider.cloud]\n", '[provider.cloud]\nthink_api = "reasoning_effort"\n'), BUDGETS + "[think]\nlow = 100\n"))
        self.assertEqual(bud.think, {"levels": {"none": 0, "low": 100, "medium": 32000, "high": 64000}, "chars_per_token": 3})
        cat, _ = config.load(write_conf(self.tmp, MODELS.replace(
            "[provider.cloud]\n", '[provider.cloud]\nthink_api = "reasoning_effort"\nstream = true\n'), BUDGETS))
        self.assertEqual((cat.providers["cloud"].think_api, cat.providers["cloud"].stream), ("reasoning_effort", True))
        self.assertEqual((cat.providers["gate"].think_api, cat.providers["gate"].stream), ("none", True))  # stream = wake
        for bad in ("[think]\nnone = 5\n", "[think]\nlow = 50000\n", "[think]\nchars_per_token = 0\n"):
            with self.assertRaises(config.ConfigError):
                config.load(write_conf(self.tmp, MODELS, BUDGETS + bad))
        with self.assertRaises(config.ConfigError):
            config.load(write_conf(self.tmp, MODELS.replace("[provider.cloud]\n", '[provider.cloud]\nthink_api = "magic"\n'), BUDGETS))

    def test_thinking_fields(self):
        cat, _ = config.load(write_conf(self.tmp, MODELS.replace(
            '[model."local-a"]\n', '[model."local-a"]\nthinking_tokens = 4096\nthink = "low"\n'), BUDGETS))
        self.assertEqual((cat.model("local-a").thinking_tokens, cat.model("local-a").think), (4096, "low"))
        self.assertEqual((cat.model("local-b").thinking_tokens, cat.model("local-b").think), (0, None))
        cat, _ = config.load(write_conf(self.tmp, MODELS.replace(
            '[model."local-a"]\n', '[model."local-a"]\nthink_presets = { low = "local-a-low" }\n'), BUDGETS))
        self.assertEqual((cat.model("local-a").preset("low"), cat.model("local-a").preset("medium"),
                          cat.model("local-b").preset("low")), ("local-a-low", "local-a", "local-b"))
        for bad in ('thinking_tokens = -1\n', 'thinking_tokens = "x"\n', 'think = "maybe"\n',
                    'think_presets = { none = "x" }\n', 'think_presets = { low = 3 }\n', 'think_presets = "x"\n'):
            with self.assertRaises(config.ConfigError):
                config.load(write_conf(self.tmp, MODELS.replace('[model."local-a"]\n', '[model."local-a"]\n' + bad), BUDGETS))

    def test_unknown_model_is_one_line(self):
        cat, _ = self.load()
        with self.assertRaises(config.ConfigError):
            cat.model("nope")

    def test_shipped_config_loads(self):
        cat, bud = config.load(HERE)
        for cls in spec.CLASSES:
            self.assertIn(cls, bud.classes)
        self.assertIn("example-small", cat.models)
        self.assertEqual(cat.with_role("validator"), ["example-small"])


class Refusals(Fixture):
    def refuse(self, models=MODELS, budgets=BUDGETS, needle=""):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(models, budgets)
        self.assertIn(needle, str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))

    def test_class_without_hard_cap(self):
        self.refuse(budgets=BUDGETS.replace("[class.review]", "[class.reviewx]"), needle="review")

    def test_hard_cap_missing_key(self):
        self.refuse(budgets=BUDGETS.replace("seconds = 900, ", ""), needle="seconds")

    def test_local_without_watts(self):
        self.refuse(models=MODELS.replace('watts = "low"\n', ""), needle="watts")

    def test_sub_window_provider_without_window(self):
        self.refuse(models=MODELS.replace('window = "cloud"', 'window = ""'), needle="window")

    def test_window_named_but_not_declared(self):
        self.refuse(budgets=BUDGETS.replace("[window.cloud]", "[window.other]"), needle="cloud")

    def test_provisional_unknown_tier(self):
        self.refuse(budgets=BUDGETS.replace('"cloud-x", "local-b"]', '"cloud-x", "ghost"]'), needle="ghost")

    def test_cost_order_lacks_rank(self):
        self.refuse(budgets=BUDGETS.replace('"local/high", ', ""), needle="local/high")

    def test_bad_role(self):
        self.refuse(models=MODELS.replace('role = ["validator"]', 'role = ["boss"]'), needle="boss")

    def test_reserve_out_of_range(self):
        self.refuse(budgets=BUDGETS.replace("reserve_for_data_runs = 0.5", "reserve_for_data_runs = 1.0"),
                    needle="reserve")

    def test_silent_kill_must_exceed_heartbeat(self):
        self.refuse(budgets=BUDGETS.replace("silent_kill_seconds = 30", "silent_kill_seconds = 10"),
                    needle="silent_kill")

    def test_unknown_class_table(self):
        self.refuse(budgets=BUDGETS + '\n[class.magic]\nhard = { runs = 1, calls = 1, seconds = 1, max_reasoning_chars = 1 }\nadmit_at = 0.5\nprovisional = ["local-a"]\n',
                    needle="magic")

    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load(os.path.join(self.tmp, "nowhere"))

    def test_bad_toml(self):
        self.refuse(models="[model\n", needle="TOML")


if __name__ == "__main__":
    unittest.main()


class ClassThink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_class_level_parsed_and_validated(self):
        cat, bud = config.load(write_conf(self.tmp, MODELS, BUDGETS.replace(
            "[class.additive]\n", '[class.additive]\nthink = "low"\n')))
        self.assertEqual((bud.cls("additive").think, bud.cls("mechanical").think), ("low", None))
        with self.assertRaises(config.ConfigError):
            config.load(write_conf(self.tmp, MODELS, BUDGETS.replace(
                "[class.additive]\n", '[class.additive]\nthink = "lots"\n')))


class LxcHostKeys(unittest.TestCase):
    """The lxc backend's three host keys: defaults, the file, the environment
    (which wins), and the example lines in the checked-in host.toml."""

    def load(self, body, environ=None):
        p = os.path.join(tempfile.mkdtemp(), "host.toml")
        with open(p, "w") as f:
            f.write(body)
        return config.load_host(p, environ={} if environ is None else environ)

    def test_backend_name(self):
        self.assertIn("lxc", config.BACKENDS)
        self.assertEqual(self.load('[host]\nbackend = "lxc"\n').backend, "lxc")

    def test_defaults(self):
        h = config.Host()
        self.assertEqual((h.sandbox_container, h.sandbox_snapshot, h.sandbox_bridge), ("", "", "vmbr0"))

    def test_file_values(self):
        h = self.load('[host]\nbackend = "lxc"\nsandbox_container = 200\n'
                      'sandbox_snapshot = "base"\nsandbox_bridge = "vmbr9"\n')
        self.assertEqual((h.sandbox_container, h.sandbox_snapshot, h.sandbox_bridge), ("200", "base", "vmbr9"))

    def test_environment_overrides(self):
        env = {"DARK_SANDBOX_CONTAINER": "201", "DARK_SANDBOX_SNAPSHOT": "pre-change",
               "DARK_SANDBOX_BRIDGE": "vmbr7"}
        self.assertEqual({k: config.HOST_ENV[k] for k in ("sandbox_container", "sandbox_snapshot",
                                                          "sandbox_bridge")},
                         {"sandbox_container": "DARK_SANDBOX_CONTAINER",
                          "sandbox_snapshot": "DARK_SANDBOX_SNAPSHOT",
                          "sandbox_bridge": "DARK_SANDBOX_BRIDGE"})
        h = self.load('[host]\nsandbox_container = "200"\nsandbox_snapshot = "base"\n', environ=env)
        self.assertEqual((h.sandbox_container, h.sandbox_snapshot, h.sandbox_bridge),
                         ("201", "pre-change", "vmbr7"))

    def test_checked_in_host_toml_has_example_lines(self):
        with open(os.path.join(HERE, "host.toml")) as f:
            text = f.read()
        for key, env in (("sandbox_container", "DARK_SANDBOX_CONTAINER"),
                         ("sandbox_snapshot", "DARK_SANDBOX_SNAPSHOT"),
                         ("sandbox_bridge", "DARK_SANDBOX_BRIDGE")):
            with self.subTest(key=key):
                line = [ln for ln in text.splitlines() if ln.startswith(key + " ")]
                self.assertEqual(len(line), 1)
                self.assertIn(f"# {env}", line[0])
        config.load_host(os.path.join(HERE, "host.toml"), environ={})


class ConfDirDefault(unittest.TestCase):
    """--conf, else $DARK_CONF, else beside the package (the runner's checked-in
    toml files). DARK_CONF lets a deployment keep its real endpoints outside the
    checkout; the flag still wins when both are set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = write_conf(self.tmp)
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write("[host]\n")

    def run_cli(self, argv, dark_conf):
        env = {} if dark_conf is None else {"DARK_CONF": dark_conf}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env):
            os.environ.pop("DARK_CONF", None)
            if dark_conf is not None:
                os.environ["DARK_CONF"] = dark_conf
            with redirect_stdout(buf):
                rc = M.main(argv)
        return rc, buf.getvalue()

    def test_dark_conf_is_read_when_the_flag_is_absent(self):
        rc, out = self.run_cli(["check-config"], dark_conf=self.conf)
        self.assertEqual(rc, 0)
        self.assertIn("config OK", out)

    def test_without_dark_conf_the_directory_beside_the_package_is_used(self):
        rc, out = self.run_cli(["check-config"], dark_conf=None)
        self.assertEqual(rc, 0)
        self.assertIn("config OK", out)

    def test_a_missing_dark_conf_dir_is_the_one_loaded(self):
        # the package's own directory is a valid config, so a refusal here can
        # only mean DARK_CONF, not the default, was used
        missing = os.path.join(self.tmp, "nowhere")
        rc, out = self.run_cli(["check-config"], dark_conf=missing)
        self.assertEqual(rc, 2)
        self.assertIn(missing, out)

    def test_the_flag_beats_dark_conf(self):
        missing = os.path.join(self.tmp, "nowhere")
        rc, out = self.run_cli(["--conf", self.conf, "check-config"], dark_conf=missing)
        self.assertEqual(rc, 0)
        self.assertIn("config OK", out)
