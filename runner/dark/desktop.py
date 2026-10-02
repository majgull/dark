#!/usr/bin/env python3
"""dark desktop - the desktop driver: the object that does to a whole desktop
what `user.PlaywrightPage` does to a web page, so `user.run_steps` can check a
desktop task with the same step loop, quote rule and trail.

The driver sits inside the sandbox, beside `user.py` and `session.py`. It
speaks to the session through an `ExecChannel`: a callable that runs one
command list as the session's user and returns the exit code and the output.
The real channel drops into the session with `setpriv` (never `runuser`,
whose PAM limits can fail in a container) and carries the variables of
`/run/dark-desktop-session`, the file the task's start script writes.

`DesktopPage(channel, compositor)` implements the page interface `run_steps`
expects: `goto`, `url`, `snapshot`, `act`, `screenshot`, `close` and
`drain_requests` (the desktop keeps no request log, so it returns []). Its
snapshot replaces the browser's accessibility tree with a desktop snapshot:
one line per window (id, application id, the title, the workspace or tag, and
which window has focus), then one `ocr:` line per line of text read off a
`grim` frame by `tesseract`. Its `SYSTEM` is the desktop action set; its
`action_target` names an action for the trail, where the browser's names do
not fit.

The compositor is reached through a small table of commands, one entry per
compositor: mango (`mmsg`) and niri (`niri msg`).

`desktop.py` is also the arm's executor, the module `session.py` hands mode
`desktop` to: it writes the task's start script to a file and runs it as root,
takes the steps with `run_steps` over a real `ExecChannel`, then runs the task's
hidden checks with `run_checks` and reports one outcome. `session.py` and
`user.py` (both stdlib-only) are imported for the reporting and the step loop.
"""

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time

try:  # injected alone as /opt/desktop.py beside /opt/session.py and /opt/user.py
    from . import session as S
    from . import user as U
except ImportError:
    import session as S  # noqa: E402
    import user as U  # noqa: E402

# where the task's start script writes the session's environment: the user,
# the display and runtime directory, and any compositor socket
ENV_FILE = "/run/dark-desktop-session"
# How long a launch waits for the new window before the model looks again.
LAUNCH_WAIT = 8.0
# seconds one command list may take before the channel gives up on it
EXEC_TIMEOUT = 30

SYSTEM = """You check one step of a task on a desktop, the way a person would. You see the desktop as a snapshot: one line per window (its id, application id, title, workspace or tag, and which window has focus), then the text read off the screen, one `ocr:` line each. Reply with exactly one JSON object and nothing else, one of:
{"do": "launch", "app": "<desktop entry id>"}
{"do": "key", "key": "<key or chord, e.g. Return or ctrl+c>"}
{"do": "type", "text": "<text to type>"}
{"do": "click", "x": <pixels from the left edge>, "y": <pixels from the top edge>}
{"do": "focus", "name": "<a window title from the snapshot>"}
{"do": "wait", "seconds": <1 to 5>}
{"do": "verdict", "verdict": "pass" or "fail" or "inconclusive", "note": "<one line: what you saw>", "evidence": "<text copied from the snapshot>"}
Give the verdict as soon as the step is done (pass) or shown not to work (fail), or inconclusive when the desktop cannot tell you. Judge only this step.
A pass must carry evidence: a window title or an `ocr:` line copied word for word from the snapshot, so the pass is checked against what the desktop showed. A pass whose evidence is not in the snapshot is refused and you are asked again. A fail needs no evidence.
When a step refers to something an earlier step made, take it from the earlier steps' notes."""


class DesktopError(Exception):
    """The desktop refused an action, or could not be reached: the message is
    what the model is told."""


def read_env_file(path=ENV_FILE):
    """The `KEY=VALUE` lines of the session's environment file as a dict.
    Blank lines and `#` comments are skipped, a value's surrounding quotes are
    taken off, and a missing or unreadable file is an empty dict."""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


class ExecChannel:
    """One command list, run as the session's user, as `(returncode, output)`;
    the output is the command's standard output and standard error together.

    The drop is `setpriv --reuid <uid> --regid <gid> --init-groups`, not
    `runuser`: runuser goes through PAM, whose limits can fail in a container.
    The command runs with the variables the start script wrote to
    `/run/dark-desktop-session`, so `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` and the
    compositor's socket are in reach. The user is named by `DARK_UID` and
    `DARK_GID`, or by `DARK_USER` / `USER`, looked up in the password
    database."""

    def __init__(self, env_file=ENV_FILE, timeout=EXEC_TIMEOUT):
        self.env = read_env_file(env_file)
        self.timeout = timeout
        self._ids = None

    def _identity(self):
        if self._ids is not None:
            return self._ids
        env = self.env
        uid, gid = env.get("DARK_UID"), env.get("DARK_GID")
        if not (uid and gid):
            name = env.get("DARK_USER") or env.get("USER")
            if name:
                try:
                    pw = pwd.getpwnam(name)
                    uid, gid = pw.pw_uid, pw.pw_gid
                except KeyError:
                    raise DesktopError(f"{ENV_FILE} names an unknown user {name!r}") from None
        if not (uid and gid):
            raise DesktopError(f"{ENV_FILE} names no session user (DARK_UID/DARK_GID or USER)")
        self._ids = (str(uid), str(gid))
        return self._ids

    def __call__(self, argv):
        uid, gid = self._identity()
        cmd = ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--", *[str(a) for a in argv]]
        env = dict(os.environ)
        env.update(self.env)
        try:
            p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError as e:
            return 127, f"setpriv not found: {e}"
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {self.timeout}s: {' '.join(str(a) for a in argv)}"
        return p.returncode, (p.stdout or "") + (p.stderr or "")


# --- the compositor table -------------------------------------------------------
def _mango_windows(output):
    """mango's `mmsg get all-clients` reply normalised to the driver's window
    shape: `{"clients": [{"id", "appid", "title", "tags", "is_focused"}]}`."""
    data = json.loads(output or "{}")
    clients = data.get("clients") if isinstance(data, dict) else data
    return [{"id": c.get("id"), "appid": c.get("appid") or "", "title": c.get("title") or "",
             "workspace": c.get("tags"), "is_focused": bool(c.get("is_focused"))}
            for c in clients or []]


def _niri_windows(output):
    """niri's `niri msg --json windows` reply normalised to the driver's
    window shape. niri names the fields `app_id` and `workspace_id`."""
    data = json.loads(output or "[]")
    windows = data.get("windows") if isinstance(data, dict) else data
    return [{"id": w.get("id"), "appid": w.get("app_id") or "", "title": w.get("title") or "",
             "workspace": w.get("workspace_id"), "is_focused": bool(w.get("is_focused"))}
            for w in windows or []]


def _mango_outputs(output):
    """mango's `mmsg get all-monitors` reply as logical rectangles
    `(x, y, width, height)`: mango reports the logical size (a 3840x2160
    output at scale 3 is 1280x720), which is what grim's -g takes."""
    data = json.loads(output or "{}")
    mons = data.get("monitors") if isinstance(data, dict) else data
    return [(int(m.get("x", 0)), int(m.get("y", 0)), int(m["width"]), int(m["height"]))
            for m in mons or [] if m.get("width") and m.get("height")]


def _niri_outputs(output):
    """niri's `niri msg --json outputs` reply as logical rectangles."""
    data = json.loads(output or "{}")
    outs = data.values() if isinstance(data, dict) else data
    rects = []
    for o in outs:
        lg = (o or {}).get("logical") or {}
        if lg.get("width") and lg.get("height"):
            rects.append((int(lg.get("x", 0)), int(lg.get("y", 0)), int(lg["width"]), int(lg["height"])))
    return rects


# name -> how to list the windows, how to read that reply, how to focus one by
# id, and how to list the outputs as logical rectangles
COMPOSITORS = {
    "mango": {"windows": ["mmsg", "get", "all-clients"],
              "parse": _mango_windows,
              "focus": lambda wid: ["mmsg", "dispatch", "focusid", f"client,{wid}"],
              "outputs": ["mmsg", "get", "all-monitors"],
              "parse_outputs": _mango_outputs},
    "niri": {"windows": ["niri", "msg", "--json", "windows"],
             "parse": _niri_windows,
             "focus": lambda wid: ["niri", "msg", "action", "focus-window", "--id", str(wid)],
             "outputs": ["niri", "msg", "--json", "outputs"],
             "parse_outputs": _niri_outputs},
}

# The edge bands read on their own: a shell's bar sits at the top or the
# bottom, and with a window open below it tesseract's page layout drops the
# bar's line from a whole-frame read. Seen in the first VM run of the TV box
# task (1280x800): the frame showed "Nothing Playing", its OCR did not, and a
# read of the top 60 px alone did.
EDGE_BAND = 1 / 12      # of an output's height, at least EDGE_MIN logical px
EDGE_MIN = 40

# the modifiers wtype knows, keyed by the names a model writes in a chord
MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
             "super": "logo", "meta": "logo", "logo": "logo", "win": "logo", "cmd": "logo"}
# the keys libxkbcommon names, keyed by the names a model is likely to write
KEYS = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
        "tab": "Tab", "space": "space", "backspace": "BackSpace", "delete": "Delete",
        "insert": "Insert", "home": "Home", "end": "End",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "pageup": "Prior", "pgup": "Prior", "pagedown": "Next", "pgdn": "Next"}


def wtype_key(chord):
    """The `wtype` command line for a key or chord: `ctrl+c` becomes
    `wtype -M ctrl -k c`, `Return` becomes `wtype -k Return`. A modifier is
    released when wtype exits, as it does for every run."""
    parts = [p.strip() for p in str(chord or "").split("+") if p.strip()]
    if not parts:
        return ["wtype", "-k", "Return"]
    argv = ["wtype"]
    for mod in parts[:-1]:
        argv += ["-M", MODIFIERS.get(mod.lower(), mod.lower())]
    argv += ["-k", KEYS.get(parts[-1].lower(), parts[-1])]
    return argv


def _workspace(tags):
    """A mango client's tags or a niri window's workspace id, as one word for
    the snapshot line."""
    if tags is None or tags == "":
        return "-"
    if isinstance(tags, (list, tuple)):
        return ",".join(str(t) for t in tags) or "-"
    return str(tags)


def window_line(w):
    """One window as the snapshot shows it: id, application id, title,
    workspace, and whether it has focus."""
    return (f'window id={w.get("id")} appid="{w.get("appid") or ""}" title="{w.get("title") or ""}"'
            f' workspace={_workspace(w.get("workspace"))} '
            f'{"focused" if w.get("is_focused") else "unfocused"}')


class DesktopPage:
    """The desktop driver: `channel` runs one command list, `compositor` names
    the entry of COMPOSITORS to speak to. `which` finds a program on PATH and
    is injectable so a test can make `ydotool` absent."""

    SYSTEM = SYSTEM

    def __init__(self, channel, compositor, which=shutil.which, clock=time.monotonic, sleep=time.sleep):
        if compositor not in COMPOSITORS:
            raise DesktopError(f"unknown compositor {compositor!r}; known: {', '.join(sorted(COMPOSITORS))}")
        self.channel = channel
        self.compositor = COMPOSITORS[compositor]
        self.which = which
        self._clock, self._sleep = clock, sleep

    # -- the compositor ---------------------------------------------------------
    def _run(self, argv, what):
        rc, out = self.channel(argv)
        if rc != 0:
            raise DesktopError(f"{what} failed (rc {rc}): {(out or '').strip()[:300]}")
        return out

    def _windows(self):
        """The compositor's window list, normalised: [{"id", "appid", "title",
        "workspace", "is_focused"}]."""
        out = self._run(self.compositor["windows"], "listing the windows")
        try:
            return self.compositor["parse"](out)
        except (ValueError, TypeError, AttributeError) as e:
            raise DesktopError(f"the window list could not be read: {str(e)[:200]}") from None

    # -- the page interface -----------------------------------------------------
    def goto(self, url):
        """Launch the task's first application, when the task names one: a
        desktop has no address to open."""
        if url:
            self._run(["gtk-launch", str(url)], f"launching {url}")

    def url(self):
        """The focused window as `appid "title"`, or `desktop` when none has
        focus: what `run_steps` shows the model as the address."""
        for w in self._windows():
            if w.get("is_focused"):
                return f'{w.get("appid") or "?"} "{w.get("title") or ""}"'
        return "desktop"

    def _edge_bands(self):
        """grim -g geometries of the top and bottom band of every output; [] if
        the compositor gives no outputs (the whole-frame read still stands)."""
        cmd = self.compositor.get("outputs")
        if not cmd:
            return []
        try:
            rects = self.compositor["parse_outputs"](self._run(cmd, "listing the outputs"))
        except (DesktopError, ValueError, TypeError, KeyError, AttributeError):
            return []
        bands = []
        for x, y, w, h in rects:
            bh = max(EDGE_MIN, int(h * EDGE_BAND))
            bands += [f"{x},{y} {w}x{bh}", f"{x},{y + h - bh} {w}x{bh}"]
        return bands

    def snapshot(self):
        """The desktop snapshot: one line per window, then one `ocr:` line per
        line of text `tesseract` reads off a fresh `grim` frame."""
        lines = [window_line(w) for w in self._windows()]
        with tempfile.TemporaryDirectory(prefix="dark-desktop-") as d:
            os.chmod(d, 0o777)  # grim runs as the session user, the executor as root
            png = os.path.join(d, "screen.png")
            rc, _ = self.channel(["grim", png])
            if rc == 0:
                # stderr dropped: the channel merges it, and tesseract's
                # "Estimating resolution as 500" became an OCR line a model
                # quoted as evidence (TV box lab run, 2026-10-02).
                rc, text = self.channel(["sh", "-c", 'tesseract "$1" - 2>/dev/null', "ocr", png])
                if rc == 0:
                    lines += [f"ocr: {line.strip()}" for line in (text or "").splitlines() if line.strip()]
            for band in self._edge_bands():
                rc, _ = self.channel(["grim", "-g", band, png])
                if rc != 0:
                    continue
                rc, text = self.channel(["sh", "-c", 'tesseract "$1" - 2>/dev/null', "ocr", png])
                if rc != 0:
                    continue
                for line in (text or "").splitlines():
                    entry = f"ocr: {line.strip()}"
                    if line.strip() and entry not in lines:
                        lines.append(entry)
        return "\n".join(lines)

    def act(self, action):
        do = action.get("do")
        if do == "launch":
            app = str(action.get("app") or action.get("name") or "")
            if not app:
                raise DesktopError("launch needs an application id")
            # Detached: gtk-launch waits for the program, and an application
            # runs for as long as the session (seen on the TV box image: foot
            # opened and gtk-launch timed out after 30 s).
            before = {w.get("id") for w in self._windows()}
            self._run(["sh", "-c", 'gtk-launch "$1" >/dev/null 2>&1 &', "launch", app], f"launching {app}")
            # A person sees the window open; the model's next look must too.
            # Without this wait the first real run judged "Open a terminal" a
            # fail on a snapshot taken before foot had mapped its window.
            end = self._clock() + LAUNCH_WAIT
            while self._clock() < end:
                if {w.get("id") for w in self._windows()} - before:
                    break
                self._sleep(0.25)
        elif do == "key":
            self._run(wtype_key(action.get("key")), f"key {action.get('key')}")
        elif do == "type":
            self._run(["wtype", "--", str(action.get("text", ""))], "typing")
        elif do == "click":
            self._click(action)
        elif do == "focus":
            self._focus(action)
        elif do == "wait":
            time.sleep(min(5, max(1, int(action.get("seconds") or 1))))
        else:
            raise DesktopError(f"unknown action {do!r}")

    def _click(self, action):
        """A click at screen coordinates. It needs ydotool (the uinput pointer
        tool); a desktop without it refuses the click rather than silently
        doing nothing."""
        if not self.which("ydotool"):
            raise DesktopError("refused: a click needs ydotool on PATH, and this desktop has no ydotool")
        x, y = int(action.get("x") or 0), int(action.get("y") or 0)
        self._run(["ydotool", "mousemove", "--absolute", str(x), str(y)], "moving the pointer")
        self._run(["ydotool", "click", "0xC0"], "clicking")

    def _focus(self, action):
        """Focus a window named by the snapshot: its id when the model gives
        one, else its title, exactly or as the one window whose title contains
        the name."""
        windows = self._windows()
        wid = action.get("id")
        if wid is None:
            want = str(action.get("name") or action.get("title") or "")
            exact = [w for w in windows if str(w.get("title") or "") == want]
            near = [w for w in windows if want and want in str(w.get("title") or "")]
            pick = exact or near
            if not pick:
                titles = ", ".join(json.dumps(str(w.get("title") or "")) for w in windows)
                raise DesktopError(f'refused: no window titled {want!r} in the snapshot; titles: {titles}')
            if len(pick) > 1:
                titles = ", ".join(json.dumps(str(w.get("title") or "")) for w in pick)
                raise DesktopError(f'refused: {len(pick)} windows contain {want!r}: {titles}; name one exactly')
            wid = pick[0].get("id")
        self._run(self.compositor["focus"](wid), f"focusing window {wid}")

    def screenshot(self, path):
        """grim runs as the session user, who cannot write into the records
        directory the executor (root) owns: the frame goes to a scratch
        directory the user can write, then is copied into place."""
        with tempfile.TemporaryDirectory(prefix="dark-desktop-") as d:
            os.chmod(d, 0o777)
            png = os.path.join(d, "screen.png")
            self._run(["grim", png], "screenshot")
            if os.path.exists(png):
                shutil.copyfile(png, str(path))

    def close(self):
        """Nothing to close: the session outlives the run, the sandbox does not."""

    def drain_requests(self):
        """The desktop keeps no request log, so the trail's requests stay []."""
        return []

    def action_target(self, action):
        """The trail's `target` for one action, named the desktop's way (the
        app of a launch, the chord of a key, the coordinates of a click),
        where the browser's action_target names a locator."""
        do = action.get("do")
        if do == "launch":
            return f'launch "{action.get("app") or action.get("name") or ""}"'
        if do == "key":
            return str(action.get("key") or "")
        if do == "type":
            return f'type "{str(action.get("text", ""))[:60]}"'
        if do == "click":
            return f'click {int(action.get("x") or 0)},{int(action.get("y") or 0)}'
        if do == "focus":
            return f'focus "{(action.get("name") or action.get("title") or action.get("id") or "")}"'
        if do == "wait":
            return f'{action.get("seconds") or 1}s'
        return json.dumps(action, sort_keys=True)


# --- the hidden checks: the task's own commands, run after the steps ----------
CHECKS_FILE = "checks.jsonl"
# a check's output is cut here in its record: a person reads it, and a check that
# prints a whole file must not make the records repo unbounded
CHECK_OUTPUT_CHARS = 2000
# a check's own default, when the task names no timeout (tasks.CHECK_TIMEOUT)
CHECK_TIMEOUT = 60


def run_checks(checks, records_dir, run=subprocess.run, target=None):
    """Run the task's hidden checks after the steps, each as root with `sh -c`,
    each under its own timeout. One `checks.jsonl` line per check, in order:
    id, command, rc, the output cut to CHECK_OUTPUT_CHARS characters, and the
    verdict pass or fail (rc 0 or not, a timeout is rc 124). Returns
    (ok, total): how many checks passed, of how many ran. A check whose
    `targets` leave out this run's `target` does not run: its line says
    `skipped` and it counts in neither number."""
    path = os.path.join(records_dir, CHECKS_FILE)
    ok = ran = 0
    for c in checks:
        cid = str(c.get("id") or "")
        command = str(c.get("command") or "")
        if target and target not in (c.get("targets") or (target,)):
            with open(path, "a") as f:
                f.write(json.dumps({"id": cid, "command": command, "rc": None, "output": "",
                                    "verdict": "skipped", "why": f"not for the {target} target"},
                                   sort_keys=True) + "\n")
            continue
        ran += 1
        timeout = c.get("timeout") or CHECK_TIMEOUT
        try:
            r = run(["sh", "-c", command], capture_output=True, text=True, timeout=timeout)
            rc = int(r.returncode)
            output = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            rc, output = 124, f"timed out after {timeout}s"
        except OSError as e:
            rc, output = 127, str(e)
        verdict = "pass" if rc == 0 else "fail"
        ok += verdict == "pass"
        with open(path, "a") as f:
            f.write(json.dumps({"id": cid, "command": command, "rc": rc,
                                "output": output[:CHECK_OUTPUT_CHARS], "verdict": verdict},
                               sort_keys=True) + "\n")
    return ok, ran


def records_paths(records_dir):
    """What the desktop records push adds beside stream.jsonl, brief.md and
    task.json: the steps with their screenshots and trails, and the hidden
    checks' own lines. The push skips a path that does not exist."""
    return {"steps.jsonl": os.path.join(records_dir, "steps.jsonl"),
            "steps": os.path.join(records_dir, "steps"),
            CHECKS_FILE: os.path.join(records_dir, CHECKS_FILE)}


def verdict(results, stop, checks_ok, checks_total):
    """(outcome, kind, detail) for a desktop run, from the steps' results, the
    reason the step loop stopped, and the checks' tally. The steps come first:
    a failed step is the model's doing. A check that failed is a failing kind
    `checks` even when every step passed, because a check is the verdict the
    model never sees. A step no one could judge, with nothing failed and no
    check failed, is `inconclusive`; else the run passes."""
    steps_ok = sum(1 for r in results if r.get("verdict") == "pass")
    failed = [r for r in results if r.get("verdict") == "fail"]
    unjudged = [r for r in results if r.get("verdict") == "inconclusive"]
    total = len(results)
    tally = f"{steps_ok}/{total} steps pass, {checks_ok}/{checks_total} checks pass"
    if stop == "seconds":
        return "fail:budget", "seconds", f"wall envelope spent at {tally}"
    if stop == "calls" and not unjudged:  # a calls stop always leaves inconclusive steps
        return "fail:budget", "calls", f"call envelope spent at {tally}"
    if failed:
        reasons = "; ".join(f"step {r.get('step')}: {r.get('note')}" for r in failed)
        return "fail:capability", "steps", f"{len(failed)} of {total} steps failed ({tally}): {reasons}"
    if checks_total and checks_ok < checks_total:
        return ("fail:capability", "checks",
                f"{checks_total - checks_ok} of {checks_total} checks failed ({tally})")
    if unjudged:
        reasons = "; ".join(f"step {r.get('step')}: {r.get('note')}" for r in unjudged)
        return ("inconclusive", None,
                f"{tally}; no step failed, but {len(unjudged)} could not be judged: {reasons}")
    return "pass", None, tally


# --- the executor: what runs inside the sandbox (session.py, mode desktop) ---
START_FILE = "/opt/dark-desktop-start.sh"
# a start script brings a session up and waits for it; its own loop bounds that
START_TIMEOUT = 900
RECORDS_PUSH_SECONDS = U.RECORDS_PUSH_SECONDS


def write_start(text, path=START_FILE):
    """Write the task's start script to `path`, executable, and return it."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def run_start(text, run=subprocess.run, path=START_FILE, timeout=START_TIMEOUT):
    """Bring the desktop session up: write the start script to a file and run
    it with `sh` as root (this executor is root), before anything else.
    Returns (rc, output); a timeout is rc 124, as a check's is."""
    write_start(text, path)
    try:
        r = run(["sh", path], capture_output=True, text=True, timeout=timeout)
        return int(r.returncode), (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)


def fail(kind, text, **kw):
    S.comment(f"AGENT-DONE fail ({kind}): {text}\n"
              + S.done("fail", kind, **kw,
                       **S.records_kw(extra_paths=records_paths(S.RECORDS_DIR),
                                      timeout=RECORDS_PUSH_SECONDS)))
    return 1


def main(model=None, page=None, channel=None, run=subprocess.run, start_path=START_FILE):
    try:
        return _main(model, page, channel, run, start_path)
    except Exception as e:  # noqa: BLE001 — always leave a trace on the issue
        S.comment(S.crash_text(e) + "\n"
                  + S.done("fail", "crash", error=type(e).__name__,
                           **S.records_kw(extra_paths=records_paths(S.RECORDS_DIR),
                                          timeout=RECORDS_PUSH_SECONDS)))
        raise
    finally:
        S.PROGRESS.stop()


def _main(model, page, channel, run, start_path):
    """The desktop arm inside its sandbox: the start script first, as root, then
    the step loop through a `DesktopPage` over the real `ExecChannel`, then the
    hidden checks. `model`, `page`, `channel` and `run` are injectable so a test
    drives this without a desktop or a real check."""
    t = S.TASK
    records = S.RECORDS_DIR
    os.makedirs(records, exist_ok=True)
    # run_steps writes the transcript where the records push reads it, and it
    # exists even when no call was made (its sha256 goes on the done tag)
    S.STREAM_PATH = os.path.join(records, "stream.jsonl")
    open(S.STREAM_PATH, "a").close()
    steps = list(t.get("steps") or [])
    checks = list(t.get("checks") or [])
    max_calls = int(t.get("max_calls") or 20)
    S.PROGRESS.start(f"AGENT-ALIVE run {t.get('run')} model {t.get('llm_model')} "
                     f"envelope {max_calls} calls (desktop arm, {len(steps)} steps)\n"
                     + S.tag("start", model=t.get("llm_model"), calls_max=max_calls))
    if not steps:
        return fail("env", "task.json carries no steps")
    rc, out = run_start(t.get("start") or "", run=run, path=start_path)
    if rc != 0:
        return fail("env", f"the start script failed (rc {rc}): {out[-800:]}")
    if page is None:
        channel = channel or ExecChannel()
        compositor = channel.env.get("DARK_COMPOSITOR")
        if not compositor:
            return fail("env", f"the start script wrote no DARK_COMPOSITOR to {ENV_FILE}")
        try:
            page = DesktopPage(channel, compositor)
        except DesktopError as e:
            return fail("env", str(e))
    model = model or U.ChatModel(t)
    deadline = S.T0 + int(t.get("max_seconds") or 900)
    try:
        results, stop = U.run_steps(model, page, "", steps, records, max_calls, deadline)
    except U.ModelError as e:
        return fail("llm", str(e))
    except DesktopError as e:
        return fail("env", str(e))
    checks_ok, checks_total = run_checks(checks, records, run=run, target=t.get("target") or "lab")
    outcome, kind, detail = verdict(results, stop, checks_ok, checks_total)
    kw = {"steps_ok": sum(1 for r in results if r.get("verdict") == "pass"),
          "steps_total": len(results), "checks_ok": checks_ok, "checks_total": checks_total}
    if outcome == "pass":
        S.comment(f"AGENT-DONE ok {detail} calls={S.STATS['calls']}\n"
                  + S.done("ok", **kw, **S.records_kw(extra_paths=records_paths(records),
                                                       timeout=RECORDS_PUSH_SECONDS)))
        return 0
    if outcome == "inconclusive":
        S.comment(f"AGENT-DONE inconclusive: {detail}\n"
                  + S.done("inconclusive", **kw,
                           **S.records_kw(extra_paths=records_paths(records),
                                          timeout=RECORDS_PUSH_SECONDS)))
        return 1
    return fail(kind or outcome, detail, **kw)


if __name__ == "__main__":
    import sys

    sys.exit(main())
