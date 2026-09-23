"""dark/notify.py — one ntfy line, best-effort (a push must never kill a shift)."""

import urllib.request


def push(url, text, title=None, log=print):
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="POST", data=text.encode(),
                                     headers={"Title": title or "dark"})
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:  # noqa: BLE001
        log(f"ntfy failed: {e}")
        return False
