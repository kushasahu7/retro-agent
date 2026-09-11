"""Shell command classification.

This is where the worst bug in the project lived: every redirect counted as a
file edit, so `2>/dev/null` produced a file called "null" that appeared to have
been edited 77 times, and Claude Code's edit count inflated from 26 to 156.
"""
import unittest

import helpers  # noqa: F401  (puts the repo on sys.path)
from retro_agent.adapters import classify_shell


class Verification(unittest.TestCase):
    def test_test_runners_are_verification(self):
        for cmd in ("pytest tests/ -q", "npm test", "npm run lint", "yarn test",
                    "go test ./...", "cargo clippy", "ruff check .", "mypy src",
                    "tsc --noEmit", "jest --ci", "make test",
                    "python -m pytest tests/test_x.py", "git diff", "git status"):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify_shell(cmd)[0], "verify")

    def test_verification_wins_over_a_redirect(self):
        # A test run piped to a file is still a test run, not an edit.
        kind, _ = classify_shell("pytest -q > results.txt")
        self.assertEqual(kind, "verify")


class Edits(unittest.TestCase):
    def test_apply_patch_names_its_target(self):
        kind, target = classify_shell(
            "apply_patch <<'EOF'\n*** Update File: worker/ingest.py\nEOF")
        self.assertEqual(kind, "edit")
        self.assertEqual(target, "worker/ingest.py")

    def test_sed_in_place_is_an_edit(self):
        kind, target = classify_shell("sed -i '' 's/a/b/' src/app.ts")
        self.assertEqual(kind, "edit")
        self.assertEqual(target, "src/app.ts")

    def test_redirect_to_a_real_file_is_an_edit(self):
        kind, target = classify_shell("echo hi > src/config.json")
        self.assertEqual(kind, "edit")
        self.assertEqual(target, "src/config.json")

    def test_tee_is_an_edit(self):
        kind, target = classify_shell("echo hi | tee src/out.txt")
        self.assertEqual(kind, "edit")
        self.assertEqual(target, "src/out.txt")


class RedirectsThatAreNotEdits(unittest.TestCase):
    """Regression: these all used to be classified as file edits."""

    def test_dev_null_is_not_an_edit(self):
        for cmd in ("docker compose up 2>/dev/null",
                    "ls > /dev/null",
                    "make build >/dev/null 2>&1"):
            with self.subTest(cmd=cmd):
                self.assertNotEqual(classify_shell(cmd)[0], "edit")

    def test_stderr_merge_is_not_an_edit(self):
        self.assertNotEqual(classify_shell("./run.sh 2>&1")[0], "edit")

    def test_extensionless_redirect_target_is_not_an_edit(self):
        # `> now` produced a phantom file called "now".
        self.assertNotEqual(classify_shell("date > now")[0], "edit")

    def test_comparison_inside_code_is_not_an_edit(self):
        # A python one-liner containing `ts>1e11` produced a file called "1e11".
        cmd = "python3 -c \"print(1 if ts>1e11 else 0)\""
        self.assertNotEqual(classify_shell(cmd)[0], "edit")

    def test_no_edit_target_is_ever_a_dev_path(self):
        for cmd in ("cmd > /dev/stdout", "cmd >> /dev/null"):
            with self.subTest(cmd=cmd):
                kind, target = classify_shell(cmd)
                if kind == "edit":
                    self.assertFalse(str(target).startswith("/dev/"))


class Reads(unittest.TestCase):
    def test_readers_are_reads(self):
        for cmd in ("cat src/app.ts", "head -20 README.md", "rg --files",
                    "ls -la", "wc -l src/*.py", "jq . package.json"):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify_shell(cmd)[0], "read")

    def test_sed_print_is_a_read_not_an_edit(self):
        self.assertEqual(classify_shell("sed -n '1,80p' src/app.ts")[0], "read")


class Wrappers(unittest.TestCase):
    def test_bash_lc_wrapper_is_stripped(self):
        # Codex wraps everything as `bash -lc "..."`.
        self.assertEqual(classify_shell('bash -lc "pytest -q"')[0], "verify")
        kind, target = classify_shell(
            "bash -lc \"apply_patch <<'EOF'\n*** Update File: a/b.py\nEOF\"")
        self.assertEqual((kind, target), ("edit", "a/b.py"))

    def test_empty_and_none_are_safe(self):
        self.assertEqual(classify_shell("")[0], "shell")
        self.assertEqual(classify_shell(None)[0], "shell")

    def test_unknown_command_is_plain_shell(self):
        self.assertEqual(classify_shell("docker compose up worker")[0], "shell")


if __name__ == "__main__":
    unittest.main()
