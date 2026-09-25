"""dark/mcp.py: dark as a Model Context Protocol server over stdio.

    python3 -m dark mcp

The server reads one JSON-RPC 2.0 message per line on stdin and writes one
per line on stdout, and nothing else goes to stdout; its log goes to stderr.
Five tools run the matching `python3 -m dark <verb>` as a subprocess (the
same interpreter, the same environment, an empty stdin) and return what it
printed; the sixth, dark_ledger_tail, reads the ledger itself, because the
CLI has no command that prints ledger rows. The server adds no behaviour to
the factory: authentication and access control are those of whatever carries
the stdio. Only the standard library and the dark package are imported.
"""

import collections
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEFAULT_TIMEOUT = 1800     # seconds a tool call may run before its command is killed
TERM_GRACE = 10            # seconds between SIGTERM and SIGKILL on a timeout
LEDGER_DEFAULT_ROWS = 10
LEDGER_MAX_ROWS = 200
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Arg:
    name: str
    type: str                # a JSON Schema type: "string" or "integer"
    doc: str
    required: bool = False
    option: str = ""         # the CLI option this becomes; "" = the server's own
    minimum: int | None = None
    maximum: int | None = None
    default: int | None = None
    file: bool = False       # a JSON text the CLI reads from a file: the server writes a temporary one


@dataclass(frozen=True)
class Tool:
    name: str
    verb: str | None         # the dark subcommand it runs; None = answered by the server itself
    fixed: tuple             # options passed on every call
    description: str
    args: tuple

    def schema(self):
        props = {}
        for a in self.args:
            p = {"type": a.type, "description": a.doc}
            if a.type == "string":
                p["minLength"] = 1
            for key, val in (("minimum", a.minimum), ("maximum", a.maximum), ("default", a.default)):
                if val is not None:
                    p[key] = val
            props[a.name] = p
        return {"type": "object", "properties": props,
                "required": [a.name for a in self.args if a.required], "additionalProperties": False}

    def listing(self):
        return {"name": self.name, "description": self.description, "inputSchema": self.schema()}


_TIMEOUT = Arg("timeout_seconds", "integer",
               f"Kill the command after this many seconds and report a timeout (default {DEFAULT_TIMEOUT}).",
               minimum=1, maximum=86400)
_TIER = "The model entry in models.toml to use."

TOOLS = (
    Tool("dark_preflight", "preflight", (),
         "Check that the factory is ready to run: one line per check, then `preflight OK` or the refusal line. "
         "Exit code 2 means not ready.",
         (_TIMEOUT,)),
    Tool("dark_run", "shift", ("--no-push",),
         "Run one code task through preflight, the model, and staging against hidden acceptance tests, then "
         "print the shift digest. The digest is committed locally and never pushed.",
         (Arg("task", "string", "Path of one task directory on the machine where the server runs.", True, "--task-dir"),
          Arg("tier", "string", _TIER, True, "--tier"),
          Arg("arm", "string", "Arm name for the run (default factory).", option="--arm"),
          Arg("slot", "integer", "Sandbox slot to use (default 0).", option="--slot", minimum=0),
          _TIMEOUT)),
    Tool("dark_user", "user", (),
         "Run one user-arm task: a browser sandbox checks a URL step by step. Prints one JSON line: run, outcome, "
         "fail_kind, detail, issue, records, steps_ok, steps_total.",
         (Arg("task", "string", "Path of the user task's task.toml or its directory.", True, "--task"),
          Arg("tier", "string", _TIER, True, "--tier"),
          _TIMEOUT)),
    Tool("dark_long", "long", (),
         "Run one long-arm task over several repositories, then judge the pushed branches with a reviewer "
         "session. Prints `long: <outcome>` and `judge: <outcome>`.",
         (Arg("task", "string", "Path of the long task's task.toml or its directory.", True, "--task"),
          Arg("tier", "string", _TIER, True, "--tier"),
          Arg("judge_tier", "string", "The model entry the judging reviewer uses (default: tier).",
              option="--judge-tier"),
          _TIMEOUT)),
    Tool("dark_review", "review", ("--arm=review",),
         "Run one review session: the task file is its instructions and report.md is its output. Prints one JSON "
         "line: run, outcome, fail_kind, detail, issue, records.",
         (Arg("task", "string", "Path of a Markdown file holding the review's instructions.", True, "--brief"),
          Arg("tier", "string", _TIER, True, "--tier"),
          Arg("review_branches", "string",
              "JSON text of a list of {\"name\", \"url\", \"branch\"} objects, one repository each, cloned "
              "read-only into the review sandbox.", option="--review-branches", file=True),
          _TIMEOUT)),
    Tool("dark_ledger_tail", None, (),
         "Print the last rows of the ledger, oldest first, one JSON object per line.",
         (Arg("n", "integer", f"How many rows (default {LEDGER_DEFAULT_ROWS}).", minimum=1, maximum=LEDGER_MAX_ROWS,
              default=LEDGER_DEFAULT_ROWS),)),
)
BY_NAME = {t.name: t for t in TOOLS}


def log(text):
    print(f"dark mcp: {text}", file=sys.stderr, flush=True)


def version():
    """dark's version: from pyproject.toml beside a checkout, else the installed package."""
    try:
        with open(os.path.join(os.path.dirname(HERE), "pyproject.toml"), "rb") as f:
            meta = tomllib.load(f).get("project", {})
        if meta.get("name") == "dark" and isinstance(meta.get("version"), str):
            return meta["version"]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        return importlib.metadata.version("dark")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def validate(tool, arguments):
    """None when the arguments fit the tool's schema, else one message."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    known = {a.name: a for a in tool.args}
    for key in arguments:
        if key not in known:
            return f"unknown argument: {key}"
    for a in tool.args:
        if a.name not in arguments:
            if a.required:
                return f"missing required argument: {a.name}"
            continue
        v = arguments[a.name]
        if a.type == "string":
            if not isinstance(v, str) or not v:
                return f"{a.name} must be a non-empty string"
            if a.file:
                try:
                    ok = isinstance(json.loads(v), list)
                except (ValueError, RecursionError):
                    ok = False
                if not ok:
                    return f"{a.name} must be JSON text holding a list"
        else:
            if not isinstance(v, int) or isinstance(v, bool):
                return f"{a.name} must be an integer"
            if a.minimum is not None and v < a.minimum or a.maximum is not None and v > a.maximum:
                return f"{a.name} must be from {a.minimum} to {a.maximum}"
    return None


def build_argv(tool, arguments):
    """The command a tool runs: --option=value items, so a value that starts with a dash is never read as an option."""
    argv = [sys.executable, "-m", "dark", tool.verb, *tool.fixed]
    for a in tool.args:
        if a.option and a.name in arguments:
            argv.append(f"{a.option}={arguments[a.name]}")
    return argv


def run_command(argv, timeout):
    """(exit code, stdout, stderr); the exit code is None when the timeout killed the command's process group."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
    except ProcessLookupError:
        out, err = proc.communicate()
    return None, out, err


def _result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _join(*parts):
    return "\n".join(p.rstrip("\n") for p in parts if p)


def call_command(tool, arguments):
    args = dict(arguments)
    timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT)
    temps = []
    try:
        for a in tool.args:
            if a.file and a.name in args:
                fd, path = tempfile.mkstemp(prefix="dark-mcp-", suffix=".json")
                temps.append(path)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(args[a.name])
                args[a.name] = path
        code, out, err = run_command(build_argv(tool, args), timeout)
    except OSError as e:
        return _result(f"{tool.name}: could not start the dark command: {e}", True)
    finally:
        for path in temps:
            try:
                os.unlink(path)
            except OSError:
                pass
    if code == 0:
        if err:
            log(f"{tool.name}: stderr: {err.rstrip()}")
        return _result(out, False)
    if code is None:
        return _result(_join(out, err, f"timed out after {timeout} seconds; the command was killed"), True)
    return _result(_join(out, err, f"exit code {code}"), True)


def tail_ledger(arguments):
    """The last n rows of the ledger the CLI writes: host.ledger_path from host.toml in $DARK_CONF."""
    from . import config
    n = arguments.get("n", LEDGER_DEFAULT_ROWS)
    try:
        host = config.load_host(os.path.join(os.environ.get("DARK_CONF", HERE), "host.toml"))
    except config.ConfigError as e:
        return _result(f"config: {e}", True)
    try:
        with open(host.ledger_path, encoding="utf-8", errors="replace") as f:
            rows = collections.deque((line for line in f if line.strip()), maxlen=n)
    except FileNotFoundError:
        rows = ()
    except OSError as e:
        return _result(f"ledger: {host.ledger_path}: {e.strerror}", True)
    return _result("".join(row if row.endswith("\n") else row + "\n" for row in rows), False)


def _valid_id(v):
    """A request id is a string or an integer, never null or a boolean."""
    return isinstance(v, (str, int)) and not isinstance(v, bool)


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tools_call(id_, params):
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _error(id_, INVALID_PARAMS, "tools/call needs params.name")
    tool = BY_NAME.get(params["name"])
    if tool is None:
        return _error(id_, INVALID_PARAMS, f"Unknown tool: {params['name']}")
    arguments = params.get("arguments")
    arguments = {} if arguments is None else arguments
    why = validate(tool, arguments)
    if why:
        return _error(id_, INVALID_PARAMS, why)
    log(f"tools/call {tool.name}")
    result = tail_ledger(arguments) if tool.verb is None else call_command(tool, arguments)
    return _ok(id_, result)


def handle(message):
    """The response to one parsed message, or None when it needs none (a notification)."""
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str)
            or "id" in message and not _valid_id(message["id"])):
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "id" not in message:
        return None  # notifications/initialized, and any other notification, get no reply
    id_, method, params = message["id"], message["method"], message.get("params")
    if method == "initialize":
        return _ok(id_, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                         "serverInfo": {"name": "dark", "version": version()}})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": [t.listing() for t in TOOLS]})
    if method == "tools/call":
        return _tools_call(id_, params)
    return _error(id_, METHOD_NOT_FOUND, f"Method not found: {method}")


def serve(stdin, stdout):
    """Answer each line of stdin on stdout, one request at a time; 0 when stdin closes."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            reply = _error(None, PARSE_ERROR, "the line is not JSON")
        else:
            try:
                reply = handle(message)
            except Exception as e:  # one bad request must not end the session
                log(f"internal error: {type(e).__name__}: {e}")
                reply = _error(message.get("id") if isinstance(message, dict) else None, INTERNAL_ERROR,
                               "internal error")
        if reply is None:
            continue
        try:
            stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stdout.flush()
        except BrokenPipeError:
            return 0  # the client went away
    return 0
