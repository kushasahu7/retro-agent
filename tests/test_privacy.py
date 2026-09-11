"""Redaction, code stripping, consent and encryption.

The stakes here are asymmetric. Over-redacting is a cosmetic problem; missing
one credential means a secret sitting in a permanent archive, or worse, in a
bundle someone emailed to a stranger. So these tests care about recall, and
about the archive never being left in a corrupt state.
"""
import json, os, tempfile, unittest
from collections import Counter

import helpers  # noqa: F401
from retro_agent import privacy


def redact(line):
    c = Counter()
    return privacy.redact_line(line, c), c


class RedactionRecall(unittest.TestCase):
    """Each of these appeared in a real transcript."""

    def assertMasked(self, secret, kind=None):
        line = json.dumps({"note": f"value is {secret} ok"})
        out, counts = redact(line)
        self.assertNotIn(secret, out, f"{secret[:8]}... survived redaction")
        if kind:
            self.assertGreaterEqual(counts[kind], 1)

    def test_anthropic_and_openai_keys(self):
        self.assertMasked("sk-ant-api03-" + "A" * 40, "api_key")
        self.assertMasked("sk-" + "B" * 40, "api_key")

    def test_github_tokens(self):
        self.assertMasked("ghp_" + "c" * 36, "api_key")
        self.assertMasked("gho_" + "d" * 36, "api_key")

    def test_aws_access_key(self):
        self.assertMasked("AKIA" + "ABCDEFGHIJKLMNOP", "api_key")

    def test_slack_token(self):
        self.assertMasked("xoxb-1234567890-abcdefghijkl", "api_key")

    def test_gitlab_and_google_and_npm(self):
        self.assertMasked("glpat-" + "e" * 20, "api_key")
        self.assertMasked("AIza" + "f" * 35, "api_key")
        self.assertMasked("npm_" + "g" * 36, "api_key")

    def test_database_urls(self):
        for url in ("postgres://user:pw@host:5432/db",
                    "postgresql://user:pw@host/db",
                    "mongodb+srv://user:pw@cluster.example/db",
                    "redis://:pw@localhost:6379/0",
                    "mysql://root:pw@127.0.0.1/app"):
            with self.subTest(url=url):
                self.assertMasked(url, "conn_string")

    def test_jwt(self):
        self.assertMasked("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef", "jwt")

    def test_assigned_secrets(self):
        for line in ('{"c":"API_KEY=aaaaaaaaaaaaaaaaaaaa"}',
                     '{"c":"password: bbbbbbbbbbbbbbbbbbbb"}',
                     '{"c":"auth_token = cccccccccccccccccccc"}'):
            with self.subTest(line=line):
                out, counts = redact(line)
                self.assertGreaterEqual(counts["assigned_secret"], 1)

    def test_private_key_block(self):
        key = ("-----BEGIN RSA PRIVATE KEY-----\\nMIIEabc\\n"
               "-----END RSA PRIVATE KEY-----")
        out, counts = redact(json.dumps({"k": key}))
        self.assertEqual(counts["private_key"], 1)


class RedactionSafety(unittest.TestCase):
    def test_output_is_always_valid_json(self):
        lines = [
            json.dumps({"k": "sk-" + "A" * 40}),
            json.dumps({"db": "postgres://u:p@h/d", "n": 1}),
            json.dumps({"nested": {"list": ["password: " + "z" * 30]}}),
            json.dumps({"quote": 'he said "token=' + "q" * 30 + '"'}),
            json.dumps({"esc": "path\\\\to\\\\thing", "k": "ghp_" + "e" * 36}),
        ]
        for line in lines:
            with self.subTest(line=line[:40]):
                out, _ = redact(line)
                json.loads(out)  # raises if redaction broke the record

    def test_a_line_that_cannot_be_redacted_safely_is_kept_raw(self):
        # Not valid JSON to begin with, so the guard must return it untouched
        # rather than emitting a half-substituted line.
        c = Counter()
        line = 'this is not json but has sk-' + "A" * 40
        out = privacy.redact_line(line, c)
        self.assertEqual(out, line)
        self.assertEqual(c["_reverted_invalid_json"], 1)

    def test_ordinary_text_is_left_alone(self):
        line = json.dumps({"msg": "the tests pass and the build is green"})
        out, counts = redact(line)
        self.assertEqual(out, line)
        self.assertEqual(sum(counts.values()), 0)

    def test_emails_are_deliberately_not_redacted_at_archive_time(self):
        # They are load-bearing context; `sanitize` masks them on the way out.
        line = json.dumps({"to": "someone@example.com"})
        out, _ = redact(line)
        self.assertIn("someone@example.com", out)


class CursorCodeStripping(unittest.TestCase):
    def test_code_fields_are_replaced_with_a_size_marker(self):
        out = privacy.strip_cursor_code({"oldString": "x" * 30, "name": "edit_file_v2"})
        self.assertEqual(out["oldString"], "<CODE_OMITTED:30b>")
        self.assertEqual(out["name"], "edit_file_v2")

    def test_stripping_reaches_nested_structures(self):
        out = privacy.strip_cursor_code(
            {"a": [{"newString": "y" * 10}], "b": {"c": {"code_edit": "z" * 5}}})
        self.assertEqual(out["a"][0]["newString"], "<CODE_OMITTED:10b>")
        self.assertEqual(out["b"]["c"]["code_edit"], "<CODE_OMITTED:5b>")

    def test_metrics_fields_survive(self):
        out = privacy.strip_cursor_code(
            {"relativeWorkspacePath": "src/a.py", "status": "completed",
             "userDecision": "accepted", "oldString": "q" * 8})
        self.assertEqual(out["relativeWorkspacePath"], "src/a.py")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["userDecision"], "accepted")


class Consent(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_archiving_is_refused_until_consent_is_recorded(self):
        self.assertIsNone(privacy.require_consent(self.root, quiet=True))

    def test_consent_persists_and_can_be_withdrawn(self):
        cfg = privacy.load_config(self.root)
        cfg["consent"] = {"accepted": True, "at": "2026-09-11T00:00:00"}
        privacy.save_config(self.root, cfg)
        self.assertIsNotNone(privacy.require_consent(self.root, quiet=True))
        cfg["consent"] = {"accepted": False, "at": None}
        privacy.save_config(self.root, cfg)
        self.assertIsNone(privacy.require_consent(self.root, quiet=True))

    def test_defaults_protect_rather_than_expose(self):
        cfg = privacy.load_config(self.root)
        self.assertTrue(cfg["redact_on_archive"])
        self.assertFalse(cfg["cursor_include_code"])
        self.assertFalse(cfg["encryption"]["enabled"])

    def test_config_is_owner_readable_only(self):
        privacy.save_config(self.root, privacy.load_config(self.root))
        mode = os.stat(privacy.config_path(self.root)).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_corrupt_config_falls_back_to_defaults(self):
        with open(privacy.config_path(self.root), "w") as fh:
            fh.write("{not json")
        self.assertFalse(privacy.load_config(self.root)["consent"]["accepted"])


@unittest.skipUnless(privacy.encryption_available(), "cryptography not installed")
class Encryption(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.arc = os.path.join(self.root, "arc", "proj")
        os.makedirs(self.arc)
        self.path = os.path.join(self.arc, "s1.jsonl.gz")
        import gzip
        with gzip.open(self.path, "wt") as fh:
            fh.write(json.dumps({"secret": "hello world"}) + "\n")
        with open(self.path, "rb") as fh:
            self.before = fh.read()
        self.arc_root = os.path.join(self.root, "arc")

    def test_round_trip_is_byte_identical(self):
        privacy.encrypt_archive(self.root, self.arc_root, "pw")
        privacy.decrypt_archive(self.root, self.arc_root, "pw")
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), self.before)

    def test_plaintext_is_removed_and_content_is_opaque(self):
        privacy.encrypt_archive(self.root, self.arc_root, "pw")
        self.assertFalse(os.path.exists(self.path))
        with open(self.path + ".enc", "rb") as fh:
            raw = fh.read()
        self.assertNotEqual(raw[:2], b"\x1f\x8b")
        self.assertNotIn(b"hello world", raw)

    def test_wrong_passphrase_is_rejected(self):
        privacy.encrypt_archive(self.root, self.arc_root, "pw")
        with self.assertRaises(Exception):
            privacy.decrypt_archive(self.root, self.arc_root, "wrong")

    def test_encrypted_members_can_be_read_transparently(self):
        privacy.encrypt_archive(self.root, self.arc_root, "pw")
        os.environ["RETRO_PASSPHRASE"] = "pw"
        try:
            fh = privacy.open_maybe_encrypted(self.path + ".enc", self.root)
            self.assertIn("hello world", fh.read())
        finally:
            os.environ.pop("RETRO_PASSPHRASE", None)

    def test_encrypted_files_are_owner_readable_only(self):
        privacy.encrypt_archive(self.root, self.arc_root, "pw")
        mode = os.stat(self.path + ".enc").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_no_passphrase_without_a_tty_returns_none_rather_than_hanging(self):
        os.environ.pop("RETRO_PASSPHRASE", None)
        self.assertIsNone(privacy.get_passphrase())


if __name__ == "__main__":
    unittest.main()
