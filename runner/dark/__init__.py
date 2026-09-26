"""dark - an agent runner for isolated coding tasks.

A task with a class, a spec and hidden acceptance goes through preflight,
execute (in a throwaway VM), verify, push, stage (in a fresh VM) and lands as
exactly one outcome in a JSONL ledger. Every bound is data (models.toml,
budgets.toml); admission of a tier to a class is derived from the ledger.
Stdlib only, Python 3.11+.
"""

import importlib.metadata
import os
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))


def _installed():
    """The installed distribution's version, or None when dark is not
    installed (the deployed VM may run the package from a checkout)."""
    try:
        return importlib.metadata.version("dark")
    except importlib.metadata.PackageNotFoundError:
        return None


def _checkout():
    """The `version` of the `[project]` named dark in the nearest
    pyproject.toml, walking up from the package directory; None when there is
    none."""
    d = _HERE
    while True:
        try:
            with open(os.path.join(d, "pyproject.toml"), "rb") as f:
                meta = tomllib.load(f).get("project", {})
            if meta.get("name") == "dark" and isinstance(meta.get("version"), str):
                return meta["version"]
        except (OSError, tomllib.TOMLDecodeError):
            pass
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def version():
    """dark's version: the `version` line of the checkout's pyproject.toml
    when the package runs from a checkout, else the installed distribution's,
    else "unknown" - never an exception. The checkout wins because an older
    `pip install` on the same machine would otherwise name a release the
    running code is not."""
    return _checkout() or _installed() or "unknown"


__version__ = version()
