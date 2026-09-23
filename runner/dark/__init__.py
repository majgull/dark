"""dark — the v2 runner (hub thread 949).

A task with a class, a spec and hidden acceptance goes through preflight,
execute (in a throwaway VM), verify, push, stage (in a fresh VM) and lands as
exactly one outcome in a JSONL ledger. Every bound is data (models.toml,
budgets.toml); admission of a tier to a class is derived from the ledger.
Stdlib only, Python 3.11+.
"""
