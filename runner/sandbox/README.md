# dark sandbox image

The image a docker-backed sandbox is created from (`runner/dark/docker.py`,
configured as `sandbox_image` in `runner/host.toml`). It is debian stable
slim plus python3, git, ca-certificates and the Go toolchain, which is what
the task templates' `verify.sh` needs. The container runs as root, like the
VM template.

Build it from the repository root:

    docker build -t dark-sandbox runner/sandbox

## browser image

`Dockerfile.browser` builds the image a user-arm sandbox is created from
(`runner/dark/user.py`, configured as `browser_image` in `runner/host.toml`):
the sandbox image above plus Chromium from apt and Playwright for Python,
both pinned. The Chromium pin is an apt version passed at build time; the
file's header says how to read it. Build it after the sandbox image:

    docker build -t dark-sandbox-browser -f runner/sandbox/Dockerfile.browser \
      --build-arg CHROMIUM_VERSION=<version> runner/sandbox

The VM backend has no browser template yet; the way to give it one is the
`vm-runtime` tarball `session.fetch_runtime` already unpacks, and that is out
of scope here.
