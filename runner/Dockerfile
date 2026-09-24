# The runner container: debian stable slim plus the tools the runner shells
# out to. The package imports only the standard library, so it runs from
# source; the `dark` command is a thin wrapper that runs the package, whose
# config directory is --conf, else $DARK_CONF, else the toml files beside it.
# The docker CLI talks to the host daemon through the socket compose mounts in.
#
# Build from the repository root:
#   docker build -f runner/Dockerfile -t dark-runner .
FROM debian:stable-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 git ca-certificates curl docker-cli \
    && rm -rf /var/lib/apt/lists/*

COPY runner /root/dark-runner/runner
COPY templates /root/dark-runner/templates
COPY bench /root/dark-runner/bench
COPY pyproject.toml README.md LICENSE /root/dark-runner/

RUN printf '#!/bin/sh\nPYTHONPATH=/root/dark-runner/runner exec python3 -m dark "$@"\n' \
      > /usr/local/bin/dark \
    && chmod 0755 /usr/local/bin/dark

WORKDIR /root/dark-runner/runner
CMD ["sleep", "infinity"]
