"""The webapp template's own gate: a fresh copy installs and passes its one
check, and each way a change drifts makes `npm run check` fail.

`npm run check` is the template's only definition of done. This test proves the
scaffold is green on a fresh copy and not merely by description: it copies
templates/webapp to a scratch directory, runs `npm ci` once, and then breaks one
thing at a time in that copy, restoring the file after each case.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DARK = HERE.parent.parent
TEMPLATE = DARK / "templates" / "webapp"


def tool(name):
    """Whether a program is on PATH, without running it."""
    return shutil.which(name) is not None


class Package(unittest.TestCase):
    """Requirement 1's file test, needing no install."""

    def test_versions_are_exact_and_the_lock_file_is_committed(self):
        package = json.loads((TEMPLATE / "package.json").read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, version in package.get(section, {}).items():
                self.assertNotIn("^", version, f"{name} carries a range")
                self.assertNotIn("~", version, f"{name} carries a range")
        self.assertTrue((TEMPLATE / "package-lock.json").is_file(), "no lock file")


@unittest.skipUnless(tool("npm") and tool("node"), "node and npm are needed")
class Check(unittest.TestCase):
    """The scaffold is green fresh, and each drift makes `check` non-zero."""

    scratch = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dark-webapp-")
        cls.scratch = os.path.join(cls.tmp, "app")
        shutil.copytree(
            TEMPLATE,
            cls.scratch,
            ignore=shutil.ignore_patterns("node_modules", "dist", ".git"),
        )
        done = cls.shell(["npm", "ci", "--no-audit", "--no-fund"], 900)
        if done.returncode != 0:
            raise unittest.SkipTest(f"npm ci failed:\n{done.stdout}\n{done.stderr}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def shell(cls, args, timeout=1200):
        return subprocess.run(
            args,
            cwd=cls.scratch,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def check(self):
        return self.shell(["npm", "run", "check"])

    def prove(self, marker, what):
        """Run `check` and require it to fail for the named reason."""
        done = self.check()
        output = done.stdout + done.stderr
        self.assertNotEqual(done.returncode, 0, f"{what} broke nothing:\n{output}")
        self.assertIn(marker, output, f"{what} failed without {marker!r}:\n{output}")

    def restore(self, path, content):
        def write_back():
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)

        self.addCleanup(write_back)

    def breaks(self, relative, addition, marker):
        """Append `addition` to `relative`, prove `check` fails, then restore."""
        path = os.path.join(self.scratch, relative)
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        self.restore(path, original)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(addition)
        self.prove(marker, relative)

    def replaces(self, relative, old, new, marker):
        """Replace `old` with `new` in `relative`, prove `check` fails, then restore."""
        path = os.path.join(self.scratch, relative)
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        self.assertIn(old, original, f"{relative} does not carry {old!r}")
        self.restore(path, original)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original.replace(old, new))
        self.prove(marker, relative)

    def breaks_new(self, relative, content, marker):
        """Add the new file `relative`, prove `check` fails, then delete it."""
        path = os.path.join(self.scratch, relative)
        self.assertFalse(os.path.exists(path), f"{relative} already exists")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        self.prove(marker, relative)

    def test_fresh_copy_passes_the_check(self):
        done = self.check()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_type_error_fails_the_check(self):
        self.breaks("src/store.ts", '\nexport const broken: number = "x";\n', "TS")

    def test_lint_error_fails_the_check(self):
        self.breaks(
            "src/store.ts",
            "\nexport function broken(): void {\n  debugger;\n}\n",
            "lint/",
        )

    def test_failing_unit_test_fails_the_check(self):
        self.breaks(
            "src/store.test.ts",
            '\nit("deliberately fails", () => {\n  expect(1).toBe(2);\n});\n',
            "deliberately fails",
        )

    def test_raw_colour_fails_the_check(self):
        self.breaks(
            "src/store.ts",
            '\nexport const broken = { color: "#7c3aed" };\n',
            "outside tokens.css",
        )

    def test_a_panel_import_outside_the_shell_fails(self):
        self.breaks_new(
            "src/leak.ts",
            'import { Notes } from "./panels/Notes";\nexport const leaked = Notes;\n',
            "outside the shell",
        )

    def test_ignoring_the_test_role_fails(self):
        self.replaces(
            "src/role.tsx",
            "if (!isTest) return null;",
            "if (isTest) return null;",
            "a test build reads the role query",
        )

    def test_playwright_in_check_fails(self):
        self.replaces(
            "package.json",
            '"check": "tsc --noEmit && biome check . && vitest run && node tools/check-tokens.mjs && vite build"',
            '"check": "tsc --noEmit && biome check . && vitest run && node tools/check-tokens.mjs && vite build && playwright test"',
            "must not run playwright",
        )

    def test_unmarked_wall_clock_timing_fails(self):
        self.breaks(
            "src/validate.test.ts",
            '\nit("measures wall time", () => {\n  const at = Date.now();\n  expect(at).toBeGreaterThan(0);\n});\n',
            "without the marker timing",
        )


if __name__ == "__main__":
    unittest.main()
