"""The repository holds code and configuration, never evidence.

Run logs, launcher logs, recordings and images belong to a dataset that a
study pins by revision and hash. This test keeps them out of git without a
hook: it fails on any tracked file over the size limit or of a banned kind.
"""
import subprocess
from pathlib import Path

import pytest

LIMIT = 200 * 1024
# A file of an evidence kind may be a small fixture or probe sample; a ledger
# day is megabytes. The kind alone is not the offence, the kind at size is.
KIND_LIMIT = 20 * 1024
BANNED = {".jsonl", ".ndjson", ".log", ".cast", ".png", ".jpg", ".jpeg", ".gif",
          ".pdf", ".zip", ".tar", ".gz", ".xz", ".zst", ".mp4", ".webm"}


def tracked_files():
    here = Path(__file__).resolve().parent
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                             capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(["git", "ls-files", "-z"], cwd=top,
                             capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")
    root = Path(top)
    return root, [root / p.decode() for p in out.split(b"\0") if p]


def test_no_tracked_file_is_evidence():
    root, files = tracked_files()
    bad = []
    for path in files:
        rel = path.relative_to(root)
        if not path.is_file():
            continue
        size = path.stat().st_size
        if path.suffix.lower() in BANNED and size > KIND_LIMIT:
            bad.append(f"{rel}: {size} bytes of {path.suffix}, over {KIND_LIMIT} for that kind")
        elif size > LIMIT:
            bad.append(f"{rel}: {size} bytes over {LIMIT}")
    assert not bad, "evidence in git:\n" + "\n".join(bad)
