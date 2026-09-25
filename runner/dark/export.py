"""dark/export.py - one dark task as a Terminal-Bench task directory.

Terminal-Bench is a public benchmark whose task directory holds `task.yaml`
(the `instruction:` block and fields such as `parser_name`,
`max_agent_timeout_sec` and `max_test_timeout_sec`), a `Dockerfile` (the
environment the agent works in), `docker-compose.yaml`, `run-tests.sh` (how
the hidden tests are run) and `solution.sh` (the reference solution).

`python3 -m dark export-harbor <task-dir> <out-dir>` writes those, plus a
`tests/` directory holding the hidden acceptance, for one dark task. The
instruction is the task's `spec`; the Dockerfile builds on the same base
image `runner/sandbox/Dockerfile` uses and copies `start/` into the working
directory, where `start/` is the task's full starting tree: the language
template, the tree an earlier chain step leaves, and the task's own `start/`,
built by the same `dark/tasks.py` functions a run uses and committed as one
git commit inside the image; `run-tests.sh` runs the acceptance the way
`dark/stager.py` does (copy into `.acceptance/`, run `run.sh` from the
working directory); `solution.sh` applies the `oracle/` overlay onto the
working directory as the image's own user, never the uid of the packing
host. A task with no `oracle/` still exports: its `solution.sh` says NOT
AVAILABLE and exits 1.

Only the standard library is used, so exporting never needs a dependency.
"""

import base64
import io
import os
import re
import stat
import sys
import tarfile
import tomllib

from . import tasks as dark_tasks

# The task directory must hold these; a missing one is refused before
# anything is written. `start/` is optional: `dark/tasks.py` builds the
# starting tree from the language template plus whatever overlay a task has.
REQUIRED = ("task.toml", "acceptance")

# The templates live beside the runner in this checkout, resolved the way
# `dark/tasks.py` and `bench/tools/check_tasks.py` resolve theirs.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = os.path.join(REPO, "templates")

# runner/sandbox/Dockerfile builds from this image, and the exported
# Dockerfile stays on it so a task runs on the base a dark sandbox uses.
BASE_IMAGE = "debian:stable-slim"
SANDBOX_PACKAGES = "python3 git ca-certificates golang-go libatomic1"

# dark holds a run's work and its hidden acceptance under the one
# stage_timeout (ten minutes unless a task sets its own), so both
# Terminal-Bench timeout fields take the task's value: the agent's turn and
# the tests are each bounded by the same dark timeout.
DEFAULT_TIMEOUT = 600

WORKDIR = "/app"
SOLUTION_MISSING = """#!/bin/bash
# Exported from dark task {name}: the dark task has no oracle/, so there is
# no reference solution to apply.
echo "NOT AVAILABLE: the dark task has no oracle/"
exit 1
"""


class ExportError(Exception):
    """One line, naming the file or directory at fault."""


def _read_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise ExportError(f"{path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ExportError(f"{path}: not valid TOML: {e}") from None


def _read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def parser_name(task_dir):
    """The Terminal-Bench parser this task's acceptance output needs.

    Terminal-Bench ships five parsers: pytest, swebench, swelancer, mlebench
    and sweperf. Only pytest is generic; the other four read one specific
    benchmark's report. A Python acceptance (an `acceptance/run.sh` that
    calls python) therefore gets pytest, and a Go acceptance gets pytest too
    because Terminal-Bench has no Go parser: dark's `CHECK <name> ok|fail`
    lines are read by none of the five, and the run's exit code is the
    verdict either way. Harbor itself ignores parser_name and grades on the
    exit code, so the value never changes a Harbor run.
    """
    run_sh = os.path.join(task_dir, "acceptance", "run.sh")
    if os.path.isfile(run_sh) and re.search(r"\bpython3?\b", _read_text(run_sh)):
        return "pytest"
    return "pytest"


def _needs_quotes(text):
    """True when a block scalar cannot carry the instruction verbatim. A
    carriage return is a line break in YAML, so a bare one inside the text
    would end the block early; those specs need a quoted scalar, where \\r
    is an escape."""
    return any(ch < " " and ch not in "\n\t" for ch in text)


def _quoted(text):
    """The instruction as one YAML double-quoted scalar."""
    out = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))
    return f'"{out}"'


def task_yaml(name, instruction, parser, timeout):
    """The task.yaml text. PyYAML is not a dependency, so the block scalar
    is written by hand: every instruction line indented two spaces, which is
    what the `instruction: |` block means. The `2` is an explicit
    indentation indicator, so an instruction that starts with indented lines
    still parses."""
    if _needs_quotes(instruction):
        field = f"instruction: {_quoted(instruction)}\n"
    else:
        body = "\n".join("  " + line if line else "" for line in instruction.split("\n"))
        field = f"instruction: |2\n{body}\n"
    return (f"# task: {name}\n"
            f"{field}"
            f"parser_name: {parser}\n"
            f"max_agent_timeout_sec: {timeout}\n"
            f"max_test_timeout_sec: {timeout}\n")


def dockerfile(name):
    """The image the agent works in: the dark sandbox's base and packages,
    the full starting tree copied into the working directory, and that tree
    committed as one git commit, the root commit an acceptance may read."""
    return (f"# Exported from dark task {name}. The base image and packages are the\n"
            f"# ones runner/sandbox/Dockerfile uses; start/ holds the starting tree\n"
            f"# dark/tasks.py built: the language template, the tree an earlier\n"
            f"# chain step leaves, and the task's own start/, as one git commit.\n"
            f"FROM {BASE_IMAGE}\n\n"
            f"RUN apt-get update \\\n"
            f"    && apt-get install -y --no-install-recommends {SANDBOX_PACKAGES} \\\n"
            f"    && rm -rf /var/lib/apt/lists/*\n\n"
            f"WORKDIR {WORKDIR}\n\n"
            f"COPY start/ {WORKDIR}/\n\n"
            f"RUN git init -q -b main \\\n"
            f"    && git add -A -f \\\n"
            f"    && git -c user.name=dark-runner -c user.email=dark-runner@localhost \\\n"
            f"       commit -q -m 'starting tree'\n")


def docker_compose(name):
    """The shape the fetched Terminal-Bench task uses: one client service
    that builds the Dockerfile and stays up for the harness."""
    return (f"# Exported from dark task {name}, in the shape Terminal-Bench uses.\n"
            f"services:\n"
            f"  client:\n"
            f"    build:\n"
            f"      dockerfile: Dockerfile\n"
            f"    image: ${{T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}}\n"
            f"    container_name: ${{T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}}\n"
            f'    command: [ "sh", "-c", "sleep infinity" ]\n'
            f"    environment:\n"
            f"      - TEST_DIR=${{T_BENCH_TEST_DIR}}\n"
            f"    volumes:\n"
            f"      - ${{T_BENCH_TASK_LOGS_PATH}}:${{T_BENCH_CONTAINER_LOGS_PATH}}\n"
            f"      - ${{T_BENCH_TASK_AGENT_LOGS_PATH}}:${{T_BENCH_CONTAINER_AGENT_LOGS_PATH}}\n")


def run_tests(name):
    """How the hidden acceptance is run, the way stager.py runs it: the
    tests/ files are copied to .acceptance/ and run.sh runs from the working
    directory. Harbor sets TEST_DIR to /tests; outside Harbor it defaults to
    ./tests beside the unpacked task."""
    return (f"#!/bin/bash\n"
            f"# Exported from dark task {name}. Copies the hidden acceptance into\n"
            f"# .acceptance/ and runs it from the working directory, exactly as\n"
            f"# runner/dark/stager.py does.\n"
            f'if [ -z "${{TEST_DIR:-}}" ]; then\n'
            f'    TEST_DIR="./tests"\n'
            f"fi\n"
            f'work="${{WORKDIR:-{WORKDIR}}}"\n'
            f'cd "$work" || exit 1\n'
            f"rm -rf .acceptance\n"
            f'cp -r "$TEST_DIR" .acceptance || exit 1\n'
            f"bash .acceptance/run.sh\n")


def solution_script(name, oracle_dir):
    """The oracle overlay as one self-contained script: Harbor uploads only
    solution.sh into the container, so the oracle files ride inside it as a
    base64 tar, extracted with --no-same-owner so the image's own user owns
    the result and never the uid of the packing host. The tar is laid over
    the working directory the way dark/tasks.py overlays a work tree:
    .delete lists paths removed first."""
    if not os.path.isdir(oracle_dir) or not os.listdir(oracle_dir):
        return SOLUTION_MISSING.format(name=name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(oracle_dir, arcname=".")
    payload = base64.encodebytes(buf.getvalue()).decode()
    return (f"#!/bin/bash\n"
            f"# Exported from dark task {name}. Applies the dark oracle/ overlay\n"
            f"# onto the working directory the way dark/tasks.py overlays a work\n"
            f"# tree: .delete lists paths removed first, then the files are copied.\n"
            f"set -e\n"
            f'work="${{WORKDIR:-{WORKDIR}}}"\n'
            f'tmp="$(mktemp -d)"\n'
            f'base64 -d >"$tmp/oracle.tar.gz" <<\'DARK_ORACLE_B64\'\n'
            f"{payload}"
            f"DARK_ORACLE_B64\n"
            f'tar -xzf "$tmp/oracle.tar.gz" -C "$tmp" --no-same-owner\n'
            f'rm -f "$tmp/oracle.tar.gz"\n'
            f'if [ -f "$tmp/.delete" ]; then\n'
            f'    while IFS= read -r line; do\n'
            f"        case \"$line\" in\n"
            f"            ''|'#'*) continue ;;\n"
            f"        esac\n"
            f'        path="${{line%/}}"\n'
            f'        case "$path" in\n'
            f"            /*|*..*) continue ;;\n"
            f"        esac\n"
            f'        rm -rf -- "$work/$path"\n'
            f'    done < "$tmp/.delete"\n'
            f'    rm -f "$tmp/.delete"\n'
            f"fi\n"
            f'cp -a "$tmp/." "$work/"\n'
            f'rm -rf "$tmp"\n')


def _start_tree(task_dir):
    """The full starting tree for the exported image, as {path: bytes}: the
    same `dark/tasks.py` call a run makes, so the export carries the language
    template, the oracle tree of the step this task follows, and the task's
    own start/."""
    task = dark_tasks.load_task(task_dir)
    base = None
    if task.after:
        earlier = dark_tasks.load_task(os.path.join(os.path.dirname(task.dir), task.after))
        base = dark_tasks.oracle_tree(earlier, TEMPLATES)
    return dark_tasks.work_tree(task, TEMPLATES, base_tree=base)


def _tree_files(root, mode_default):
    """[(relative path, bytes, mode)] of every file under root, keeping the
    executable bit. A symlink is refused: the task set is not model-written,
    but a link would make the export read outside the task directory."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                raise ExportError(f"{p}: a symlink in the task directory is refused")
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            with open(p, "rb") as f:
                data = f.read()
            src_mode = os.stat(p).st_mode
            mode = 0o755 if src_mode & stat.S_IXUSR else mode_default
            out.append((rel, data, mode))
    return out


def export(task_dir, out_dir):
    """Write the Terminal-Bench layout for one dark task and return out_dir.
    Refuses (ExportError, nothing written) when the task directory lacks
    task.toml or acceptance/, or when dark/tasks.py cannot build the
    starting tree."""
    task_dir = os.path.abspath(task_dir)
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(task_dir):
        raise ExportError(f"{task_dir}: not a directory")
    missing = [n for n in REQUIRED
               if not (os.path.isfile(os.path.join(task_dir, n)) if n.endswith(".toml")
                       else os.path.isdir(os.path.join(task_dir, n)))]
    if missing:
        raise ExportError(f"{task_dir}: missing {', '.join(missing)}")

    d = _read_toml(os.path.join(task_dir, "task.toml"))
    name = str(d.get("id") or os.path.basename(task_dir))
    instruction = d.get("spec")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ExportError(f"{os.path.join(task_dir, 'task.toml')}: spec must be a non-empty string")
    timeout = d.get("stage_timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ExportError(f"{os.path.join(task_dir, 'task.toml')}: stage_timeout must be a positive integer")

    # Everything below reads the task directory; the write happens after, so
    # a refusal leaves the output directory untouched.
    try:
        tree = _start_tree(task_dir)
    except dark_tasks.TaskError as e:
        raise ExportError(str(e)) from None
    files = [
        ("task.yaml", task_yaml(name, instruction, parser_name(task_dir), timeout).encode(), 0o644),
        ("Dockerfile", dockerfile(name).encode(), 0o644),
        ("docker-compose.yaml", docker_compose(name).encode(), 0o644),
        ("run-tests.sh", run_tests(name).encode(), 0o755),
        ("solution.sh", solution_script(name, os.path.join(task_dir, "oracle")).encode(), 0o755),
    ]
    for rel, data in sorted(tree.items()):
        # dark/tasks.py's write_tree keeps the executable bit on a .sh file
        # and writes every other file 0o644; the image's commit needs the
        # same modes the runner would have written.
        mode = 0o755 if rel.endswith(".sh") else 0o644
        files.append((os.path.join("start", rel), data, mode))
    for rel, data, mode in _tree_files(os.path.join(task_dir, "acceptance"), 0o644):
        files.append((os.path.join("tests", rel), data, mode))

    os.makedirs(out_dir, exist_ok=True)
    for rel, data, mode in files:
        p = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        os.chmod(p, mode)
    return out_dir


def cli(task_dir, out_dir):
    """The `dark export-harbor` entry point: 0 on success, 2 on refusal."""
    try:
        written = export(task_dir, out_dir)
    except ExportError as e:
        print(f"export-harbor: {e}", file=sys.stderr)
        return 2
    print(f"export-harbor: {task_dir} -> {written}")
    return 0
