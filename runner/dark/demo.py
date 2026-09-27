#!/usr/bin/env python3
"""dark demo - a demo video of one user-arm run, cut from its own records, that
a person can watch without reading anything else.

The user arm leaves, in <records>: task.json (the task's name, spec, URL and
numbered steps), stream.jsonl (every browser action the model chose, with its
step), steps.jsonl (one verdict and note per step), trace.zip (the Playwright
trace, whose events carry the time of every action and the point of every
click) and video.webm (the page, 1280x720, from the moment it was created).
This command puts them together as <records>/demo.mp4:

- a title card: the task's name, what it asks, the URL and the model;
- the run itself, with the current step's text in a band above the page, each
  action as a caption in a band below it (the page is never covered) ("click link 'Printers'"), a ring where
  the page was clicked, and a failed action named as failed;
- at each step's verdict the frame is held, long enough to read the verdict
  and its note (and to hear them, with --voice);
- an end card: every step's verdict and note, and the outcome.

Times come from the trace: step k ends at the k-th screenshot (the arm takes
one per verdict, steps/<NN>.png), and the step's actions from stream.jsonl
are laid in order on the trace's action events between that screenshot and
the one before. A run recorded before the trace existed, or a trace that does
not match the stream, still gets a demo: its actions are spread evenly over
the step and the command says so on standard error.

Rendering needs ffmpeg with libass and libx264; --voice needs piper and one
of its .onnx voices. Stdlib only otherwise, and every step before the ffmpeg
call (`load`, `timeline`, `subtitles`) is a plain function the tests drive.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
import zipfile

W, H, FPS = 1280, 720, 25  # user.VIEWPORT, and the rate Playwright records at
TITLE_S, END_S = 5.0, 7.0  # seconds of the two cards
HOLD_S = 3.5               # the least time a verdict's frame is held
TOP, BOTTOM = 56, 96       # the dark bands above and below the page: the captions never cover it
CH = H + TOP + BOTTOM      # the cut's height
TAIL_S = 1.0               # the run's last frame, held before the end card
DEMO_FILE = "demo.mp4"

# the trace method each browser action of PlaywrightPage.act shows up as
METHODS = {"click": ("click",), "fill": ("fill",), "select": ("selectOption",),
           "press": ("keyboardPress", "press"), "goto": ("goto",),
           "wait": ("waitForTimeout",)}
ACTION_METHODS = {m for ms in METHODS.values() for m in ms}
VERDICT_COLOUR = {"pass": "&H0050C878", "fail": "&H004040E0", "inconclusive": "&H0000B4F0"}
VERDICT_MARK = {"pass": "PASS", "fail": "FAIL", "inconclusive": "INCONCLUSIVE"}


class DemoError(Exception):
    pass


def _jsonl(path):
    rows = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    return rows


def _trace_events(path):
    if not os.path.isfile(path):
        return []
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".trace")]
            if not names:
                return []
            raw = z.read(names[0]).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return []
    out = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def load(records):
    """The run's records as one dict; a missing video or verdict list is an error."""
    task_path = os.path.join(records, "task.json")
    task = {}
    if os.path.isfile(task_path):
        with open(task_path, encoding="utf-8") as f:
            task = json.load(f)
    verdicts = _jsonl(os.path.join(records, "steps.jsonl"))
    if not verdicts:
        raise DemoError(f"{records}: no steps.jsonl rows")
    return {"task": task, "stream": _jsonl(os.path.join(records, "stream.jsonl")),
            "verdicts": verdicts, "trace": _trace_events(os.path.join(records, "trace.zip"))}


def describe(action):
    """One short line for a browser action, as a caption shows it."""
    do = action.get("do")
    name = action.get("name") or ""
    role = action.get("role") or ""
    target = f"{role} '{name}'".strip() if name else role
    if do == "click":
        return f"click {target}"
    if do == "fill":
        return f"type '{action.get('text', '')}' into {target}"
    if do == "select":
        return f"choose '{action.get('value', '')}' in {target}"
    if do == "press":
        return f"press {action.get('key') or 'Enter'}"
    if do == "goto":
        return f"go to {action.get('url', '')}"
    if do == "wait":
        return f"wait {action.get('seconds', 1)} s"
    return str(do)


def timeline(rec, video_s=None):
    """Steps with their start and end, their actions and their verdict, on the
    video's clock (seconds from the page's creation). Returns (steps, exact):
    exact is False when the times were spread evenly instead of read."""
    ev = rec["trace"]
    before = [e for e in ev if e.get("type") == "before"]
    errors = {e.get("callId") for e in ev if e.get("type") == "after" and e.get("error")}
    points = {e.get("callId"): e.get("point") for e in ev if e.get("type") == "input" and e.get("point")}
    new_page = next((e for e in before if e.get("method") == "newPage"), None)
    shots = [e for e in before if e.get("method") == "screenshot"]
    verdicts = rec["verdicts"]
    exact = bool(new_page) and len(shots) >= len(verdicts)
    by_step = {}
    for a in rec["stream"]:
        act = a.get("action") or {}
        if act.get("do") in ("verdict", "invalid", None):
            continue
        by_step.setdefault(a.get("step"), []).append(a)

    steps = []
    if exact:
        t0 = new_page["startTime"]
        sec = lambda ms: max(0.0, (ms - t0) / 1000.0)
        gotos = [e for e in before if e.get("method") == "goto"]
        start = gotos[0]["startTime"] if gotos else t0
        acts = [e for e in before if e.get("method") in ACTION_METHODS and e is not (gotos[0] if gotos else None)]
        for k, v in enumerate(verdicts):
            end = shots[k]["startTime"]
            window = [e for e in acts if start <= e["startTime"] < end]
            chosen = by_step.get(v.get("step"), [])
            actions = []
            wi = 0
            for a in chosen:
                want = METHODS.get((a.get("action") or {}).get("do"), ())
                j = next((i for i in range(wi, len(window)) if window[i].get("method") in want), None)
                if j is None:
                    exact = False
                    continue
                e = window[j]
                wi = j + 1
                failed = e.get("callId") in errors or bool((a.get("result") or {}).get("error"))
                actions.append({"t": sec(e["startTime"]), "text": describe(a["action"]),
                                "failed": failed, "point": points.get(e.get("callId"))})
            steps.append({"n": v.get("step", k + 1), "start": sec(start), "end": sec(end),
                          "actions": actions, "verdict": v.get("verdict", "inconclusive"),
                          "note": v.get("note") or ""})
            start = end
        if all(len(s["actions"]) == len(by_step.get(s["n"], [])) for s in steps):
            return steps, True
    # no usable trace: each step gets an equal share of the video, its actions spread over it
    total = video_s or 4.0 * len(verdicts)
    share = total / len(verdicts)
    steps = []
    for k, v in enumerate(verdicts):
        s0, s1 = k * share, (k + 1) * share
        chosen = by_step.get(v.get("step"), [])
        gap = (s1 - s0) / (len(chosen) + 1)
        steps.append({"n": v.get("step", k + 1), "start": s0, "end": s1,
                      "actions": [{"t": s0 + (i + 1) * gap * 0.9, "text": describe(a["action"]),
                                   "failed": bool((a.get("result") or {}).get("error")), "point": None}
                                  for i, a in enumerate(chosen)],
                      "verdict": v.get("verdict", "inconclusive"), "note": v.get("note") or ""})
    return steps, False


def outcome(steps):
    vs = [s["verdict"] for s in steps]
    if vs and all(v == "pass" for v in vs):
        return "pass"
    return "fail" if "fail" in vs else "inconclusive"


def _ts(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(text):
    text = " ".join(str(text).replace("\\", "/").split())
    return text.replace("{", "(").replace("}", ")")


def _ring(x, y, r=22):
    """An ASS vector drawing of a ring of radius r centred on (x, y)."""
    k = round(r * 0.5523, 1)  # the bezier handle for a quarter circle
    def circle(rr, kk):
        return (f"m 0 {-rr} b {kk} {-rr} {rr} {-kk} {rr} 0 b {rr} {kk} {kk} {rr} 0 {rr} "
                f"b {-kk} {rr} {-rr} {kk} {-rr} 0 b {-rr} {-kk} {-kk} {-rr} 0 {-rr}")
    return (f"{{\\an7\\pos({x:.0f},{y:.0f})\\bord0\\shad0\\1c&H0000D7FF&\\1a&H30&\\p1}}"
            f"{circle(r, k)} {circle(r - 5, round((r - 5) * 0.5523, 1))}{{\\p0}}")


HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {CH}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Banner,DejaVu Sans,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,8,40,40,14,1
Style: Action,DejaVu Sans,30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,60,60,30,1
Style: Verdict,DejaVu Sans,30,&H00FFFFFF,&H00FFFFFF,&H20000000,&H20000000,0,0,0,0,100,100,0,0,3,14,0,2,60,60,30,1
Style: Kicker,DejaVu Sans,24,&H00B0B0B0,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,0,0,7,90,90,90,1
Style: Title,DejaVu Sans,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,90,90,140,1
Style: Body,DejaVu Sans,28,&H00E0E0E0,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,90,90,240,1
Style: Ring,DejaVu Sans,20,&H0000D7FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def plan_holds(steps, voice_s=None):
    """The seconds the frame is held at each step's verdict: HOLD_S, or the
    spoken verdict's length plus a breath when that is longer."""
    voice_s = voice_s or {}
    return [max(HOLD_S, voice_s.get(i, 0.0) + 0.6) for i in range(len(steps))]


def subtitles(rec, steps, holds, video_s, title_s=TITLE_S, end_s=END_S):
    """The whole ASS script for the cut: title card, the run with its held
    verdict frames, end card. Returns (text, total seconds)."""
    task = rec["task"]
    def at(t):  # the run's clock to the cut's clock
        return title_s + t + sum(h for s, h in zip(steps, holds) if s["end"] < t - 1e-6)
    lines = []
    def dia(start, end, style, text, layer=0):
        if end > start:
            lines.append(f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}")

    n = len(steps)
    run_s = video_s + sum(holds) + TAIL_S
    total = title_s + run_s + end_s
    # title card
    name = task.get("task") or task.get("run") or "user-arm run"
    dia(0, title_s, "Kicker", "DARK  ·  USER-ARM RUN")
    dia(0, title_s, "Title", _esc(name))
    body = [_esc(task.get("spec", ""))]
    if task.get("url"):
        body.append("")
        body.append(f"Page: {_esc(task['url'])}")
    if task.get("llm_model"):
        body.append(f"Model: {_esc(task['llm_model'])}  ·  {n} steps")
    dia(0, title_s, "Body", "\\N".join(body))
    # the run
    step_texts = task.get("steps") or []
    for i, s in enumerate(steps):
        text = step_texts[s["n"] - 1] if isinstance(s["n"], int) and 0 < s["n"] <= len(step_texts) else ""
        banner = f"Step {s['n']} of {n}" + (f":  {_esc(text)}" if text else "")
        b0 = at(s["start"]) + holds[i - 1] if i else title_s  # after the step before has been read
        dia(b0, at(s["end"]) + holds[i], "Banner", banner)
        for j, a in enumerate(s["actions"]):
            nxt = s["actions"][j + 1]["t"] if j + 1 < len(s["actions"]) else s["end"]
            a0 = at(a["t"])
            a1 = max(a0 + 1.2, min(at(nxt), a0 + 4.0))
            if a["failed"]:
                dia(a0, a1, "Action", f"{{\\1c&H006060FF&}}{_esc(a['text'])}  (did not work, trying again)")
            else:
                dia(a0, a1, "Action", _esc(a["text"]))
            p = a.get("point")
            if p and not a["failed"]:
                dia(a0, a0 + 0.8, "Ring", _ring(p.get("x", 0), p.get("y", 0) + TOP), layer=1)
        v0 = at(s["end"])
        colour = VERDICT_COLOUR.get(s["verdict"], VERDICT_COLOUR["inconclusive"])
        mark = VERDICT_MARK.get(s["verdict"], s["verdict"].upper())
        dia(v0, v0 + holds[i], "Verdict",
            f"{{\\3c{colour}&\\4c{colour}&\\b1}}{mark}{{\\b0}}   {_esc(s['note'])}", layer=2)
    # end card
    e0 = title_s + run_s
    out = outcome(steps)
    colour = VERDICT_COLOUR[out]
    passed = sum(1 for s in steps if s["verdict"] == "pass")
    dia(e0, total, "Kicker", "RESULT")
    dia(e0, total, "Title", f"{{\\1c{colour}&}}{VERDICT_MARK[out]}{{\\1c&H00FFFFFF&}}   {passed} of {n} steps pass")
    rows = []
    for s in steps:
        c = VERDICT_COLOUR.get(s["verdict"], VERDICT_COLOUR["inconclusive"])
        rows.append(f"{{\\1c{c}&\\b1}}{VERDICT_MARK.get(s['verdict'], s['verdict'])}{{\\b0\\1c&H00E0E0E0&}}  "
                    f"step {s['n']}: {_esc(s['note'])}")
    dia(e0, total, "Body", "\\N".join(rows))
    return HEADER + "\n".join(lines) + "\n", total


def speech(rec, steps):
    """What --voice says: the task at the title card, each verdict while its
    frame is held, the outcome at the end card."""
    task = rec["task"]
    title = (task.get("task") or "A user-arm run") + ". " + " ".join(str(task.get("spec", "")).split())
    verdicts = [f"Step {s['n']}, {s['verdict']}. {s['note']}" for s in steps]
    passed = sum(1 for s in steps if s["verdict"] == "pass")
    end = f"Result: {outcome(steps)}. {passed} of {len(steps)} steps pass."
    return title, verdicts, end


def _probe_s(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        raise DemoError(f"{path}: ffprobe could not read a duration: {p.stderr.strip()[:200]}")


def _say(piper, voice, text, out):
    p = subprocess.run([piper, "--model", voice, "--output_file", out], input=text,
                       capture_output=True, text=True)
    if p.returncode != 0 or not os.path.isfile(out):
        raise DemoError(f"piper failed rc={p.returncode}: {p.stderr.strip()[-200:]}")
    with wave.open(out) as w:
        return w.getnframes() / float(w.getframerate())


def render(records, out=None, voice=None, piper="piper", keep=False):
    """Write <records>/demo.mp4 (or `out`); returns (path, seconds, exact)."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise DemoError(f"{tool} not found: the demo needs ffmpeg with libass and libx264")
    video = os.path.join(records, "video.webm")
    if not os.path.isfile(video):
        raise DemoError(f"{records}: no video.webm (the run was recorded without a video)")
    rec = load(records)
    video_s = _probe_s(video)
    steps, exact = timeline(rec, video_s)
    for s in steps:  # a trace that runs past the video is cut to it
        s["end"] = min(s["end"], video_s)
        s["start"] = min(s["start"], s["end"])
        for a in s["actions"]:
            a["t"] = min(a["t"], s["end"])
    if not exact:
        print("demo: the trace does not match the actions; times are spread evenly", file=sys.stderr)
    out = os.path.abspath(out or os.path.join(records, DEMO_FILE))
    tmp = tempfile.mkdtemp(prefix="dark-demo-")
    try:
        clips = []  # (wav, start seconds on the cut's clock)
        voice_s = {}
        if voice:
            if not shutil.which(piper) and not os.path.isfile(piper):
                raise DemoError(f"{piper} not found: --voice needs piper")
            title, verdicts, end = speech(rec, steps)
            t_s = _say(piper, voice, title, os.path.join(tmp, "title.wav"))
            for i, text in enumerate(verdicts):
                voice_s[i] = _say(piper, voice, text, os.path.join(tmp, f"v{i}.wav"))
            e_s = _say(piper, voice, end, os.path.join(tmp, "end.wav"))
        title_s = max(TITLE_S, t_s + 0.8) if voice else TITLE_S
        end_s = max(END_S, e_s + 1.0) if voice else END_S
        holds = plan_holds(steps, voice_s)
        ass, total = subtitles(rec, steps, holds, video_s, title_s, end_s)
        with open(os.path.join(tmp, "demo.ass"), "w", encoding="utf-8") as f:
            f.write(ass)
        if voice:
            clips.append(("title.wav", 0.3))
            done = 0.0
            for i, s in enumerate(steps):
                clips.append((f"v{i}.wav", title_s + s["end"] + done + 0.2))
                done += holds[i]
            clips.append(("end.wav", title_s + video_s + sum(holds) + TAIL_S + 0.4))

        # the run cut at every verdict, each piece ending on a held frame
        cuts = [0.0] + [s["end"] for s in steps] + [video_s]
        n = len(cuts) - 1
        pads = holds + [TAIL_S]
        card = f"color=c=0x14181f:s={W}x{CH}:r={FPS}"
        inputs = ["-i", video, "-f", "lavfi", "-t", f"{title_s:.2f}", "-i", card,
                  "-f", "lavfi", "-t", f"{end_s:.2f}", "-i", card]
        for wav, _ in clips:
            inputs += ["-i", wav]
        fc = [f"[0:v]fps={FPS},scale={W}:{H},setsar=1,format=yuv420p,pad={W}:{CH}:0:{TOP}:color=0x14181f,split={n}" + "".join(f"[s{i}]" for i in range(n))]
        for i in range(n):
            fc.append(f"[s{i}]trim=start={cuts[i]:.3f}:end={max(cuts[i + 1], cuts[i] + 0.04):.3f},"
                      f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={pads[i]:.2f}[p{i}]")
        fc.append("[1:v]format=yuv420p,setsar=1[t]")
        fc.append("[2:v]format=yuv420p,setsar=1[e]")
        fc.append("[t]" + "".join(f"[p{i}]" for i in range(n)) + f"[e]concat=n={n + 2}:v=1:a=0[cat]")
        fc.append("[cat]ass=demo.ass[v]")
        maps = ["-map", "[v]"]
        if clips:
            for k, (_, at) in enumerate(clips):
                ms = int(at * 1000)
                fc.append(f"[{3 + k}:a]aresample=48000,adelay={ms}|{ms},apad[a{k}]")
            fc.append("".join(f"[a{k}]" for k in range(len(clips)))
                      + f"amix=inputs={len(clips)}:normalize=0,atrim=0:{total:.2f}[a]")
            maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
        cmd = (["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc)] + maps
               + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", "-t", f"{total:.2f}", out])
        p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if p.returncode != 0:
            raise DemoError(f"ffmpeg failed rc={p.returncode}: {p.stderr.strip()[-400:]}")
        return out, total, exact
    finally:
        if keep:
            print(f"demo: work files kept in {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dark demo", description=__doc__.split("\n\n")[0])
    ap.add_argument("records", help="a user-arm run's records directory (video.webm, trace.zip, steps.jsonl, ...)")
    ap.add_argument("--out", help=f"where the video goes (default <records>/{DEMO_FILE})")
    ap.add_argument("--voice", help="a piper .onnx voice: the task, each verdict and the outcome are spoken")
    ap.add_argument("--piper", default=os.environ.get("PIPER", "piper"), help="the piper binary (default $PIPER, else piper)")
    ap.add_argument("--keep", action="store_true", help="keep the subtitle and audio work files")
    args = ap.parse_args(argv)
    try:
        path, total, exact = render(args.records, args.out, args.voice, args.piper, args.keep)
    except DemoError as e:
        print(f"demo: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"demo": path, "seconds": round(total, 1), "timed_from_trace": exact}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
