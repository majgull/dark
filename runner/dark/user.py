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
again until it gives the step a verdict, pass, fail or inconclusive, with
one line of note. A pass verdict must quote the page in its `evidence`
field, and is accepted only when every fragment of that quote is found in
the snapshot the model was shown for that call, compared with case,
whitespace and the punctuation at a line edge ignored, with the snapshot's
role prefixes taken off and with a fragment boundary drawn where the model
joined separate elements. A step that runs out of calls, repeats one
browser action on an unchanged page, or has one pass verdict refused for
the same missing quote three times on an unchanged page (however its note is
worded), is recorded inconclusive by the arm itself; a pass that
names another identifier than an earlier step named (job HL-17 where step 3
sent HL-18) is refused the same way; a wait only
passes time and never counts toward the repetition. Then a screenshot is
taken as <records>/steps/<NN>.png, a trail is
written as <records>/steps/<NN>.trail.jsonl (one line per action: when it
happened, what it acted on, and the requests the page made before the next
snapshot) and {step, verdict, note} is appended to <records>/steps.jsonl,
with the evidence on a pass. The run passes when every step's verdict is pass.

Before a step's first action a separate call, the judge, is given only the
step's text and the page snapshot, never the acting model's messages, and
names the element or text that will show the step succeeded; the step's line
carries it as `expected`, with the judge's one sentence as `expected_why`. A
judge call that fails or does not answer in that form records `expected: null`
with the reason and changes nothing else about the step. The judge's call
counts against the same calls and wall envelopes as the acting model's.

An action names its element by role and accessible name. An element named
exactly that is used, the first when several are; when no name is exact, one
element whose name contains it is used, and several are refused with their
names, so the model can name one precisely: the action clicks nothing, and
the step's history and trail mark it refused. (Playwright's own match is a
substring, and its `.first` clicked a commit link `records: <run>` listed
before the run's folder link `<run>`.)

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
import urllib.parse
import urllib.request

try:
    from . import session as S
except ImportError:  # injected as /opt/user.py beside /opt/session.py and run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import session as S  # noqa: E402

# the page as the model sees it, cut to this: 12000 hid the folder of a Gitea records
# page behind its file tree (37 percent of 362 recorded snapshots were longer; 95
# percent are within 32000), and the model judged a page it had not been shown
SNAPSHOT_CHARS = 32000
JOIN_CHARS = "—–-\"'…."  # the punctuation an accessibility snapshot puts at a line's edge
MIN_FRAGMENT = 8  # a quote part this short is a word, not a quote: it keeps its neighbour
# the ARIA roles an accessibility snapshot names its nodes by, as the model copies them into a quote
ROLES = ("alert application article banner blockquote button caption cell checkbox code columnheader "
         "combobox complementary contentinfo definition deletion dialog document emphasis figure form "
         "generic grid gridcell group heading img insertion link list listbox listitem log main mark "
         "math menu menubar menuitem meter navigation note option paragraph progressbar radio region "
         "row rowgroup rowheader search searchbox separator slider spinbutton status strong subscript "
         "superscript switch tab table tablist tabpanel term textbox time timer toolbar tooltip tree "
         "treeitem text").split()
_ROLE = "(?:" + "|".join(ROLES) + ")"
# where a model joins separate page elements: a dash between spaces, `; `, `. `,
# an ellipsis between spaces, and a closing quote followed by the next node's role
# (`main "dark-records/adhoc" link "adhoc"`, the snapshot's own lines run together)
FRAGMENT_SPLIT = re.compile(r"\s(?:\.\.\.|…)\s|\s[—–-]\s|;\s+|\.\s+"
                            rf"|(?<=[\"'])\s+(?={_ROLE}(?:\s+\"|:\s))")
# a node written as `role "name"` with no colon after it, as a model quotes one: the name stays
NAMED_NODE = re.compile(rf'^{_ROLE}\s+"([^"]*)"(?:\s*\[[^\]]*\])*\s*(?=$|[^:\s])')
# a snapshot line as the name it shows: YAML's quoting of a name with `: ` in it undone,
# then `role "name" [attrs]: rest` read as `name rest`
YAML_QUOTED = re.compile(r"^'((?:[^']|'')*)'(:.*)?$")
NODE_LINE = re.compile(r'^[a-z][a-z-]*\s+"([^"]*)"(?:\s*\[[^\]]*\])*\s*(?::\s*(.*))?$')
LINE_MARKER = re.compile(r"^[-*]\s+")  # the `- ` an accessibility tree puts before a node
ROLE_PREFIX = re.compile(r'^[a-z][a-z-]*(?:\s+"[^"]*")?\s*:')  # `button "Open": ` / `text: `
ACTION_TIMEOUT_MS = 10000
# seconds of the wall envelope kept back for ending the run: the last browser
# action (a goto takes up to 30 s), the screenshot, closing the page (which
# writes trace.zip and video.webm), README.md and the records push, and the
# sandbox's boot, since the runner counts its envelope from the spawn and the
# arm from its own start. No model call starts inside it, and none may run into it.
RECORDS_RESERVE_SECONDS = 120
VIEWPORT = {"width": 1280, "height": 720}  # Playwright's default, spelled out: the video is this size too
TRACE_FILE, VIDEO_FILE = "trace.zip", "video.webm"  # beside steps.jsonl in the records
TRAIL_SUFFIX = ".trail.jsonl"  # <records>/steps/<NN>.trail.jsonl, beside the step's <NN>.png

SYSTEM = """You check one step of a task in a web application through a browser, the way a user would. You see the page as an accessibility snapshot. Reply with exactly one JSON object and nothing else, one of:
{"do": "click", "role": "<role>", "name": "<accessible name>"}
{"do": "fill", "role": "<role>", "name": "<accessible name>", "text": "<text to type>"}
{"do": "select", "role": "combobox", "name": "<accessible name>", "value": "<option>"}
{"do": "press", "key": "<key, e.g. Enter>"}
{"do": "goto", "url": "<address>"}
{"do": "wait", "seconds": <1 to 5>}
{"do": "verdict", "verdict": "pass" or "fail" or "inconclusive", "note": "<one line: what you saw>", "evidence": "<text copied from the page>"}
Give the verdict as soon as the step is done (pass) or shown not to work (fail), or inconclusive when the page cannot tell you. Judge only this step.
A pass must carry evidence: text copied word for word from the page's snapshot, so the pass is checked against what the page showed. A pass whose evidence is not on the page is refused and you are asked again. A fail needs no evidence.
When a step refers to something an earlier step made (an order, a job, a code), take it from the earlier steps' notes.
Name an element by its whole accessible name as the snapshot shows it. An element named exactly that is used; a name that only part of several elements' names matches is refused, and you are shown their names to pick one."""

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


JUDGE_SYSTEM = """You say in advance where a web page will show that one step of a task succeeded. You see the step's text and the page as an accessibility snapshot, before anything is done on it. Name the one element or the text on the page, as the snapshot names it, that will show the step succeeded. Reply with exactly one JSON object and nothing else:
{"expected": "<element name or text>", "why": "<one sentence>"}"""

JUDGE_PROMPT = """STEP: {text}

PAGE (accessibility snapshot):
{snapshot}
"""


class ModelError(Exception):
    """The model endpoint failed: the run is fail:structural (llm)."""


class BrowserError(Exception):
    """The browser could not start or open the URL: fail:structural (env)."""


# --- the model ------------------------------------------------------------------
def shown_snapshot(snapshot):
    """The page as the model sees it: cut to SNAPSHOT_CHARS, the one place
    that cut is spelled, so a pass verdict is judged against what was shown.
    A cut page ends with a line saying so and how much is missing, so the
    model does not take the part it was shown for the whole page."""
    snapshot = snapshot or ""
    if len(snapshot) <= SNAPSHOT_CHARS:
        return snapshot
    return (snapshot[:SNAPSHOT_CHARS] +
            f"\n[the snapshot is cut here: {len(snapshot) - SNAPSHOT_CHARS} more characters of this page are not shown]")


def _normalised(text):
    """How evidence and page are compared: lower-cased, every run of
    whitespace, a line break included, squeezed to one space, and the
    punctuation that decorates a line edge—dashes, quotes, ellipsis, full
    stops—dropped at either side of every space, so a quote copied across two
    or three adjacent snapshot lines still matches."""
    squeezed = " ".join(str(text or "").lower().split())
    edge = re.escape(JOIN_CHARS)
    trimmed = re.sub(rf"(?<=\s)[{edge}]+|[{edge}]+(?=\s)|^[{edge}]+|[{edge}]+$", "", squeezed)
    return " ".join(trimmed.split())


def strip_roles(tree):
    """The accessibility tree with its role prefixes taken off: the line
    marker, then a leading `role "name":` or `role:` token, so
    `- button "Open Road Trip": Road Trip` is `Road Trip` and
    `- text: 20 of 20 saved` is `20 of 20 saved`. A line with no such token,
    `- heading "Shop"`, is left as it is, so a quote of the snapshot's own
    words still matches."""
    out = []
    for line in str(tree or "").splitlines():
        line = LINE_MARKER.sub("", line.strip(), count=1)
        out.append(ROLE_PREFIX.sub("", line, count=1))
    return "\n".join(out)


def page_text(snapshot):
    """The snapshot as evidence is looked for in it: role prefixes removed,
    then collapsed by `_normalised`, which also joins adjacent lines."""
    return _normalised(strip_roles(snapshot))


def page_names(snapshot):
    """The snapshot with every node read as the name it shows: `- link "x":`
    is `x`, `- button "Open": Road Trip` is `Open Road Trip`, and a line YAML
    quoted because its name holds `: ` (`- 'link "records: a"':`) is `records:
    a`. `strip_roles` drops a name followed by a colon, so a link with a /url
    child, as every Gitea link has, left nothing of its name to quote."""
    out = []
    for line in str(snapshot or "").splitlines():
        line = LINE_MARKER.sub("", line.strip(), count=1)
        m = YAML_QUOTED.match(line)
        if m:
            line = m.group(1).replace("''", "'") + (m.group(2) or "")
        m = NODE_LINE.match(line)
        if m:
            line = " ".join(x for x in (m.group(1), m.group(2)) if x)
        else:
            line = ROLE_PREFIX.sub("", line, count=1)
        out.append(line)
    return _normalised("\n".join(out))


def fragment_text(fragment):
    """A quote fragment as it is looked for: a node the model wrote as
    `role "name"` with no colon is its name, `main "dark-records/adhoc"` is
    `dark-records/adhoc`; otherwise `strip_roles` as for the page."""
    text = LINE_MARKER.sub("", str(fragment or "").strip().strip("'").strip(), count=1)
    text = NAMED_NODE.sub(lambda m: m.group(1) + " ", text, count=1)
    return _normalised(strip_roles(text))


def evidence_fragments(evidence):
    """The parts a pass verdict's quote is checked in, in order: the quote is
    split where a model joins separate page elements, an em dash or a hyphen
    between spaces, `; ` or `. `, and a part under `MIN_FRAGMENT` characters
    keeps the part beside it, because a short part alone is the model's own
    punctuation and not a quote. So `A — Nujabes - Feather` is two fragments,
    `A` and `Nujabes - Feather`."""
    text = str(evidence or "")
    parts, seps, pos = [], [], 0
    for m in FRAGMENT_SPLIT.finditer(text):
        parts.append(text[pos:m.start()])
        seps.append(m.group(0))
        pos = m.end()
    parts.append(text[pos:])
    groups = []  # [text, the separator that began it]
    for i, part in enumerate(parts):
        sep = seps[i - 1] if i else ""
        if groups and len(_normalised(groups[-1][0])) < MIN_FRAGMENT:
            groups[-1][0] += sep + part  # too short to stand alone: it stays
        else:
            groups.append([part, sep])
    if len(groups) > 1 and len(_normalised(groups[-1][0])) < MIN_FRAGMENT:
        tail = groups.pop()
        groups[-1][0] += tail[1] + tail[0]
    return [g[0].strip() for g in groups if g[0].strip()]


def first_missing_fragment(evidence, snapshot):
    """The first fragment of a pass verdict's quote that is not on the page,
    or None when every one is; a blank quote answers "", nothing to look for,
    which a caller reads as a refusal. The fragments have their role prefixes
    taken off too, so a quote of the snapshot's own format is found."""
    fragments = evidence_fragments(evidence)
    if not fragments:
        return ""
    page, names = page_text(snapshot), page_names(snapshot)
    for fragment in fragments:
        old = _normalised(strip_roles(fragment))
        new = fragment_text(fragment)
        if old not in page and new not in page and new not in names:
            return fragment
    return None


ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)*)-(\d{1,9})(?![A-Za-z0-9])")


def named_ids(text):
    """{stem: {number}} for every identifier of the form <stem>-<number> in text,
    as print jobs, orders and tickets are named (HL-L2400DWE-18, ORD-7)."""
    out = {}
    for stem, num in ID_RE.findall(str(text or "")):
        out.setdefault(stem, set()).add(num)
    return out


def id_drift(action, earlier):
    """None, or why a pass names another identifier than the one an earlier step
    named: an earlier note names exactly one <stem>-<n>, and this pass names
    <stem>-<m> but never <stem>-<n>. A later step that reports on "the job" must
    report on the same job, not the first one the page happens to list."""
    now = named_ids(f"{action.get('note', '')} {action.get('evidence', '')}")
    for rec in reversed(earlier or []):
        before = named_ids(rec.get("note"))
        for stem, nums in now.items():
            was = before.get(stem)
            if was and len(was) == 1 and not nums & was:
                old = next(iter(was))
                new = sorted(nums)[0]
                return (f"step {rec.get('step')} named {stem}-{old}, and this pass names {stem}-{new}: "
                        f"find {stem}-{old} on the page, or give fail or inconclusive")
    return None


def evidence_on_page(evidence, snapshot):
    """Whether a pass verdict's quote is really on the page: every fragment
    `first_missing_fragment` looks for is found. A missing or blank quote
    never counts."""
    return first_missing_fragment(evidence, snapshot) is None


def earlier_steps(results):
    """The finished steps as `step N (pass|fail): <note>` lines, or `none`
    when none has finished yet: the block the prompt shows so that a step
    which refers to something an earlier step made can read its note."""
    return "\n".join(f"step {r['step']} ({r['verdict']}): {r['note']}" for r in (results or [])) or "none"


def parse_expected(text):
    """(expected, why) from the judge's reply: the first JSON object with a
    non-blank string `expected`, or (None, why it was refused), so a malformed
    answer records no target and never changes the step."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, f"judge reply is not JSON: {(text or '')[:200]}"
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None, f"judge reply is not JSON: {(text or '')[:200]}"
    if not isinstance(d, dict) or not isinstance(d.get("expected"), str) or not d["expected"].strip():
        return None, f"judge reply names no expected target: {m.group(0)[:200]}"
    return d["expected"].strip()[:300], str(d.get("why") or "")[:300]


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

    def next_action(self, n, text, url, snapshot, history, earlier=None, timeout=None):
        """`timeout` is the time the run has left for this call; the call
        waits no longer than that, nor than the task's llm_timeout."""
        prompt = PROMPT.format(spec=self.t.get("spec", ""), n=n, total=len(self.t.get("steps") or []),
                               text=text, url=url, snapshot=shown_snapshot(snapshot),
                               earlier=earlier_steps(earlier),
                               history="\n".join(json.dumps(h, sort_keys=True) for h in history) or "(none)")
        content, finish, thought = self._complete(SYSTEM, prompt, timeout)
        action = parse_action(content)
        if action.get("do") == "invalid" and finish == "length":
            # the thinking used the whole token budget and no answer was written:
            # say so in the record, and to the model, instead of "not JSON"
            action.update(cut="length", reasoning_chars=thought)
        return action

    def expected_target(self, text, snapshot, timeout=None):
        """The judge's reply, as text: its own system prompt and only the
        step's text and the snapshot, so it never sees the acting model's
        messages. run_steps parses it with parse_expected. `timeout` as for
        next_action."""
        return self._complete(JUDGE_SYSTEM, JUDGE_PROMPT.format(text=text, snapshot=shown_snapshot(snapshot)),
                              timeout)[0]

    def _complete(self, system, prompt, timeout=None):
        """One chat completion: (the reply's content, its finish_reason, the
        characters of reasoning), the provider's token counts added to the
        session's; ModelError when the endpoint fails.
        The call waits no longer than `timeout` seconds when given, nor than
        the task's llm_timeout."""
        req = urllib.request.Request(
            f"{self.t['llm_url']}/chat/completions", method="POST",
            data=json.dumps(self.body([{"role": "system", "content": system},
                                       {"role": "user", "content": prompt}])).encode(),
            headers={"Content-Type": "application/json"})
        try:
            limit = int(self.t.get("llm_timeout") or 600)
            with urllib.request.urlopen(req, timeout=limit if timeout is None else min(limit, timeout)) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ModelError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ModelError(f"{type(e).__name__}: {e}") from None
        u = d.get("usage") or {}
        S.STATS["tokens_in"] += int(u.get("prompt_tokens") or 0)
        S.STATS["tokens_out"] += int(u.get("completion_tokens") or 0)
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        thought = len(msg.get("reasoning") or msg.get("reasoning_content") or "")
        S.STATS["reasoning_chars"] += thought
        return msg.get("content") or "", choice.get("finish_reason"), thought


# --- the browser ----------------------------------------------------------------
class AmbiguousTarget(Exception):
    """An action's name matched several elements and none exactly: the action
    is refused, never performed on the first of them."""


QUOTED_LINE = re.compile(r"""^-\s+(?:'((?:[^']|'')*)'|(.*))""")
ROLE_NAME = re.compile(r'^[a-z][a-z-]*\s+"((?:[^"\\]|\\.)*)"')
MAX_CANDIDATES = 8  # names an ambiguity refusal quotes; the rest are counted


def accessible_name(locator):
    """The accessible name of one element, from its own aria snapshot's first
    line (`- link "records: 1032-x"`, which Playwright writes as a quoted YAML
    scalar when the name holds `: `); its text when that line has no name."""
    try:
        first = (locator.aria_snapshot() or "").splitlines()[0].strip()
    except Exception:  # noqa: BLE001 — a name we cannot read is still a candidate
        first = ""
    m = QUOTED_LINE.match(first)
    body = (m.group(1).replace("''", "'") if m and m.group(1) is not None else
            m.group(2) if m else "")
    n = ROLE_NAME.match(body)
    if n:
        try:
            return json.loads(f'"{n.group(1)}"')
        except ValueError:
            return n.group(1)
    try:
        return " ".join((locator.inner_text() or "").split())
    except Exception:  # noqa: BLE001
        return ""


def resolve_target(page, role, name):
    """The element an action names. Playwright's `name` matches a substring,
    any case, so `.first` of it can be another element than the one named: the
    link `records: 1032-x` before the link `1032-x`. So an element whose name is
    exactly the one asked for wins (the first, when several are); a single
    element containing the name is taken; several containing it and none equal
    to it is an AmbiguousTarget naming them, so the model can name one exactly.
    No name: the first element of the role, as before."""
    if not name:
        return page.get_by_role(role).first
    loose = page.get_by_role(role, name=name)
    loose.first.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
    exact = page.get_by_role(role, name=name, exact=True)
    if exact.count():
        return exact.first
    n = loose.count()
    if n > 1:
        names = [accessible_name(loose.nth(i)) for i in range(min(n, MAX_CANDIDATES))]
        more = f" and {n - MAX_CANDIDATES} more" if n > MAX_CANDIDATES else ""
        raise AmbiguousTarget(
            f'refused: {n} {role} elements contain "{name}" and none is named exactly that: '
            + ", ".join(json.dumps(x, ensure_ascii=False) for x in names) + more
            + f"; ask again with one {role}'s exact name")
    return loose.first


def _origin(url):
    """scheme://host:port of a URL: the key the trail keeps requests under."""
    p = urllib.parse.urlsplit(str(url or ""))
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


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
        self._origin = None  # the URL first opened: the trail keeps requests to its origin
        self._pending = {}   # requests since the last drain, {id: {method, url, status}}
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
        self._pending = {}
        self.page = self._context.new_page()
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)

    def goto(self, url):
        if not self._origin:
            self._origin = _origin(url)
        try:
            self.page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"{url} did not open: {e}") from None

    def _same_origin(self, url):
        return bool(self._origin) and _origin(url) == self._origin

    def _on_request(self, request):
        if not self._same_origin(request.url):
            return
        self._pending[id(request)] = {"method": request.method, "url": request.url,
                                      "status": None, "request": request}

    def _on_response(self, response):
        if not self._same_origin(response.url):
            return
        request = getattr(response, "request", None)
        rec = self._pending.get(id(request)) if request is not None else None
        if rec is None:  # the response's request is another object than the event's
            rec = next((r for r in self._pending.values()
                        if r["status"] is None and r["url"] == response.url), None)
        if rec is None:
            rec = {"method": getattr(request, "method", "") if request is not None else "",
                   "url": response.url, "status": None, "request": request}
            self._pending[id(request) if request is not None else id(response)] = rec
        rec["status"] = response.status

    def drain_requests(self):
        """The requests the page issued since the last drain, in order, each
        {method, url, status}, the target's origin only. The first drain is
        empty; a request with no response yet keeps status None."""
        out = [{k: v for k, v in rec.items() if k != "request"} for rec in self._pending.values()]
        self._pending.clear()
        return out

    def url(self):
        return self.page.url

    def snapshot(self):
        try:
            return self.page.locator("body").aria_snapshot()
        except Exception:  # noqa: BLE001 — an older Playwright: the accessibility tree as JSON
            return json.dumps(self.page.accessibility.snapshot(), indent=1)

    def _target(self, a):
        return resolve_target(self.page, a.get("role") or "button", a.get("name") or None)

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


def action_target(action):
    """The thing an action acted on, as the trail's `target`: the locator of a
    click, fill or select, the address of a goto, the key of a press, the
    seconds of a wait. A stuck step's note names the same thing."""
    do = action.get("do")
    if do in ("click", "fill", "select"):
        return f'{action.get("role") or "button"} "{action.get("name") or ""}"'
    if do == "goto":
        return str(action.get("url") or "")
    if do == "press":
        return str(action.get("key") or "")
    if do == "wait":
        return f'{action.get("seconds") or 1}s'
    return json.dumps(action, sort_keys=True)


def _drain(page):
    """The requests the page issued since the last drain, or [] for a page
    that does not record them (the fakes most tests use)."""
    drain = getattr(page, "drain_requests", None)
    return drain() if drain else []


def _seen_errors(trail):
    """The distinct 4xx/5xx the step's actions provoked, in order, as
    `saw 502 GET /api/liked`, for the step's note."""
    seen, out = set(), []
    for line in trail:
        for r in line.get("requests") or []:
            if int(r.get("status") or 0) < 400:
                continue
            key = (r.get("status"), r.get("method"), r.get("url"))
            if key not in seen:
                seen.add(key)
                url = str(r.get("url") or "")
                out.append(f"saw {r.get('status')} {r.get('method')} {urllib.parse.urlsplit(url).path or url}")
    return out


def ask_judge(judge, n, text, page, stream, max_calls, deadline, clock):
    """{expected, expected_why} for step n, asked before its first action. The
    call counts against the calls and wall envelopes like the acting model's:
    `deadline` is the end of the time calls may use (run_steps passes its own
    deadline less the records reserve), the call is given only the time left
    before it. A spent envelope, a failed call or an answer that is not one
    JSON target gives `expected: null` with the reason, and nothing else about
    the step."""
    if S.STATS["calls"] >= max_calls:
        return {"expected": None, "expected_why": "not asked: the calls envelope was spent"}
    if deadline - clock() <= 0:
        return {"expected": None, "expected_why": "not asked: the wall envelope was spent"}
    snap = page.snapshot() or ""
    S.STATS["calls"] += 1
    S.STATS["requests"] += 1
    try:
        left = max(0.001, deadline - clock())  # the snapshot took some of it
        expected, why = parse_expected(judge.expected_target(text, snap, timeout=left))
    except ModelError as e:
        expected, why = None, f"judge call failed: {e}"[:300]
    rec = {"expected": expected, "expected_why": why}
    # no "action" key: the stream's readers take this call for no browser action
    _append(stream, {"step": n, "call": S.STATS["calls"], "url": page.url(), "snapshot_chars": len(snap),
                     "judge": rec})
    return rec


def run_steps(model, page, url, steps, records_dir, max_calls, deadline, clock=time.time, judge=None,
              reserve=RECORDS_RESERVE_SECONDS):
    """Open `url` and take every step in order. Returns (results, stop):
    results is one {step, verdict, note} per step, in order, a pass also
    carrying the evidence it was accepted on, as also appended to
    <records_dir>/steps.jsonl; stop is None, or "calls" /
    "seconds" when the envelope ran out. A step that ran out of calls,
    repeated one browser action on an unchanged snapshot, or had a pass
    verdict refused for the same reason three times on an unchanged
    snapshot, whatever its note said, is `inconclusive` with the
    reason in `note`; with a `judge` (an object with expected_target), each
    step's line also carries `expected` and `expected_why` from ask_judge,
    asked before the step's first action. The repeated-action count starts
    again whenever the snapshot changes, and a `wait`, which only passes time, never counts
    toward it, so a step may wait as long as its call budget allows; a spent
    calls envelope leaves the steps it did not
    reach inconclusive too, never failed. Each step that was taken leaves
    <records_dir>/steps/<NN>.png and its trail, one {t, action, target,
    requests} line per action, as <records_dir>/steps/<NN>.trail.jsonl. The
    transcript of every call goes to <records_dir>/stream.jsonl. The last
    `reserve` seconds before `deadline` are the records': no call starts
    inside them, each call is given only the time left before them, and a
    call that fails once that time is spent is the "seconds" stop. Any other
    ModelError, and BrowserError, propagate."""
    steps_dir = os.path.join(records_dir, "steps")
    os.makedirs(steps_dir, exist_ok=True)
    jsonl = os.path.join(records_dir, "steps.jsonl")
    stream = os.path.join(records_dir, "stream.jsonl")
    page.goto(url)
    results, stop = [], None
    for n, text in enumerate(steps, 1):
        if stop:
            rec = {"step": n, "verdict": "inconclusive" if stop == "calls" else "fail",
                   "note": f"not reached: the {stop} envelope was spent"}
            if judge is not None:
                rec.update(expected=None, expected_why=f"not asked: the {stop} envelope was spent")
            results.append(rec)
            _append(jsonl, rec)
            continue
        history, verdict, note, evidence = [], None, "", ""
        trail, pending, repeats, prev = [], None, 0, None
        refusals, refused_prev = 0, None  # the same refused pass verdict, an unchanged page
        step_start = clock()
        _drain(page)  # nothing the page did before this step belongs to it
        # the judge names where the result should appear before anything is done
        expected = ask_judge(judge, n, text, page, stream, max_calls, deadline - reserve, clock) if judge is not None else {}
        while verdict is None:
            if S.STATS["calls"] >= max_calls:
                stop, verdict, note = "calls", "inconclusive", "calls exhausted"
                break
            if deadline - reserve - clock() <= 0:  # no call starts inside the reserve
                stop, verdict, note = "seconds", "fail", "the wall envelope was spent before a verdict"
                break
            snap = page.snapshot() or ""
            if pending is not None:
                # what the last action set off, up to the snapshot before this call
                pending["requests"] = _drain(page)
                pending = None
            S.STATS["calls"] += 1
            S.STATS["requests"] += 1
            left = max(0.001, deadline - reserve - clock())  # the snapshot took some of it
            try:
                action = model.next_action(n, text, page.url(), snap, history, earlier=results, timeout=left)
            except ModelError:
                if deadline - reserve - clock() >= 1:
                    raise
                # the call ran out the time it was given: the envelope, not the model
                stop, verdict, note = "seconds", "fail", "the wall envelope was spent before a verdict"
                break
            is_wait = action.get("do") == "wait"  # a wait only passes time: it never counts as stuck
            key = json.dumps(action, sort_keys=True)
            entry = {"step": n, "call": S.STATS["calls"], "url": page.url(), "snapshot_chars": len(snap),
                     "action": action}
            if action.get("do") == "verdict":
                if action.get("verdict") not in ("pass", "fail", "inconclusive"):
                    history.append({"action": action, "error": "verdict must be pass, fail or inconclusive"})
                elif action.get("verdict") == "pass" and not evidence_on_page(action.get("evidence"),
                                                                              shown_snapshot(snap)):
                    # a pass without the page's own words is no pass: refuse it
                    # and ask again, which costs another call like any call; the
                    # refusal names the fragment the page did not show
                    missing = first_missing_fragment(action.get("evidence"), shown_snapshot(snap))
                    history.append({"action": action,
                                    "error": (f'evidence not found on the page: "{missing}"' if missing
                                              else "evidence not found on the page")})
                    # a verdict is not an action: it never feeds the stuck count,
                    # but the same refusal three times tells the model nothing new
                    # either, so the step ends naming the quote. The count keys on
                    # the page and the refusal (the fragment it could not find),
                    # not on the whole action: a model that rewords its note each
                    # time never reset it before (1070: 13 calls in one step)
                    refusals = refusals + 1 if refused_prev == (snap, history[-1]["error"]) else 1
                    refused_prev = (snap, history[-1]["error"])
                elif action.get("verdict") == "pass" and id_drift(action, results):
                    # the page's words, but about another job than the earlier step's:
                    # refused like a missing quote, and counted the same way
                    history.append({"action": action, "error": id_drift(action, results)})
                    refusals = refusals + 1 if refused_prev == (snap, history[-1]["error"]) else 1
                    refused_prev = (snap, history[-1]["error"])
                else:
                    verdict, note = action["verdict"], str(action.get("note") or "")[:300]
                    evidence = str(action.get("evidence") or "") if verdict == "pass" else ""
            elif action.get("do") == "invalid" and action.get("cut") == "length":
                history.append({"action": action, "error": "no reply: your thinking used the whole token "
                                                           "budget before any answer; think briefly and "
                                                           "reply with one JSON object"})
            elif action.get("do") == "invalid":
                history.append({"action": action, "error": "not one JSON action; reply with one JSON object"})
            else:
                # only a browser action can be stuck; a verdict in between
                # resets nothing and counts for nothing, and a wait never
                # counts at all, so a step may wait its whole call budget
                if not is_wait:
                    repeats = repeats + 1 if prev == (snap, key) else 1
                    prev = (snap, key)
                S.STATS["tool_calls"] += 1
                _drain(page)  # requests the model call itself made are no action's
                refused = False
                try:
                    page.act(action)
                    history.append({"action": action, "ok": True})
                except AmbiguousTarget as e:
                    # nothing was clicked: the model gets the candidates' names in full
                    refused = True
                    history.append({"action": action, "error": str(e)[:1200], "refused": True})
                except Exception as e:  # noqa: BLE001 — a failed action is evidence, not a crash
                    history.append({"action": action, "error": f"{type(e).__name__}: {e}"[:300]})
                trail.append({"t": round(clock() - step_start, 3), "action": action,
                              "target": action_target(action), "requests": []})
                if refused:
                    trail[-1]["refused"] = True
                pending = trail[-1]
            if verdict is None and not is_wait and repeats >= 3:
                # the same browser action on an unchanged page three times: no
                # new information can reach the model, so the step stays unjudged
                verdict, note = "inconclusive", f"stuck: {action_target(action)}"
            elif verdict is None and refusals >= 3:
                # the same refused pass verdict three times: end the step and
                # say what the model quoted, rather than call the verdict stuck
                drift = id_drift(action, results) if action.get("verdict") == "pass" else None
                verdict, note = "inconclusive", (f"refused three times: {drift}"[:300] if drift else
                                                 f"evidence not found: {str(action.get('evidence') or '')[:80]}")
            entry["result"] = history[-1] if history and verdict is None else {"verdict": verdict}
            _append(stream, entry)
        saw = _seen_errors(trail)
        if saw:
            note = "; ".join([note, *saw]) if note else "; ".join(saw)
        with open(os.path.join(steps_dir, f"{n:02d}{TRAIL_SUFFIX}"), "w") as f:
            for line in trail:
                f.write(S.scrub(json.dumps(line, sort_keys=True)) + "\n")
        page.screenshot(os.path.join(steps_dir, f"{n:02d}.png"))
        rec = {"step": n, "verdict": verdict, "note": note}
        if verdict == "pass":
            rec["evidence"] = evidence
        rec.update(expected)
        results.append(rec)
        _append(jsonl, rec)
        S.PROGRESS.add(f"step {n}: {verdict}")
    return results, stop


README_FILE = "README.md"
BADGE = {"pass": "🟢 PASS", "fail": "🔴 FAIL", "inconclusive": "🟡 INCONCLUSIVE"}
FILES_EXPLAINED = (
    ("steps.jsonl", "one line per step: its verdict, the model's note, for a pass the page text it quoted, "
                    "and `expected` and `expected_why`: where a separate judge said, before the step's first "
                    "action, the result should appear, and why (`expected` is null when the judge gave no answer)."),
    ("steps/NN.png", "the page at the moment step NN got its verdict."),
    ("steps/NN.trail.jsonl", "every browser action in step NN, with the requests the page made after it."),
    ("stream.jsonl", "every call to the model: the action it chose and what happened when it was done."),
    ("trace.zip", "the Playwright trace of the whole run: open it at https://trace.playwright.dev, or with "
                  "`npx playwright show-trace trace.zip`."),
    ("video.webm", "the whole run as a video; `dark demo <this directory>` cuts it into a captioned demo."),
    ("brief.md, task.json", "what the run was given: the task's text, URL, steps and model (tokens removed)."),
)


def _cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")


def write_readme(records_dir, outcome, detail):
    """<records>/README.md: what a person opening the run's folder reads first,
    rendered by the Git host: the outcome as a coloured badge, a table of the
    steps with their verdicts, and one line per file saying what it holds."""
    t = S.TASK
    steps = list(t.get("steps") or [])
    rows = []
    path = os.path.join(records_dir, "steps.jsonl")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    by_step = {r.get("step"): r for r in rows if isinstance(r, dict)}
    lines = [f"# {t.get('run') or 'user-arm run'}", "",
             f"**{BADGE.get(outcome, outcome.upper())}**: {_cell(detail)}", "",
             "A user-arm run of dark: a language model used a real browser on "
             f"<{t.get('url', '')}> step by step, the way a new user would, and gave each step a verdict: "
             "pass, fail, or inconclusive when it could not tell. "
             f"Task `{t.get('task', '')}`, model `{t.get('llm_model', '')}`.", "",
             "| step | verdict | what the model saw |", "|---|---|---|"]
    for i, text in enumerate(steps, 1):
        r = by_step.get(i)
        verdict = BADGE.get(r.get("verdict"), r.get("verdict")) if r else "not reached"
        lines.append(f"| {i}. {_cell(text)} | {verdict} | {_cell(r.get('note', '')) if r else ''} |")
    lines += ["", "## The files", ""]
    lines += [f"- `{name}`: {what}" for name, what in FILES_EXPLAINED]
    with open(os.path.join(records_dir, README_FILE), "w", encoding="utf-8") as f:
        f.write(S.scrub("\n".join(lines) + "\n"))


def records_paths(records_dir):
    """What the records push adds beside stream.jsonl, brief.md, task.json.
    README.md, trace.zip and video.webm are listed always; the push skips a
    path that does not exist, so a recording that was not made is not an error."""
    return {README_FILE: os.path.join(records_dir, README_FILE),
            "steps.jsonl": os.path.join(records_dir, "steps.jsonl"),
            "steps": os.path.join(records_dir, "steps"),
            TRACE_FILE: os.path.join(records_dir, TRACE_FILE),
            VIDEO_FILE: os.path.join(records_dir, VIDEO_FILE)}


def fail(kind, text, **kw):
    _readme("fail", f"({kind}) {text}")
    S.comment(f"AGENT-DONE fail ({kind}): {text}\n"
              + S.done("fail", kind, **kw, **S.records_kw(extra_paths=records_paths(S.RECORDS_DIR))))
    return 1


def inconclusive(text, **kw):
    """No step failed and at least one could not be judged: not a pass, and
    told apart from a fail in the comment and the tag."""
    _readme("inconclusive", text)
    S.comment(f"AGENT-DONE inconclusive: {text}\n"
              + S.done("inconclusive", **kw, **S.records_kw(extra_paths=records_paths(S.RECORDS_DIR))))
    return 1


def _readme(outcome, detail):
    """The README never stops the records push: a fault writing it is one stderr line."""
    try:
        if S.RECORDS_DIR and os.path.isdir(S.RECORDS_DIR):
            write_readme(S.RECORDS_DIR, outcome, detail)
    except Exception as e:  # noqa: BLE001
        print(f"README.md not written: {e}", file=sys.stderr)


def main(model=None, page=None, judge=None):
    try:
        return _main(model, page, judge)
    finally:
        S.PROGRESS.stop()


def _main(model, page, judge):
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
    if model is None:
        # one endpoint, two prompts: the judge's never carries the acting model's messages;
        # a model a test hands in brings its own judge or none
        model = ChatModel(t)
        judge = judge or model
    deadline = S.T0 + int(t.get("max_seconds") or 900)
    try:
        try:
            results, stop = run_steps(model, page, url, steps, records, max_calls, deadline, judge=judge)
        finally:
            page.close()  # before any fail() below pushes the records: this is what saves trace.zip and video.webm
    except BrowserError as e:
        return fail("env", str(e))
    except ModelError as e:
        return fail("llm", str(e))
    ok = sum(1 for r in results if r["verdict"] == "pass")
    failed = [r for r in results if r["verdict"] == "fail"]
    unjudged = [r for r in results if r["verdict"] == "inconclusive"]
    # the summary line keeps the inconclusive count apart from the failed one
    tally = f"{ok}/{len(steps)} steps pass"
    if unjudged:
        tally += f", {len(unjudged)} inconclusive"
    if failed:
        tally += f", {len(failed)} failed"
    kw = {"steps_ok": ok, "steps_total": len(steps)}
    if stop == "seconds":
        return fail("seconds", f"wall envelope spent at {tally}", **kw)
    if stop == "calls" and not unjudged:  # a calls stop always leaves inconclusive steps
        return fail("calls", f"call envelope ({max_calls}) spent at {tally}", **kw)
    if failed:
        reasons = "; ".join(f"step {r['step']}: {r['note']}" for r in failed)
        return fail("steps", f"{len(failed)} of {len(steps)} steps failed ({tally}): {reasons}", **kw)
    if unjudged:
        reasons = "; ".join(f"step {r['step']}: {r['note']}" for r in unjudged)
        return inconclusive(f"{tally}; no step failed, but {len(unjudged)} could not be judged: {reasons}", **kw)
    _readme("pass", tally)
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
