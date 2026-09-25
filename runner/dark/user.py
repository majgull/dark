#!/usr/bin/env python3
"""dark user - the executor for the user arm: a fresh sandbox that gets only
a URL and a task written as numbered steps, and checks the deployed
application with a browser, the way a user would, without ever seeing its
source. Runs INSIDE the throwaway sandbox (the browser image), injected with
/opt/task.json beside dark/session.py, whose reporting it shares: the same
progress comment and heartbeat, the same AGENT-DONE tag, the same records
push.

For each step the model is asked for the next browser action, given the
step text, the notes of the steps already finished, and the page's
accessibility snapshot; the action is performed, and the model is asked
again until it gives the step a verdict, pass or fail, with one line of
note. Then a screenshot is taken as
<records>/steps/<NN>.png and {step, verdict, note} is appended to
<records>/steps.jsonl. The run passes when every step's verdict is pass.

The real browser also records the whole run: a Playwright trace as
<records>/trace.zip and a video as <records>/video.webm, both written when
the page is closed. A recording that cannot be made or saved is skipped and
never changes a step, a verdict or a screenshot name.

The model is reached through `ChatModel` and the browser through
`PlaywrightPage`; both sit behind a small interface (`next_action`, and
goto/url/snapshot/act/screenshot/close), so tests drive `run_steps` and
`main` with a scripted model and a fake page. Stdlib only, apart from
Playwright, which only the browser image has and which is imported when the
real page is opened.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    from . import session as S
except ImportError:  # injected as /opt/user.py beside /opt/session.py and run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session as S  # noqa: E402

SNAPSHOT_CHARS = 12000  # the page as the model sees it, cut to this
ACTION_TIMEOUT_MS = 10000
VIEWPORT = {"width": 1280, "height": 720}  # Playwright's default, spelled out: the video is this size too
TRACE_FILE, VIDEO_FILE = "trace.zip", "video.webm"  # beside steps.jsonl in the records

SYSTEM = """You check one step of a task in a web application through a browser, the way a user would. You see the page as an accessibility snapshot. Reply with exactly one JSON object and nothing else, one of:
{"do": "click", "role": "<role>", "name": "<accessible name>"}
{"do": "fill", "role": "<role>", "name": "<accessible name>", "text": "<text to type>"}
{"do": "select", "role": "combobox", "name": "<accessible name>", "value": "<option>"}
{"do": "press", "key": "<key, e.g. Enter>"}
{"do": "goto", "url": "<address>"}
{"do": "wait", "seconds": <1 to 5>}
{"do": "verdict", "verdict": "pass" or "fail", "note": "<one line: what you saw>"}
Give the verdict as soon as the step is done (pass) or shown not to work (fail). Judge only this step.
When a step refers to something an earlier step made (an order, a job, a code), take it from the earlier steps' notes."""

PROMPT = """TASK:
{spec}

STEP {n} of {total}: {text}

URL: {url}

EARLIER STEPS:
{earlier}

ACTIONS SO FAR IN THIS STEP:
{history}

PAGE (accessibility snapshot):
{snapshot}
"""


class ModelError(Exception):
    """The model endpoint failed: the run is fail:structural (llm)."""


class BrowserError(Exception):
    """The browser could not start or open the URL: fail:structural (env)."""


# --- the model ------------------------------------------------------------------
def earlier_steps(results):
    """The finished steps as `step N (pass|fail): <note>` lines, or `none`
    when none has finished yet: the block the prompt shows so that a step
    which refers to something an earlier step made can read its note."""
    return "\n".join(f"step {r['step']} ({r['verdict']}): {r['note']}" for r in (results or [])) or "none"


def parse_action(text):
    """The first JSON object in a reply, or {"do": "invalid"} with the reply
    kept, so a malformed answer costs a call, never the run."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and isinstance(d.get("do"), str):
                return d
        except ValueError:
            pass
    return {"do": "invalid", "reply": (text or "")[:300]}


class ChatModel:
    """next_action over the provider entry the session arm uses: the same
    llm_url and model id, OpenAI chat completions."""

    def __init__(self, task):
        self.t = task

    def body(self, messages):
        t = self.t
        body = {"model": t["llm_model"], "messages": messages}
        response = int(t.get("max_tokens") or 8192)
        level = t.get("think")
        if level:
            chars = int(t.get("think_chars") or 0)
            body["max_tokens"] = response + -(-chars // int(t.get("chars_per_token") or 3))
            api = t.get("think_api") or "none"
            if api == "reasoning_effort":
                body["reasoning_effort"] = level
            elif api == "chat_template":
                body["chat_template_kwargs"] = {"enable_thinking": level != "none"}
        else:
            body["max_tokens"] = response
        if t.get("temperature") is not None:
            body["temperature"] = t["temperature"]
        return body

    def next_action(self, n, text, url, snapshot, history, earlier=None):
        prompt = PROMPT.format(spec=self.t.get("spec", ""), n=n, total=len(self.t.get("steps") or []),
                               text=text, url=url, snapshot=snapshot[:SNAPSHOT_CHARS],
                               earlier=earlier_steps(earlier),
                               history="\n".join(json.dumps(h, sort_keys=True) for h in history) or "(none)")
        req = urllib.request.Request(
            f"{self.t['llm_url']}/chat/completions", method="POST",
            data=json.dumps(self.body([{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": prompt}])).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(self.t.get("llm_timeout") or 600)) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ModelError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ModelError(f"{type(e).__name__}: {e}") from None
        u = d.get("usage") or {}
        S.STATS["tokens_in"] += int(u.get("prompt_tokens") or 0)
        S.STATS["tokens_out"] += int(u.get("completion_tokens") or 0)
        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        S.STATS["reasoning_chars"] += len(msg.get("reasoning") or msg.get("reasoning_content") or "")
        return parse_action(msg.get("content") or "")


# --- the browser ----------------------------------------------------------------
class PlaywrightPage:
    """The driver interface over Playwright's sync API and the image's own
    Chromium (DARK_CHROMIUM); Playwright never downloads a browser.

    With `records_dir` the run is also recorded: a trace (a DOM snapshot and a
    screencast frame per action, console output and network calls) is started
    before the page opens and saved as <records_dir>/trace.zip by close(), and
    the context records a video, saved as <records_dir>/video.webm by close().
    Both are best effort: one that cannot start or be saved is skipped."""

    def __init__(self, records_dir=None):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserError(f"playwright is not installed (wrong image?): {e}") from None
        self._records = records_dir
        self._video_dir = tempfile.mkdtemp(prefix="dark-video-") if records_dir else None
        self._video = self._tracing = False
        try:
            self._pw = sync_playwright().start()
            exe = os.environ.get("DARK_CHROMIUM", "/usr/bin/chromium")
            self._browser = self._pw.chromium.launch(
                executable_path=exe if os.path.exists(exe) else None, args=["--no-sandbox"])
            try:
                self._open(video=bool(records_dir))
            except Exception as e:  # noqa: BLE001
                if not records_dir:
                    raise
                # an image without Playwright's ffmpeg cannot record a video: the run goes on without one
                print(f"video recording unavailable, opening the page without it: {e}", file=sys.stderr)
                self._open(video=False)
        except Exception as e:  # noqa: BLE001 — any launch failure is the environment's
            raise BrowserError(f"chromium did not start: {e}") from None

    def _open(self, video):
        kw = {"viewport": dict(VIEWPORT)}
        if video:
            kw.update(record_video_dir=self._video_dir, record_video_size=dict(VIEWPORT))
        self._context = self._browser.new_context(**kw)
        self._video, self._tracing = video, False
        if self._records:
            try:
                # before the page is created, so the first navigation is in the trace
                self._context.tracing.start(screenshots=True, snapshots=True)
                self._tracing = True
            except Exception as e:  # noqa: BLE001
                # no trace, and the run goes on
                print(f"trace not started: {e}", file=sys.stderr)
        self.page = self._context.new_page()

    def goto(self, url):
        try:
            self.page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"{url} did not open: {e}") from None

    def url(self):
        return self.page.url

    def snapshot(self):
        try:
            return self.page.locator("body").aria_snapshot()
        except Exception:  # noqa: BLE001 — an older Playwright: the accessibility tree as JSON
            return json.dumps(self.page.accessibility.snapshot(), indent=1)

    def _target(self, a):
        return self.page.get_by_role(a.get("role") or "button", name=a.get("name") or None).first

    def act(self, a):
        do = a.get("do")
        if do == "click":
            self._target(a).click(timeout=ACTION_TIMEOUT_MS)
        elif do == "fill":
            self._target(a).fill(str(a.get("text", "")), timeout=ACTION_TIMEOUT_MS)
        elif do == "select":
            self._target(a).select_option(str(a.get("value", "")), timeout=ACTION_TIMEOUT_MS)
        elif do == "press":
            self.page.keyboard.press(str(a.get("key") or "Enter"))
        elif do == "goto":
            self.page.goto(str(a.get("url") or ""), wait_until="load", timeout=30000)
        elif do == "wait":
            self.page.wait_for_timeout(1000 * min(5, max(1, int(a.get("seconds") or 1))))
        else:
            raise ValueError(f"unknown action {do!r}")
        try:
            self.page.wait_for_load_state("load", timeout=ACTION_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — a page that keeps loading is still a page to look at
            pass

    def screenshot(self, path):
        self.page.screenshot(path=path, full_page=True)

    def _save_trace(self):
        if self._tracing:
            self._context.tracing.stop(path=os.path.join(self._records, TRACE_FILE))

    def _save_video(self):
        # the file exists once the context is closed; a browser that died leaves none
        if self._video:
            shutil.move(self.page.video.path(), os.path.join(self._records, VIDEO_FILE))

    def close(self):
        for f in (self._save_trace, lambda: self._context.close(), self._save_video,
                  lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
        if self._video_dir:
            shutil.rmtree(self._video_dir, ignore_errors=True)


# --- the steps ------------------------------------------------------------------
def _append(path, obj):
    with open(path, "a") as f:
        f.write(S.scrub(json.dumps(obj, sort_keys=True)) + "\n")


def run_steps(model, page, url, steps, records_dir, max_calls, deadline, clock=time.time):
    """Open `url` and take every step in order. Returns (results, stop):
    results is one {step, verdict, note} per step, in order, as also
    appended to <records_dir>/steps.jsonl; stop is None, or "calls" /
    "seconds" when the envelope ran out, in which case the step it ran out
    in and every step after it are fail. Each step that was taken leaves
    <records_dir>/steps/<NN>.png. The transcript of every call goes to
    <records_dir>/stream.jsonl. ModelError and BrowserError propagate."""
    steps_dir = os.path.join(records_dir, "steps")
    os.makedirs(steps_dir, exist_ok=True)
    jsonl = os.path.join(records_dir, "steps.jsonl")
    stream = os.path.join(records_dir, "stream.jsonl")
    page.goto(url)
    results, stop = [], None
    for n, text in enumerate(steps, 1):
        if stop:
            rec = {"step": n, "verdict": "fail", "note": f"not reached: the {stop} envelope was spent"}
            results.append(rec)
            _append(jsonl, rec)
            continue
        history, verdict, note = [], None, ""
        while verdict is None:
            if S.STATS["calls"] >= max_calls:
                stop, verdict, note = "calls", "fail", f"the call envelope ({max_calls}) was spent before a verdict"
                break
            if clock() > deadline:
                stop, verdict, note = "seconds", "fail", "the wall envelope was spent before a verdict"
                break
            snap = page.snapshot() or ""
            S.STATS["calls"] += 1
            S.STATS["requests"] += 1
            action = model.next_action(n, text, page.url(), snap, history, earlier=results)
            entry = {"step": n, "call": S.STATS["calls"], "url": page.url(), "snapshot_chars": len(snap),
                     "action": action}
            if action.get("do") == "verdict":
                if action.get("verdict") in ("pass", "fail"):
                    verdict, note = action["verdict"], str(action.get("note") or "")[:300]
                else:
                    history.append({"action": action, "error": "verdict must be pass or fail"})
            elif action.get("do") == "invalid":
                history.append({"action": action, "error": "not one JSON action; reply with one JSON object"})
            else:
                S.STATS["tool_calls"] += 1
                try:
                    page.act(action)
                    history.append({"action": action, "ok": True})
                except Exception as e:  # noqa: BLE001 — a failed action is evidence, not a crash
                    history.append({"action": action, "error": f"{type(e).__name__}: {e}"[:300]})
            entry["result"] = history[-1] if history and verdict is None else {"verdict": verdict}
            _append(stream, entry)
        page.screenshot(os.path.join(steps_dir, f"{n:02d}.png"))
        rec = {"step": n, "verdict": verdict, "note": note}
        results.append(rec)
        _append(jsonl, rec)
        S.PROGRESS.add(f"step {n}: {verdict}")
    return results, stop


def records_paths(records_dir):
    """What the records push adds beside stream.jsonl, brief.md, task.json.
    trace.zip and video.webm are listed always; the push skips a path that
    does not exist, so a recording that was not made is not an error."""
    return {"steps.jsonl": os.path.join(records_dir, "steps.jsonl"),
            "steps": os.path.join(records_dir, "steps"),
            TRACE_FILE: os.path.join(records_dir, TRACE_FILE),
            VIDEO_FILE: os.path.join(records_dir, VIDEO_FILE)}


def fail(kind, text, **kw):
    S.comment(f"AGENT-DONE fail ({kind}): {text}\n"
              + S.done("fail", kind, **kw, **S.records_kw(extra_paths=records_paths(S.RECORDS_DIR))))
    return 1


def main(model=None, page=None):
    try:
        return _main(model, page)
    finally:
        S.PROGRESS.stop()


def _main(model, page):
    t = S.TASK
    url, steps = t.get("url"), list(t.get("steps") or [])
    records = S.RECORDS_DIR
    os.makedirs(records, exist_ok=True)
    # run_steps writes the transcript where the records push reads it, and it
    # exists even when no call was made (its sha256 goes on the done tag)
    S.STREAM_PATH = os.path.join(records, "stream.jsonl")
    open(S.STREAM_PATH, "a").close()
    max_calls = int(t.get("max_calls") or 20)
    S.PROGRESS.start(f"AGENT-ALIVE run {t.get('run')} model {t.get('llm_model')} "
                     f"envelope {max_calls} calls (user arm, {len(steps)} steps)\n"
                     + S.tag("start", model=t.get("llm_model"), calls_max=max_calls))
    if not url or not steps:
        return fail("env", "task.json carries no url or no steps")
    try:
        page = page or PlaywrightPage(records)
    except BrowserError as e:
        return fail("env", str(e))
    model = model or ChatModel(t)
    deadline = S.T0 + int(t.get("max_seconds") or 900)
    try:
        try:
            results, stop = run_steps(model, page, url, steps, records, max_calls, deadline)
        finally:
            page.close()  # before any fail() below pushes the records: this is what saves trace.zip and video.webm
    except BrowserError as e:
        return fail("env", str(e))
    except ModelError as e:
        return fail("llm", str(e))
    ok = sum(1 for r in results if r["verdict"] == "pass")
    kw = {"steps_ok": ok, "steps_total": len(steps)}
    if stop == "calls":
        return fail("calls", f"call envelope ({max_calls}) spent at {ok}/{len(steps)} steps", **kw)
    if stop == "seconds":
        return fail("seconds", f"wall envelope spent at {ok}/{len(steps)} steps", **kw)
    if ok < len(steps):
        failed = "; ".join(f"step {r['step']}: {r['note']}" for r in results if r["verdict"] != "pass")
        return fail("steps", f"{len(steps) - ok} of {len(steps)} steps failed: {failed}", **kw)
    S.comment(f"AGENT-DONE ok steps={ok}/{len(steps)} calls={S.STATS['calls']}\n"
              + S.done("ok", **kw, **S.records_kw(extra_paths=records_paths(records))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — always leave a trace on the issue
        S.comment(f"AGENT-DONE fail (crash): {e!r}\n" + S.done("fail", "crash", error=type(e).__name__))
        raise
