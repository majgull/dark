# The image a docker-backed sandbox is created from. debian stable slim plus
# the tools a task template's verify.sh needs: python3 for the python
# template, git and the Go toolchain for the go template (the task set has Go
# tasks). The container runs as root, like the VM template.
#
# Build it:
#   docker build -t dark-sandbox runner/sandbox
FROM debian:stable-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates golang-go \
    && rm -rf /var/lib/apt/lists/*
