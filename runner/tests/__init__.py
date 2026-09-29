"""The test package.

Git needs an e-mail for every reflog entry it writes (a clone, a push, a
commit). With no user.email and no EMAIL it builds one from the host name,
and that lookup goes to DNS: on a runner with the host's network and a
resolver that times out, each git call waits about 10 s, the agent misses its
4 s heartbeat and the suite fails an hour later. EMAIL is git's own default,
below user.email and GIT_*_EMAIL, so a test that sets an identity still
gets the one it set.
"""
import os

os.environ.setdefault("EMAIL", "tests@dark.invalid")
