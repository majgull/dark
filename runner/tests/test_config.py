import os
import tempfile
import unittest

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
        self.assertIn("my/qwen-3.6-35b-nonthink", cat.models)
        self.assertEqual(cat.with_role("validator"), ["deepseek-v4-flash:cloud"])


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
