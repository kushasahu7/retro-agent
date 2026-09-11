"""Per-agent parsing.

Most of these are regressions. Each one is a bug that shipped and was caught by
eye rather than by a test, which is the reason this file exists.
"""
import json, os, tempfile, unittest, uuid

import helpers
from helpers import (claude_session, codex_session, codex_meta, codex_shell,
                     codex_custom_exec, codex_token_count, IDE_WRAPPED,
                     cursor_db, cursor_text, cursor_tool)
import adapters
import retro


class ClaudeCode(unittest.TestCase):
    def load(self, script):
        d = tempfile.mkdtemp()
        return adapters.ClaudeAdapter.load(
            claude_session(os.path.join(d, "p", f"{uuid.uuid4()}.jsonl"), script))

    def test_title_comes_from_the_ai_title_record(self):
        d = tempfile.mkdtemp()
        p = claude_session(os.path.join(d, "p", "s.jsonl"),
                           [("user", "hi")], title="Fix the worker")
        self.assertEqual(adapters.ClaudeAdapter.load(p).title, "Fix the worker")

    def test_tool_kinds_are_assigned_from_the_tool_name(self):
        s = self.load([("user", "go"),
                       ("tool", ("Edit", {"file_path": "/a/b.py"}, False)),
                       ("tool", ("Read", {"file_path": "/a/b.py"}, False)),
                       ("tool", ("WebFetch", {"url": "https://x.test"}, False)),
                       ("tool", ("Bash", {"command": "pytest -q"}, False))])
        self.assertEqual([t["kind"] for t in s.tools],
                         ["edit", "read", "web", "verify"])

    def test_edit_target_is_the_file_path(self):
        s = self.load([("user", "go"),
                       ("tool", ("Write", {"file_path": "/a/b.py"}, False))])
        self.assertEqual(s.tools[0]["target"], "/a/b.py")

    def test_error_flag_is_taken_from_the_agent_not_inferred(self):
        s = self.load([("user", "go"),
                       ("tool", ("Bash", {"command": "x"}, True)),
                       ("tool", ("Bash", {"command": "y"}, False))])
        self.assertEqual([t["error"] for t in s.tools], [True, False])

    def test_mcp_tools_fall_back_to_other(self):
        s = self.load([("user", "go"),
                       ("tool", ("mcp__thing__do", {"a": 1}, False))])
        self.assertEqual(s.tools[0]["kind"], "other")

    def test_unknown_record_types_are_counted_not_dropped_silently(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": helpers._ts(1),
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "brand-new-thing", "x": 1}) + "\n")
        s = adapters.ClaudeAdapter.load(p)
        self.assertEqual(s.unknown_types["brand-new-thing"], 1)

    def test_malformed_lines_do_not_crash_the_parse(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as fh:
            fh.write("{not json at all\n")
            fh.write("\n")
            fh.write(json.dumps({"type": "user", "timestamp": helpers._ts(1),
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
        self.assertEqual(len(adapters.ClaudeAdapter.load(p).turns), 1)


class Codex(unittest.TestCase):
    def load(self, records):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "2026", "09", "02",
                         f"rollout-2026-09-02T10-00-00-{uuid.uuid4()}.jsonl")
        return adapters.CodexAdapter.load(codex_session(p, records))

    def test_shell_calls_are_classified_through_the_command(self):
        s = self.load([codex_meta()]
                      + codex_shell("c1", "pytest -q", 0, 1)
                      + codex_shell("c2", "apply_patch <<'EOF'\n"
                                          "*** Update File: w/i.py\nEOF", 0, 2))
        self.assertEqual([t["kind"] for t in s.tools], ["verify", "edit"])
        self.assertEqual(s.tools[1]["target"], "w/i.py")

    def test_exit_code_drives_the_error_flag(self):
        s = self.load([codex_meta()]
                      + codex_shell("c1", "ls", 0, 1)
                      + codex_shell("c2", "ls", 127, 2))
        self.assertEqual([t["error"] for t in s.tools], [False, True])

    def test_custom_tool_calls_are_captured(self):
        """Regression: 52 custom_tool_call records were dropped entirely."""
        s = self.load([codex_meta()]
                      + codex_custom_exec("c1", "pytest -q", 1)
                      + codex_custom_exec("c2", "rm -rf build", 2, failed=True))
        self.assertEqual(len(s.tools), 2)
        self.assertEqual(s.tools[0]["kind"], "verify")
        self.assertTrue(s.tools[1]["error"])

    def test_token_counts_are_cumulative_not_additive(self):
        """Regression: summing total_token_usage inflated totals badly."""
        s = self.load([codex_meta(),
                       codex_token_count(total_in=100, total_out=50, minute=1),
                       codex_token_count(total_in=400, total_out=200, minute=2)])
        self.assertEqual(s.tokens["input_tokens"], 400)
        self.assertEqual(s.tokens["output_tokens"], 200)

    def test_the_real_prompt_is_pulled_out_of_the_ide_context_block(self):
        """Regression: five sessions reported zero human turns because every
        user message was an IDE context wrapper."""
        s = self.load([codex_meta(),
                       {"type": "message", "timestamp": helpers._ts(1),
                        "role": "user", "content": [
                            {"type": "input_text",
                             "text": IDE_WRAPPED.format("make the hero responsive")}]}])
        real = [t for t in s.turns if not t["meta"]]
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0]["text"], "make the hero responsive")

    def test_environment_context_is_marked_as_not_a_human_turn(self):
        s = self.load([codex_meta(),
                       {"type": "message", "timestamp": helpers._ts(1),
                        "role": "user", "content": [
                            {"type": "input_text",
                             "text": "<environment_context>\n<cwd>/x</cwd>"}]}])
        self.assertTrue(all(t["meta"] for t in s.turns))

    def test_user_message_records_become_turns(self):
        """Regression: user_message and agent_message were skipped as noise."""
        s = self.load([codex_meta(),
                       {"type": "user_message", "timestamp": helpers._ts(1),
                        "payload": {"type": "user_message", "message": "add retries"}},
                       {"type": "agent_message", "timestamp": helpers._ts(2),
                        "payload": {"type": "agent_message", "message": "done"}}])
        self.assertEqual([t["role"] for t in s.turns], ["user", "assistant"])
        self.assertEqual(s.turns[0]["text"], "add retries")

    def test_the_same_prompt_in_both_shapes_is_not_counted_twice(self):
        s = self.load([codex_meta(),
                       {"type": "user_message", "timestamp": helpers._ts(1),
                        "payload": {"type": "user_message", "message": "add retries"}},
                       {"type": "message", "timestamp": helpers._ts(1),
                        "role": "user",
                        "content": [{"type": "input_text", "text": "add retries"}]}])
        self.assertEqual(len([t for t in s.turns if t["role"] == "user"]), 1)

    def test_patch_apply_end_yields_one_edit_per_changed_file(self):
        s = self.load([codex_meta(),
                       {"type": "patch_apply_end", "timestamp": helpers._ts(1),
                        "call_id": "e1", "success": True,
                        "changes": {"/a/x.md": {"type": "add", "content": "hi"},
                                    "/a/y.md": {"type": "update", "content": "ho"}}}])
        self.assertEqual(len(s.tools), 2)
        self.assertTrue(all(t["kind"] == "edit" for t in s.tools))
        self.assertFalse(any(t["error"] for t in s.tools))

    def test_a_failed_patch_is_an_error(self):
        s = self.load([codex_meta(),
                       {"type": "patch_apply_end", "timestamp": helpers._ts(1),
                        "call_id": "e1", "success": False,
                        "changes": {"/a/x.md": {"type": "add"}}}])
        self.assertTrue(s.tools[0]["error"])


class Cursor(unittest.TestCase):
    def build(self, bubbles, name="Website fixes"):
        d = tempfile.mkdtemp()
        db = cursor_db(os.path.join(d, "state.vscdb"), {"c1": (name, bubbles)})
        return adapters.CursorAdapter.load("cursor://c1", db=db), db

    def test_discovery_finds_conversations(self):
        _, db = self.build([cursor_text("b1", "hello")])
        self.assertEqual(adapters.CursorAdapter.discover(db=db), ["cursor://c1"])

    def test_type_one_is_the_human_and_type_two_the_agent(self):
        s, _ = self.build([cursor_text("b1", "fix it", 1),
                           cursor_text("b2", "fixed", 2)])
        self.assertEqual([t["role"] for t in s.turns], ["user", "assistant"])

    def test_edit_target_reads_relative_workspace_path(self):
        """Regression: the adapter looked for `targetFile`, which does not exist,
        so 86% of Cursor edits had no target and churn never fired."""
        s, _ = self.build([cursor_tool("b1", "edit_file_v2",
                                       params={"relativeWorkspacePath": "src/Hero.jsx"})])
        self.assertEqual(s.tools[0]["kind"], "edit")
        self.assertEqual(s.tools[0]["target"], "src/Hero.jsx")

    def test_raw_args_file_path_is_a_fallback_target(self):
        s, _ = self.build([cursor_tool("b1", "search_replace",
                                       raw={"file_path": "package.json"})])
        self.assertEqual(s.tools[0]["target"], "package.json")

    def test_terminal_commands_are_classified_through_the_command(self):
        s, _ = self.build([cursor_tool("b1", "run_terminal_command_v2",
                                       params={"command": "pytest -q"})])
        self.assertEqual(s.tools[0]["kind"], "verify")

    def test_status_drives_the_error_flag(self):
        s, _ = self.build([cursor_tool("b1", "read_file_v2", status="completed"),
                           cursor_tool("b2", "read_file_v2", status="error")])
        self.assertEqual([t["error"] for t in s.tools], [False, True])

    def test_user_decision_is_captured(self):
        s, _ = self.build([cursor_tool("b1", "edit_file_v2", decision="accepted",
                                       params={"relativeWorkspacePath": "a.py"}),
                           cursor_tool("b2", "edit_file_v2", decision="rejected",
                                       params={"relativeWorkspacePath": "a.py"})])
        m = retro.metrics(s)
        self.assertEqual(m["decided"], 2)
        self.assertEqual(m["rejected"], 1)

    def test_export_round_trips_through_jsonl(self):
        _, db = self.build([cursor_text("b1", "fix it", 1),
                            cursor_tool("b2", "edit_file_v2",
                                        params={"relativeWorkspacePath": "src/a.py"})])
        blob = adapters.CursorAdapter.export("cursor://c1", db=db, include_code=True)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c1.jsonl")
        with open(p, "wb") as fh:
            fh.write(blob)
        s = adapters.CursorAdapter.load(p)
        self.assertEqual(len(s.turns), 1)
        self.assertEqual(s.tools[0]["target"], "src/a.py")

    def test_export_omits_source_code_by_default(self):
        _, db = self.build([cursor_tool("b1", "search_replace",
                                        params={"relativeWorkspacePath": "a.py",
                                                "oldString": "x" * 500,
                                                "newString": "y" * 500})])
        blob = adapters.CursorAdapter.export("cursor://c1", db=db).decode()
        self.assertIn("CODE_OMITTED", blob)
        self.assertNotIn("x" * 100, blob)
        self.assertNotIn("y" * 100, blob)


if __name__ == "__main__":
    unittest.main()
