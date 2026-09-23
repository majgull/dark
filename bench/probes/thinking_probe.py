import json, sys, time, urllib.request
GATE = "http://127.0.0.1:11434/v1/chat/completions"
PROMPT = "Write a python function is_iso_week(s) that returns True only for strings of exactly four ASCII digits, a hyphen, an upper-case W and two ASCII digits. Reply with the code only."
def call(model, api, level, stream):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 4000, "temperature": 0}
    if api == "reasoning_effort": body["reasoning_effort"] = level
    else: body["chat_template_kwargs"] = {"enable_thinking": level != "none"}
    if stream: body["stream"] = True; body["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(GATE, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read().decode()
    dt = time.time() - t
    if not stream:
        d = json.loads(raw); m = d["choices"][0]["message"]
        return {"model": model, "api": api, "level": level, "stream": False, "seconds": round(dt, 1),
                "usage": d.get("usage"), "reasoning_chars": len(m.get("reasoning") or m.get("reasoning_content") or ""),
                "content_chars": len(m.get("content") or ""), "msg_keys": sorted(m.keys()), "finish": d["choices"][0].get("finish_reason")}
    usage, rc, cc, keys = None, 0, 0, set()
    for line in raw.splitlines():
        if not line.startswith("data:") or line.strip() == "data: [DONE]": continue
        c = json.loads(line[5:])
        if c.get("usage"): usage = c["usage"]
        for ch in c.get("choices") or []:
            d = ch.get("delta") or {}; keys |= set(d.keys())
            rc += len(d.get("reasoning") or d.get("reasoning_content") or ""); cc += len(d.get("content") or "")
    return {"model": model, "api": api, "level": level, "stream": True, "seconds": round(dt, 1), "usage": usage,
            "reasoning_chars": rc, "content_chars": cc, "delta_keys": sorted(keys)}
for model in sys.argv[1].split(","):
    for level in ("none", "low", "medium"):
        for stream in (False, True):
            try: print(json.dumps(call(model, sys.argv[2], level, stream)))
            except Exception as e: print(json.dumps({"model": model, "level": level, "stream": stream, "error": str(e)[:300]}))
        sys.stdout.flush()
