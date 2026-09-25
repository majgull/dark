"""python3 -m dark mcp: the Model Context Protocol server over stdio
(dark/mcp.py). Every test starts the server as a real subprocess and talks to
it over pipes. Most run it with a fake `dark` module: a package named dark, in a
directory under /tmp/fx put first on the children's PYTHONPATH, so the
`python3 -m dark <verb>` a tool call starts is the fake, which records its argv
and stdin and prints what the test tells it to. The server itself is started by
a one-line `python3 -c` that puts runner/ first on its own sys.path, so the fake
never shadows the server. `Pipes` starts the real `python3 -m dark mcp` and
lets it run the real CLI, which refuses an empty DARK_CONF."""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest

FX = "/tmp/fx"
RUNNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(RUNNER)

TOOL_NAMES = ["dark_preflight", "dark_run", "dark_user", "dark_long", "dark_review", "dark_ledger_tail"]
REQUIRED = {"dark_preflight": [], "dark_run": ["task", "tier"], "dark_user": ["task", "tier"],
            "dark_long": ["task", "tier"], "dark_review": ["task", "tier"], "dark_ledger_tail": []}

FAKE_MAIN = '''import json, os, sys, time
stdin = sys.stdin.read()
rec = {"argv": sys.argv[1:], "stdin": stdin}
for a in sys.argv[1:]:
    if a.startswith("--review-branches="):
        with open(a.split("=", 1)[1]) as f:
            rec["review_branches"] = f.read()
with open(os.environ["FAKE_DARK_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\\n")
sys.stdout.write(os.environ.get("FAKE_DARK_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_DARK_ERR", ""))
sys.stdout.flush()
if os.environ.get("FAKE_DARK_SLEEP"):
    time.sleep(float(os.environ["FAKE_DARK_SLEEP"]))
sys.exit(int(os.environ.get("FAKE_DARK_EXIT", "0")))
'''

BOOT = f"import sys; sys.path.insert(0, {RUNNER!r}); from dark.mcp import serve; sys.exit(serve(sys.stdin, sys.stdout))"


def pyproject_version():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


class Session:
    """One server process and its pipes; a watchdog kills a server that hangs."""

    def __init__(self, argv, env, cwd):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", env=env, cwd=cwd)
        self.watchdog = threading.Timer(60, self.proc.kill)
        self.watchdog.start()
        self.lines = []  # every raw line the server wrote to stdout
        self.closed = None

    def send(self, message):
        self.proc.stdin.write((message if isinstance(message, str) else json.dumps(message)) + "\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        self.lines.append(line)
        return json.loads(line)

    def request(self, id_, method, params=None):
        msg = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.recv()

    def tool(self, name, arguments=None, id_=100):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request(id_, "tools/call", params)

    def close(self):
        """Close stdin; (exit code, what stdout still held, stderr). Safe to call twice."""
        if self.closed is None:
            self.proc.stdin.close()
            code = self.proc.wait()
            self.watchdog.cancel()
            self.closed = (code, self.proc.stdout.read(), self.proc.stderr.read())
            self.proc.stdout.close()
            self.proc.stderr.close()
        return self.closed


class ServerCase(unittest.TestCase):
    """A directory under /tmp/fx holding the fake dark package and its call log."""

    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="mcp-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, True)
        pkg = os.path.join(self.dir, "fake", "dark")
        os.makedirs(pkg)
        open(os.path.join(pkg, "__init__.py"), "w").close()
        with open(os.path.join(pkg, "__main__.py"), "w") as f:
            f.write(FAKE_MAIN)
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)
        self.log = os.path.join(self.dir, "calls.jsonl")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DARK_")}
        self.env.update(PYTHONPATH=os.path.join(self.dir, "fake"), FAKE_DARK_LOG=self.log)

    def start(self, **env):
        s = Session([sys.executable, "-c", BOOT], {**self.env, **env}, self.work)
        self.addCleanup(s.close)
        return s

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]


class Framing(ServerCase):
    def test_initialize(self):
        s = self.start()
        r = s.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(r, {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "dark", "version": pyproject_version()}}})

    def test_initialized_notification_gets_no_reply(self):
        s = self.start()
        s.request(1, "initialize", {})
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        s.send({"jsonrpc": "2.0", "method": "notifications/anything_else"})
        self.assertEqual(s.request(2, "tools/list")["id"], 2)  # the next line out answers id 2, so nothing came between

    def test_unknown_method_is_32601(self):
        r = self.start().request("abc", "resources/list")
        self.assertEqual((r["id"], r["error"]["code"]), ("abc", -32601))

    def test_malformed_line_is_32700_and_the_loop_continues(self):
        s = self.start()
        s.send("{not json")
        r = s.recv()
        self.assertEqual((r["id"], r["error"]["code"]), (None, -32700))
        self.assertEqual(s.request(2, "ping")["result"], {})

    def test_not_one_request_is_32600(self):
        s = self.start()
        for line in ["[]", "42", '{"jsonrpc":"2.0","method":7}', '{"id":1,"method":"ping"}',
                     '{"jsonrpc":"2.0","id":true,"method":"ping"}', '{"jsonrpc":"2.0","id":null,"method":"ping"}']:
            with self.subTest(line=line):
                s.send(line)
                self.assertEqual(s.recv()["error"]["code"], -32600)
        self.assertEqual(s.request(2, "ping")["id"], 2)

    def test_blank_lines_are_skipped(self):
        s = self.start()
        s.send("")
        s.send("   ")
        self.assertEqual(s.request(1, "ping")["id"], 1)

    def test_exit_0_when_stdin_closes_and_stdout_held_nothing_else(self):
        s = self.start(FAKE_DARK_OUT="line one\nline two\n", FAKE_DARK_ERR="chatter\n")
        s.request(1, "initialize", {})
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        s.send("garbage")
        s.recv()
        s.tool("dark_preflight")
        code, rest, err = s.close()
        self.assertEqual(code, 0)
        self.assertEqual(rest, "")
        for line in s.lines:  # one JSON-RPC message per line, nothing else
            self.assertTrue(line.endswith("\n") and "\n" not in line[:-1])
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        self.assertIn("dark_preflight", err)  # the log went to stderr


class Pipes(unittest.TestCase):
    """The real `python3 -m dark mcp`, and the real CLI behind its tool calls."""

    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="mcp-pipes-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DARK_")}
        self.env.update(PYTHONPATH=RUNNER, DARK_CONF=self.dir)

    def test_a_whole_session(self):
        s = Session([sys.executable, "-m", "dark", "mcp"], self.env, self.dir)
        self.addCleanup(s.close)
        r = s.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(r["result"]["serverInfo"], {"name": "dark", "version": pyproject_version()})
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = s.request(2, "tools/list")
        self.assertEqual([t["name"] for t in r["result"]["tools"]], TOOL_NAMES)
        r = s.tool("dark_preflight", id_=3)  # the real CLI: an empty DARK_CONF is refused with exit code 2
        self.assertEqual(r["id"], 3)
        self.assertTrue(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("config:", text)
        self.assertTrue(text.endswith("exit code 2"), text)
        code, rest, _ = s.close()
        self.assertEqual((code, rest), (0, ""))
        self.assertEqual([json.loads(line)["id"] for line in s.lines], [1, 2, 3])

    def test_the_subcommand_is_listed(self):
        out = subprocess.run([sys.executable, "-m", "dark", "--help"], capture_output=True, text=True,
                             env=self.env, cwd=self.dir)
        self.assertIn("mcp", out.stdout)


class ToolList(ServerCase):
    def test_six_tools_with_schemas(self):
        tools = self.start().request(1, "tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], TOOL_NAMES)
        for t in tools:
            with self.subTest(tool=t["name"]):
                self.assertTrue(t["description"])
                schema = t["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertEqual(schema["required"], REQUIRED[t["name"]])
                self.assertIs(schema["additionalProperties"], False)
                self.assertTrue(set(schema["required"]) <= set(schema["properties"]))
                for prop in schema["properties"].values():
                    self.assertIn(prop["type"], ("string", "integer"))
                if t["name"] != "dark_ledger_tail":
                    self.assertIn("timeout_seconds", schema["properties"])

    def test_the_optional_arguments(self):
        props = {t["name"]: set(t["inputSchema"]["properties"])
                 for t in self.start().request(1, "tools/list")["result"]["tools"]}
        self.assertEqual(props["dark_preflight"], {"timeout_seconds"})
        self.assertEqual(props["dark_run"], {"task", "tier", "arm", "slot", "timeout_seconds"})
        self.assertEqual(props["dark_user"], {"task", "tier", "timeout_seconds"})
        self.assertEqual(props["dark_long"], {"task", "tier", "judge_tier", "timeout_seconds"})
        self.assertEqual(props["dark_review"], {"task", "tier", "review_branches", "timeout_seconds"})
        self.assertEqual(props["dark_ledger_tail"], {"n"})

    def test_ledger_tail_defaults_to_ten_rows(self):
        tools = {t["name"]: t for t in self.start().request(1, "tools/list")["result"]["tools"]}
        self.assertEqual(tools["dark_ledger_tail"]["inputSchema"]["properties"]["n"]["default"], 10)


class Validation(ServerCase):
    def test_bad_arguments_are_32602_and_start_nothing(self):
        s = self.start()
        cases = [
            ("dark_run", {"task": "/t"}, "missing required argument: tier"),
            ("dark_run", {"tier": "m"}, "missing required argument: task"),
            ("dark_run", {"task": "/t", "tier": "m", "colour": "red"}, "unknown argument: colour"),
            ("dark_preflight", {"no_vm": True}, "unknown argument: no_vm"),
            ("dark_run", {"task": "/t", "tier": 3}, "tier must be a non-empty string"),
            ("dark_run", {"task": "/t", "tier": ""}, "tier must be a non-empty string"),
            ("dark_run", {"task": "/t", "tier": "m", "slot": "1"}, "slot must be an integer"),
            ("dark_run", {"task": "/t", "tier": "m", "slot": True}, "slot must be an integer"),
            ("dark_run", {"task": "/t", "tier": "m", "slot": -1}, "slot must be from 0"),
            ("dark_user", {"task": "/t", "tier": "m", "timeout_seconds": 0}, "timeout_seconds must be from 1"),
            ("dark_user", {"task": "/t", "tier": "m", "timeout_seconds": 1.5}, "timeout_seconds must be an integer"),
            ("dark_review", {"task": "/b", "tier": "m", "review_branches": "not json"}, "must be JSON text holding a list"),
            ("dark_review", {"task": "/b", "tier": "m", "review_branches": "{}"}, "must be JSON text holding a list"),
            ("dark_ledger_tail", {"n": 0}, "n must be from 1 to 200"),
            ("dark_ledger_tail", {"n": 201}, "n must be from 1 to 200"),
            ("dark_ledger_tail", {"n": "3"}, "n must be an integer"),
            ("dark_ledger_tail", [], "arguments must be an object"),
            ("dark_nope", {}, "Unknown tool: dark_nope"),
        ]
        for name, arguments, message in cases:
            with self.subTest(name=name, arguments=arguments):
                r = s.tool(name, arguments)
                self.assertEqual(r["error"]["code"], -32602)
                self.assertIn(message, r["error"]["message"])
        r = s.request(7, "tools/call", {"arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        r = s.request(8, "tools/call", "nope")
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(self.calls(), [])

    def test_arguments_may_be_left_out_when_none_is_required(self):
        s = self.start(FAKE_DARK_OUT="ok\n")
        self.assertEqual(s.tool("dark_preflight")["result"]["content"][0]["text"], "ok\n")
        self.assertEqual(s.request(2, "tools/call", {"name": "dark_preflight", "arguments": None})["result"]["isError"],
                         False)


class ToolCalls(ServerCase):
    def call(self, name, arguments, **env):
        r = self.start(**env).tool(name, arguments)
        self.assertNotIn("error", r)
        return r["result"], self.calls()[-1]

    def test_preflight_returns_the_commands_stdout_and_starts_it_with_an_empty_stdin(self):
        out = "check one ok\ncheck two ok\npreflight OK\n"
        result, call = self.call("dark_preflight", {}, FAKE_DARK_OUT=out)
        self.assertEqual(result, {"content": [{"type": "text", "text": out}], "isError": False})
        self.assertEqual(call, {"argv": ["preflight"], "stdin": ""})

    def test_run_is_one_shift_that_never_pushes(self):
        result, call = self.call("dark_run", {"task": "/w/t1", "tier": "mid"}, FAKE_DARK_OUT="digest\n")
        self.assertEqual(call["argv"], ["shift", "--no-push", "--task-dir=/w/t1", "--tier=mid"])
        self.assertEqual(result["content"][0]["text"], "digest\n")
        _, call = self.call("dark_run", {"task": "/w/t1", "tier": "mid", "arm": "a1", "slot": 2})
        self.assertEqual(call["argv"], ["shift", "--no-push", "--task-dir=/w/t1", "--tier=mid", "--arm=a1", "--slot=2"])
        # The tool table has no `think`, so the server refuses it before starting anything.
        r = self.start().tool("dark_run", {"task": "/w/t1", "tier": "mid", "think": True})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("unknown argument: think", r["error"]["message"])

    def test_user(self):
        _, call = self.call("dark_user", {"task": "/w/u", "tier": "mid"})
        self.assertEqual(call["argv"], ["user", "--task=/w/u", "--tier=mid"])

    def test_long_with_and_without_a_judge_tier(self):
        _, call = self.call("dark_long", {"task": "/w/l", "tier": "mid"})
        self.assertEqual(call["argv"], ["long", "--task=/w/l", "--tier=mid"])
        _, call = self.call("dark_long", {"task": "/w/l", "tier": "mid", "judge_tier": "big"})
        self.assertEqual(call["argv"], ["long", "--task=/w/l", "--tier=mid", "--judge-tier=big"])

    def test_review_takes_the_task_as_its_brief(self):
        _, call = self.call("dark_review", {"task": "/w/b.md", "tier": "mid"})
        self.assertEqual(call["argv"], ["review", "--arm=review", "--brief=/w/b.md", "--tier=mid"])

    def test_review_branches_reach_the_command_as_a_file_that_is_removed_afterwards(self):
        branches = json.dumps([{"name": "api", "url": "http://git.example/o/api.git", "branch": "run/1"}])
        _, call = self.call("dark_review", {"task": "/w/b.md", "tier": "mid", "review_branches": branches})
        self.assertEqual(call["argv"][:4], ["review", "--arm=review", "--brief=/w/b.md", "--tier=mid"])
        option, path = call["argv"][4].split("=", 1)
        self.assertEqual(option, "--review-branches")
        self.assertEqual(call["review_branches"], branches)
        self.assertFalse(os.path.exists(path))

    def test_timeout_seconds_is_the_servers_and_never_reaches_the_command(self):
        _, call = self.call("dark_user", {"task": "/w/u", "tier": "mid", "timeout_seconds": 60})
        self.assertEqual(call["argv"], ["user", "--task=/w/u", "--tier=mid"])

    def ledger_conf(self):
        """A DARK_CONF whose ledger holds five rows of two kinds, written under /tmp/fx."""
        conf = os.path.join(self.dir, "conf")
        state = os.path.join(self.dir, "state")
        os.makedirs(conf)
        os.makedirs(state)
        with open(os.path.join(conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{state}"\n')
        kinds = ["run.start", "run.end", "run.start", "run.end", "run.start"]
        with open(os.path.join(state, "ledger.jsonl"), "w") as f:
            for n, kind in enumerate(kinds, 1):
                f.write(json.dumps({"kind": kind, "n": n}) + "\n")
        return conf

    def test_ledger_tail_is_answered_by_the_server_and_takes_no_kind(self):
        # The server answers this tool itself, so its schema has no `kind`: a kind
        # filter is refused as an unknown argument, and neither call starts a command.
        s = self.start(DARK_CONF=self.ledger_conf())
        result = s.tool("dark_ledger_tail", {})["result"]
        rows = [json.loads(line) for line in result["content"][0]["text"].splitlines()]
        self.assertEqual([(r["kind"], r["n"]) for r in rows],
                         [("run.start", 1), ("run.end", 2), ("run.start", 3), ("run.end", 4), ("run.start", 5)])
        self.assertFalse(result["isError"])
        r = s.tool("dark_ledger_tail", {"kind": "run.end"})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("unknown argument: kind", r["error"]["message"])
        self.assertEqual(self.calls(), [])


class Ledger(ServerCase):
    def conf(self, rows):
        conf = os.path.join(self.dir, "conf")
        state = os.path.join(self.dir, "state")
        os.makedirs(conf)
        os.makedirs(state)
        with open(os.path.join(conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{state}"\n')
        if rows is not None:
            with open(os.path.join(state, "ledger.jsonl"), "w") as f:
                f.write("".join(json.dumps({"kind": "run.end", "n": i}) + "\n" for i in range(rows)))
        return conf

    def tail(self, conf, arguments):
        return self.start(DARK_CONF=conf).tool("dark_ledger_tail", arguments)["result"]

    def test_last_ten_rows_by_default_oldest_first(self):
        result = self.tail(self.conf(12), {})
        rows = [json.loads(line) for line in result["content"][0]["text"].splitlines()]
        self.assertEqual([r["n"] for r in rows], list(range(2, 12)))
        self.assertFalse(result["isError"])
        self.assertEqual(self.calls(), [])  # no command was started

    def test_n_rows(self):
        rows = self.tail(self.conf(12), {"n": 3})["content"][0]["text"].splitlines()
        self.assertEqual([json.loads(r)["n"] for r in rows], [9, 10, 11])

    def test_fewer_rows_than_asked(self):
        rows = self.tail(self.conf(2), {"n": 50})["content"][0]["text"].splitlines()
        self.assertEqual(len(rows), 2)

    def test_no_ledger_yet_is_an_empty_success(self):
        result = self.tail(self.conf(None), {})
        self.assertEqual(result, {"content": [{"type": "text", "text": ""}], "isError": False})

    def test_no_host_toml_is_an_error_that_names_the_file(self):
        result = self.tail(os.path.join(self.dir, "missing"), {})
        self.assertTrue(result["isError"])
        self.assertIn("host.toml", result["content"][0]["text"])


class Failures(ServerCase):
    def test_exit_code_3_is_an_error_with_stdout_stderr_and_the_code(self):
        r = self.start(FAKE_DARK_OUT="partial\n", FAKE_DARK_ERR="boom\n", FAKE_DARK_EXIT="3").tool("dark_preflight", {})
        result = r["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["text"], "partial\nboom\nexit code 3")

    def test_success_stderr_goes_to_the_servers_stderr_not_the_reply(self):
        s = self.start(FAKE_DARK_OUT="fine\n", FAKE_DARK_ERR="a warning\n")
        result = s.tool("dark_preflight", {})["result"]
        self.assertEqual((result["content"][0]["text"], result["isError"]), ("fine\n", False))
        self.assertIn("a warning", s.close()[2])

    def test_a_value_that_starts_with_a_dash_stays_one_argument(self):
        self.start().tool("dark_user", {"task": "-t", "tier": "-x"})
        self.assertEqual(self.calls()[-1]["argv"], ["user", "--task=-t", "--tier=-x"])

    def test_a_command_past_its_timeout_is_killed_and_reported(self):
        s = self.start(FAKE_DARK_OUT="started\n", FAKE_DARK_SLEEP="60")
        result = s.tool("dark_preflight", {"timeout_seconds": 1})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("started", result["content"][0]["text"])
        self.assertIn("timed out after 1 seconds", result["content"][0]["text"])
        self.assertEqual(s.request(2, "ping")["id"], 2)  # the server is still answering


class Imports(unittest.TestCase):
    def test_only_the_standard_library_and_the_dark_package(self):
        with open(os.path.join(RUNNER, "dark", "mcp.py")) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [] if node.level else [node.module]  # a relative import is the dark package
            else:
                continue
            for mod in mods:
                self.assertIn(mod.split(".")[0], sys.stdlib_module_names, mod)

    def test_the_dependency_list_stays_empty(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            self.assertEqual(tomllib.load(f)["project"]["dependencies"], [])


if __name__ == "__main__":
    unittest.main()
