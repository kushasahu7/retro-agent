"""End-to-end CLI behaviour, run as a subprocess against a sandboxed corpus.

Every store path is redirected, so these tests never read or write the real
~/.claude, ~/.codex or Cursor state, and never touch the archive in the checkout.
"""
import gzip, json, os, shutil, subprocess, sys, tempfile, unittest, uuid

import helpers
from helpers import claude_session, codex_session, codex_meta, codex_shell

REPO = helpers.REPO
RETRO_MODULE = "retro_agent.cli"


class Sandbox(unittest.TestCase):
    """A throwaway corpus plus a fully redirected environment."""

    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="retro-test-")
        self.projects = os.path.join(self.box, "projects")
        self.codex = os.path.join(self.box, "codex", "sessions")
        self.claude = os.path.join(self.box, "claude")
        os.makedirs(self.claude)

        self.session = claude_session(
            os.path.join(self.projects, "-home-dev-acme", f"{uuid.uuid4()}.jsonl"),
            [("user", "make the hero responsive"),
             ("tool", ("Edit", {"file_path": "/home/dev/acme/src/Hero.jsx"}, False)),
             ("tool", ("Edit", {"file_path": "/home/dev/acme/src/Hero.jsx"}, False)),
             ("tool", ("Edit", {"file_path": "/home/dev/acme/src/Hero.jsx"}, False)),
             ("tool", ("Bash", {"command": "docker compose up"}, True)),
             ("tool", ("Bash", {"command": "docker compose up"}, True))],
            title="Make the hero responsive")

        codex_session(
            os.path.join(self.codex, "2026", "09", "02",
                         f"rollout-2026-09-02T10-00-00-{uuid.uuid4()}.jsonl"),
            [codex_meta()] + codex_shell("c1", "pytest -q", 0, 1))

        # A prompt history file, so `heatmap --metric prompts` has data.
        with open(os.path.join(self.claude, "history.jsonl"), "w") as fh:
            fh.write(json.dumps({"display": "make the hero responsive",
                                 "pastedContents": {}, "project": "/home/dev/acme",
                                 "sessionId": "s1",
                                 "timestamp": int(helpers.BASE.timestamp() * 1000)}) + "\n")

        self.env = dict(os.environ,
                        RETRO_PROJECTS=self.projects,
                        RETRO_CODEX=self.codex,
                        RETRO_CURSOR_DB=os.path.join(self.box, "absent.vscdb"),
                        RETRO_ARCHIVE=os.path.join(self.box, "arc"),
                        RETRO_DB=os.path.join(self.box, "retro.db"),
                        RETRO_CLAUDE=self.claude)
        self.env.pop("RETRO_PASSPHRASE", None)

    def tearDown(self):
        shutil.rmtree(self.box, ignore_errors=True)

    def run_retro(self, *args):
        env = dict(self.env)
        env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run([sys.executable, "-m", RETRO_MODULE, *args],
                              capture_output=True, text=True, env=env, cwd=REPO)

    def consent(self):
        r = self.run_retro("consent", "--accept")
        self.assertEqual(r.returncode, 0, r.stderr)

    def archived_files(self):
        out = []
        for dirpath, _, files in os.walk(os.path.join(self.box, "arc")):
            out += [os.path.join(dirpath, f) for f in files if f.endswith(".gz")]
        return out


class ConsentGate(Sandbox):
    def test_archive_refuses_without_consent(self):
        r = self.run_retro("archive")
        self.assertIn("PERMANENT", r.stdout)
        self.assertEqual(self.archived_files(), [])

    def test_archive_proceeds_once_consent_is_recorded(self):
        self.consent()
        r = self.run_retro("archive")
        self.assertIn("archived", r.stdout)
        self.assertEqual(len(self.archived_files()), 2)

    def test_revoking_consent_stops_archiving_again(self):
        self.consent()
        self.run_retro("archive")
        self.run_retro("consent", "--revoke")
        r = self.run_retro("archive")
        self.assertIn("PERMANENT", r.stdout)


class Archiving(Sandbox):
    def setUp(self):
        super().setUp()
        self.consent()

    def test_rerunning_changes_nothing(self):
        self.run_retro("archive")
        r = self.run_retro("archive")
        self.assertIn("0 new", r.stdout)
        self.assertIn("unchanged", r.stdout)

    def test_a_grown_session_is_re_snapshotted(self):
        self.run_retro("archive")
        with open(self.session, "a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": helpers._ts(99),
                                 "message": {"role": "user", "content": "one more"}}) + "\n")
        r = self.run_retro("archive")
        self.assertIn("1 updated", r.stdout)

    def test_a_deleted_source_session_is_reported_as_rescued(self):
        self.run_retro("archive")
        os.remove(self.session)
        r = self.run_retro("archive")
        self.assertIn("vanished from source", r.stdout)
        r = self.run_retro("status")
        self.assertIn("RESCUED", r.stdout)

    def test_a_rescued_session_is_still_analysable(self):
        self.run_retro("archive")
        os.remove(self.session)
        self.run_retro("archive")
        self.assertEqual(self.run_retro("scan").returncode, 0)
        r = self.run_retro("friction")
        self.assertIn("Make the hero responsive", r.stdout)

    def test_the_archive_directory_is_owner_only(self):
        self.run_retro("archive")
        mode = os.stat(os.path.join(self.box, "arc")).st_mode & 0o777
        self.assertEqual(mode, 0o700)


class RedactionOnArchive(Sandbox):
    def setUp(self):
        super().setUp()
        self.secret = "sk-ant-api03-" + "Z" * 40
        claude_session(
            os.path.join(self.projects, "-home-dev-acme", f"{uuid.uuid4()}.jsonl"),
            [("user", f"the key is {self.secret}"),
             ("tool", ("Bash", {"command": "echo done"}, False))],
            title="Leaky session")
        self.consent()

    def _archive_text(self):
        parts = []
        for p in self.archived_files():
            with gzip.open(p, "rt", errors="replace") as fh:
                parts.append(fh.read())
        return "".join(parts)

    def test_secrets_never_reach_the_archive(self):
        r = self.run_retro("archive")
        self.assertIn("redacted before writing", r.stdout)
        self.assertNotIn(self.secret, self._archive_text())

    def test_every_archived_line_is_still_valid_json(self):
        self.run_retro("archive")
        for p in self.archived_files():
            with gzip.open(p, "rt", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if line.strip():
                        json.loads(line)  # raises if redaction corrupted it

    def test_raw_is_an_explicit_opt_out(self):
        r = self.run_retro("archive", "--raw")
        self.assertIn("WARNING", r.stdout)
        self.assertIn(self.secret, self._archive_text())


class Reading(Sandbox):
    def setUp(self):
        super().setUp()
        self.consent()
        self.run_retro("archive")
        self.run_retro("scan")

    def test_friction_reports_the_planted_churn_and_errors(self):
        r = self.run_retro("friction")
        self.assertIn("Hero.jsx", r.stdout)
        self.assertIn("ZERO verification runs", r.stdout)
        self.assertIn("error clusters", r.stdout)

    def test_friction_can_be_filtered_by_agent(self):
        r = self.run_retro("friction", "--agent", "codex")
        self.assertNotIn("Hero.jsx", r.stdout)

    def test_parity_lists_every_agent_present(self):
        r = self.run_retro("parity")
        self.assertIn("claude-code", r.stdout)
        self.assertIn("codex", r.stdout)

    def test_parity_percentages_are_never_above_one_hundred(self):
        """Regression: a denominator mismatch reported 107% coverage."""
        import re
        for pct in re.findall(r"(\d+)%", self.run_retro("parity").stdout):
            self.assertLessEqual(int(pct), 100)

    def test_heatmap_runs_and_can_export_svg(self):
        out = os.path.join(self.box, "h.svg")
        r = self.run_retro("heatmap", "--no-color", "--svg", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        with open(out) as fh:
            self.assertIn("<svg", fh.read(200))

    def test_every_heatmap_metric_is_accepted(self):
        for metric in ("prompts", "sessions", "edits", "tools", "tokens"):
            with self.subTest(metric=metric):
                r = self.run_retro("heatmap", "--metric", metric, "--no-color")
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_scan_reports_no_unrecognised_records_for_known_fixtures(self):
        r = self.run_retro("scan")
        self.assertNotIn("unrecognised", r.stdout)


class Sanitizing(Sandbox):
    def setUp(self):
        super().setUp()
        self.consent()
        self.run_retro("archive")
        self.run_retro("scan")
        self.out = os.path.join(self.box, "bundle")

    def test_a_bundle_and_a_redaction_report_are_written(self):
        r = self.run_retro("sanitize", "hero", "--out", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.out, "bundle.md")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "REDACTIONS.md")))

    def test_home_paths_are_masked(self):
        self.run_retro("sanitize", "hero", "--out", self.out)
        with open(os.path.join(self.out, "bundle.md")) as fh:
            body = fh.read()
        self.assertNotIn(os.path.expanduser("~"), body)

    def test_strict_mode_pseudonymises_filenames(self):
        self.run_retro("sanitize", "hero", "--out", self.out, "--strict")
        with open(os.path.join(self.out, "bundle.md")) as fh:
            body = fh.read()
        self.assertNotIn("Hero.jsx", body)

    def test_an_unmatched_session_fails_politely(self):
        r = self.run_retro("sanitize", "nothing-matches-this", "--out", self.out)
        self.assertIn("no session matched", r.stdout)


class Forgetting(Sandbox):
    def setUp(self):
        super().setUp()
        self.consent()
        self.run_retro("archive")
        self.run_retro("scan")

    def rows(self, table):
        import sqlite3
        con = sqlite3.connect(os.path.join(self.box, "retro.db"))
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()

    def test_a_dry_run_deletes_nothing(self):
        r = self.run_retro("forget", "--all")
        self.assertIn("re-run with --yes", r.stdout)
        self.assertEqual(len(self.archived_files()), 2)

    def test_yes_is_required_to_delete(self):
        self.run_retro("forget", "--all")
        self.assertGreater(self.rows("archive"), 0)

    def test_forget_all_clears_archive_sessions_and_prompts(self):
        self.run_retro("forget", "--all", "--yes")
        self.assertEqual(self.archived_files(), [])
        for table in ("archive", "sessions", "prompts", "blobs"):
            with self.subTest(table=table):
                self.assertEqual(self.rows(table), 0)

    def test_forget_all_works_even_when_the_archive_table_is_empty(self):
        """Regression: an early return made a second --all silently do nothing."""
        self.run_retro("forget", "--all", "--yes")
        r = self.run_retro("forget", "--all", "--yes")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 0)

    def test_forgetting_one_agent_leaves_the_others(self):
        self.run_retro("forget", "--agent", "codex", "--yes")
        self.assertEqual(len(self.archived_files()), 1)


class HelpAndErrors(Sandbox):
    def test_bare_invocation_prints_grouped_help(self):
        r = self.run_retro()
        self.assertIn("QUICK START", r.stdout)
        self.assertIn("COMMANDS", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_every_spelling_of_help_works(self):
        for word in ("help", "-help", "--help", "-h", "-?", "?"):
            with self.subTest(word=word):
                r = self.run_retro(word)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("QUICK START", r.stdout)

    def test_per_command_help_is_reachable_three_ways(self):
        for args in (("help", "forget"), ("forget", "--help"), ("forget", "help")):
            with self.subTest(args=args):
                r = self.run_retro(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Dry run unless --yes", r.stdout)

    def test_every_command_has_help_text(self):
        for cmd in ("consent", "archive", "status", "scan", "friction", "heatmap",
                    "parity", "sanitize", "install-hook", "encrypt", "forget"):
            with self.subTest(cmd=cmd):
                r = self.run_retro("help", cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertGreater(len(r.stdout), 120, f"{cmd} help is too thin")

    def test_an_unknown_command_is_rejected(self):
        r = self.run_retro("definitely-not-a-command")
        self.assertNotEqual(r.returncode, 0)

    def test_help_for_an_unknown_command_says_so(self):
        r = self.run_retro("help", "nope")
        self.assertIn("no such command", r.stdout)

    def test_errors_are_prefixed_with_the_subcommand(self):
        """Regression: read 'retro <command> [options] sanitize: error: ...'."""
        r = self.run_retro("sanitize")
        self.assertIn("retro sanitize: error:", r.stderr)

    def test_reading_before_scanning_asks_you_to_scan(self):
        r = self.run_retro("friction")
        self.assertIn("scan", r.stdout.lower())


class Locking(Sandbox):
    def test_a_stale_lock_does_not_block_archiving(self):
        self.consent()
        os.makedirs(self.box, exist_ok=True)
        with open(os.path.join(self.box, ".archive.lock"), "w") as fh:
            fh.write("999999")  # a pid that is not running
        r = self.run_retro("archive")
        self.assertIn("archived", r.stdout)

    def test_a_live_lock_is_respected(self):
        self.consent()
        with open(os.path.join(self.box, ".archive.lock"), "w") as fh:
            fh.write(str(os.getpid()))  # this test process is certainly alive
        r = self.run_retro("archive")
        self.assertIn("in progress", r.stdout)
        self.assertEqual(self.archived_files(), [])


class Isolation(Sandbox):
    def test_the_real_stores_are_never_touched(self):
        """The suite must be safe to run on a machine with real sessions."""
        self.consent()
        self.run_retro("archive")
        self.run_retro("scan")
        for leaked in (os.path.join(REPO, "archive"), os.path.join(REPO, "retro.db")):
            before = os.path.getmtime(leaked) if os.path.exists(leaked) else None
            self.run_retro("friction")
            after = os.path.getmtime(leaked) if os.path.exists(leaked) else None
            self.assertEqual(before, after, f"{leaked} was modified by the tests")


if __name__ == "__main__":
    unittest.main()
