# The image a docker-backed sandbox is created from. debian stable slim plus
# the tools a task template's verify.sh needs: python3 for the python
# template, git and the Go toolchain for the go template (the task set has Go
# tasks), and libatomic1, which the session arm's node (the vm-runtime
# tarball, unpacked at run time) links against. The container runs as root,
# like the VM template.
#
# Build it:
#   docker build -t dark-sandbox runner/sandbox
FROM debian:stable-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates golang-go libatomic1 \
    && rm -rf /var/lib/apt/lists/*
