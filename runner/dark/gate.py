"""dark/gate.py - the local-model host's keep-awake lease.

The gate that fronts the local models (the provider with `wake = true` in
models.toml) also powers the LLM VM and the Proxmox host down when no
model request has arrived for a while. It knows nothing about the runner's
own VMs on that host, so it can power the host down under running executor
and staging VMs when the cloud tiers are the only ones talking to a model.
The gate offers a lease for exactly this
(`POST /gate/hold {"seconds": N}`: neither tier powers anything down while
it is held; it expires on its own). Every run and every staging takes one
long enough to cover its own envelope plus the staging timeout and a
margin; nothing releases early, the lease simply lapses after the last VM.
"""

import json
import urllib.error
import urllib.request

MARGIN_SECONDS = 900  # VM spawn, clone, reap, and the next run's start


def control_base(catalog):
    """The gate's control URL root (its provider url without the /v1), or
    None when no provider wakes anything."""
    for p in catalog.providers.values():
        if p.wake:
            url = p.url.rstrip("/")
            return url[:-3] if url.endswith("/v1") else url
    return None


def hold(base, seconds, timeout=5):
    """Take (or extend) the keep-awake lease for `seconds`. (ok, detail)."""
    req = urllib.request.Request(f"{base}/gate/hold", method="POST",
                                 data=json.dumps({"seconds": int(seconds)}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        return True, f"held {body.get('hold_seconds_remaining', '?')}s"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(e)[:200]
