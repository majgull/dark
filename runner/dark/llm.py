"""dark/llm.py - the control plane's model client, the calls the runner
makes itself outside a task VM. OpenAI-compatible chat, served-model
listing, and the failure taxonomy the runner classifies from. The executor
inside the VM has its own copy of the chat call (agent.py is injected
alone); this one is for preflight probes and the spec/review/validator
classes.
"""

import json
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    """kind: "timeout" | "http" | "rate" | "conn" | "shape"."""

    def __init__(self, kind, detail):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def get_json(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise LLMError("http", f"GET {url} -> {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError("conn", f"GET {url}: {getattr(e, 'reason', e)}") from None
    except json.JSONDecodeError:
        raise LLMError("shape", f"GET {url}: not JSON") from None


def served_models(provider, timeout=20):
    """Ids the provider serves right now, from its catalog or health URL."""
    if provider.catalog:
        d = get_json(f"{provider.url}/{provider.catalog}", timeout)
        return {m.get("id") for m in d.get("data") or [] if m.get("id")}
    if provider.health:
        d = get_json(provider.health, timeout)
        if d.get("key") is False:
            raise LLMError("conn", f"{provider.name}: no token behind the proxy")
        return set(d.get("models") or [])
    raise LLMError("shape", f"provider {provider.name}: neither catalog nor health URL")


def chat(url, model, messages, max_tokens, timeout, temperature=None,
         rate_signature=None):
    """One chat completion. Returns (content, usage) where usage has
    tokens_in, tokens_out, reasoning_chars, seconds, finish_reason."""
    body = {"model": model, "messages": messages, "max_tokens": int(max_tokens)}
    if temperature is not None:
        body["temperature"] = temperature
    req = urllib.request.Request(
        f"{url}/chat/completions", method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read().decode(errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        if e.code == 429 or (rate_signature and rate_signature in f"{e.code} {text}"):
            raise LLMError("rate", f"{model}: {e.code} {text}") from None
        raise LLMError("http", f"{model}: {e.code} {text}") from None
    except TimeoutError:
        raise LLMError("timeout", f"{model}: no reply in {timeout}s") from None
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            raise LLMError("timeout", f"{model}: no reply in {timeout}s") from None
        raise LLMError("conn", f"{model}: {e.reason}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise LLMError("conn", f"{model}: {e}") from None
    try:
        choice = d["choices"][0]
        m = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("shape", f"{model}: reply without choices[0].message") from None
    usage = d.get("usage") or {}
    return m.get("content") or "", {
        "tokens_in": usage.get("prompt_tokens") or 0,
        "tokens_out": usage.get("completion_tokens") or 0,
        "reasoning_chars": len(m.get("reasoning") or m.get("reasoning_content") or ""),
        "seconds": round(time.monotonic() - t0, 1),
        "finish_reason": choice.get("finish_reason"),
    }
