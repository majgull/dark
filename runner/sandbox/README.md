# dark sandbox image

The image a docker-backed sandbox is created from (`runner/dark/docker.py`,
configured as `sandbox_image` in `runner/host.toml`). It is debian stable
slim plus python3, git, ca-certificates and the Go toolchain, which is what
the task templates' `verify.sh` needs. The container runs as root, like the
VM template.

Build it from the repository root:

    docker build -t dark-sandbox runner/sandbox
