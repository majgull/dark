# The image a docker-backed sandbox is created from. debian stable slim plus
# the tools a task template's verify.sh needs: python3 for the python
# template, git and the Go toolchain for the go template (the task set has Go
# tasks), and libatomic1, which the session arm's node (the vm-runtime
# tarball, unpacked at run time) links against. The container runs as root,
# like the VM template. The stager's diff checks need two more: gitleaks (a
# pinned release, checked against its published sha256) and diff-cover (a
# pinned version in its own venv). A claude:<model-id> tier runs the pinned
# @anthropic-ai/claude-code below in place of pi (dark/session.py); pi's own
# runtime keeps coming from the vm-runtime tarball, not this image.
#
# Build it:
#   docker build -t dark-sandbox runner/sandbox
FROM debian:stable-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates golang-go libatomic1 \
                          nodejs npm \
    && rm -rf /var/lib/apt/lists/*

ARG GITLEAKS_VERSION=8.30.1
ARG GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ARG DIFF_COVER_VERSION=10.6.0
# checked against the tarball registry.npmjs.org serves for this exact
# version (npm's own integrity check re-verifies it against the registry's
# signed metadata at install time)
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CLAUDE_CODE_SHA256=6560abb7c2d71de8cdd164676a2386930e96f12c8c9c7e33b7548d2fbd734a92
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl python3-venv \
    && f="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    && curl -sSfL -o "/tmp/$f" "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/$f" \
    && echo "${GITLEAKS_SHA256}  /tmp/$f" | sha256sum -c - \
    && tar -xzf "/tmp/$f" -C /usr/local/bin gitleaks \
    && rm "/tmp/$f" \
    && python3 -m venv /opt/diff-cover \
    && /opt/diff-cover/bin/pip install --no-cache-dir "diff-cover==${DIFF_COVER_VERSION}" \
    && ln -s /opt/diff-cover/bin/diff-cover /usr/local/bin/diff-cover \
    && t="claude-code-${CLAUDE_CODE_VERSION}.tgz" \
    && curl -sSfL -o "/tmp/$t" "https://registry.npmjs.org/@anthropic-ai/claude-code/-/$t" \
    && echo "${CLAUDE_CODE_SHA256}  /tmp/$t" | sha256sum -c - \
    && npm install -g "/tmp/$t" \
    && rm "/tmp/$t" \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
