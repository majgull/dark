"""dark/pressure.py - release pressure: count the Unreleased changelog entries.

Merged work should ship as a patch release soon after, not wait for a large
one (specs/cadence requirement 4). The gate runs this over CHANGELOG.md:
more than WARN_ABOVE entries under `## [Unreleased]` prints a warning and
goes on, more than FAIL_ABOVE fails the gate.

    python3 -m dark.pressure <path/to/CHANGELOG.md>

An entry is a line starting `- ` between `## [Unreleased]` and the next
`## [` heading, whatever `### ` subsection it sits under.
"""
import sys

WARN_ABOVE = 5
FAIL_ABOVE = 10


def unreleased_entries(text):
    """Number of `- ` lines in the `## [Unreleased]` section of `text`."""
    inside = False
    count = 0
    for line in text.splitlines():
        if line.startswith("## ["):
            if inside:
                break
            inside = line.startswith("## [Unreleased]")
        elif inside and line.startswith("- "):
            count += 1
    return count


def verdict(count):
    """(exit code, message or None) for `count` Unreleased entries."""
    if count > FAIL_ABOVE:
        return 1, (f"RELEASE PRESSURE FAIL: {count} Unreleased changelog entries, "
                   f"over {FAIL_ABOVE}; cut a patch release")
    if count > WARN_ABOVE:
        return 0, (f"release pressure warning: {count} Unreleased changelog entries, "
                   f"over {WARN_ABOVE}; a patch release is due")
    return 0, None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m dark.pressure <CHANGELOG.md>", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        code, message = verdict(unreleased_entries(f.read()))
    if message:
        print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
