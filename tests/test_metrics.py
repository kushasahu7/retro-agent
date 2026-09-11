"""The friction metrics themselves.

These are the numbers the whole tool reports, and they are pure functions of a
parsed session, so they can be asserted exactly.
"""
import os, tempfile, unittest, uuid

import helpers
from helpers import claude_session
import adapters
import retro


def load(script, title="T"):
    d = tempfile.mkdtemp()
    p = claude_session(os.path.join(d, "proj", f"{uuid.uuid4()}.jsonl"), script, title)
    return retro.metrics(adapters.ClaudeAdapter.load(p))


def edit(path="src/Hero.jsx", err=False):
    return ("tool", ("Edit", {"file_path": path}, err))


def verify(err=False):
    return ("tool", ("Bash", {"command": "pytest -q"}, err))


def shell(cmd="docker compose up", err=False):
    return ("tool", ("Bash", {"command": cmd}, err))


class Churn(unittest.TestCase):
    def test_three_edits_with_no_verification_is_churn(self):
        m = load([("user", "go"), edit(), edit(), edit()])
        self.assertEqual([f for f, _ in m["churn"]], ["Hero.jsx"])
        self.assertEqual(m["edits"], 3)
        self.assertEqual(m["verifies"], 0)

    def test_two_edits_is_not_churn(self):
        m = load([("user", "go"), edit(), edit()])
        self.assertEqual(m["churn"], [])

    def test_verification_between_edits_clears_churn(self):
        m = load([("user", "go"), edit(), verify(), edit(), verify(), edit()])
        self.assertEqual(m["churn"], [])
        self.assertEqual(m["verifies"], 2)

    def test_verification_after_the_run_does_not_clear_it(self):
        # The check has to happen *between* the edits to count.
        m = load([("user", "go"), edit(), edit(), edit(), verify()])
        self.assertEqual([f for f, _ in m["churn"]], ["Hero.jsx"])

    def test_edits_far_apart_are_not_churn(self):
        # Three edits to one file, but spread beyond the 10-call window.
        script = [("user", "go"), edit()]
        script += [shell()] * 9
        script += [edit()]
        script += [shell()] * 9
        script += [edit()]
        m = load(script)
        self.assertEqual(m["churn"], [])

    def test_different_files_do_not_pool(self):
        m = load([("user", "go"), edit("a/x.py"), edit("a/y.py"), edit("a/z.py")])
        self.assertEqual(m["churn"], [])

    def test_churn_reports_total_edits_for_the_file(self):
        m = load([("user", "go")] + [edit()] * 5)
        self.assertEqual(m["churn"], [("Hero.jsx", 5)])


class Errors(unittest.TestCase):
    def test_flail_rate_counts_only_answered_calls(self):
        m = load([("user", "go"), shell(err=True), shell(err=False)])
        self.assertEqual(m["errors"], 1)
        self.assertAlmostEqual(m["flail"], 0.5)

    def test_error_clusters_need_two_consecutive_failures(self):
        m = load([("user", "go"), shell(err=True), shell(err=False),
                  shell(err=True), shell(err=True), shell(err=True)])
        self.assertEqual(m["error_clusters"], [3])

    def test_single_failures_are_not_a_cluster(self):
        m = load([("user", "go"), shell(err=True), shell(), shell(err=True)])
        self.assertEqual(m["error_clusters"], [])

    def test_a_trailing_cluster_is_counted(self):
        m = load([("user", "go"), shell(), shell(err=True), shell(err=True)])
        self.assertEqual(m["error_clusters"], [2])


class Retries(unittest.TestCase):
    def test_identical_calls_count_as_retries(self):
        m = load([("user", "go"), shell("npm ci"), shell("npm ci"), shell("npm ci")])
        self.assertEqual(m["exact_retries"], 2)

    def test_different_inputs_are_not_retries(self):
        m = load([("user", "go"), shell("npm ci"), shell("npm ls")])
        self.assertEqual(m["exact_retries"], 0)

    def test_key_order_does_not_create_a_false_difference(self):
        s = adapters.SessionIR("x", "p")
        for inp in ({"a": 1, "b": 2}, {"b": 2, "a": 1}):
            s.tools.append(dict(idx=0, name="Bash", kind="shell", target=None,
                                input=inp, ts=None, sidechain=False, error=False))
        self.assertEqual(retro.metrics(s)["exact_retries"], 1)


class Timing(unittest.TestCase):
    def test_active_time_excludes_long_gaps(self):
        # The fixture puts calls one minute apart, so nothing is skipped.
        m = load([("user", "go"), shell(), shell(), shell()])
        self.assertGreater(m["active_min"], 0)
        self.assertEqual(m["idle_gaps"], [])

    def test_a_session_with_one_timestamp_has_no_duration(self):
        s = adapters.SessionIR("x", "p")
        s.turns.append(dict(idx=0, role="user", ts=helpers.BASE, sidechain=False,
                            text="hi", chars=2, uuid=None, parent=None, meta=False))
        self.assertEqual(retro.metrics(s)["wall_min"], 0.0)


class Turns(unittest.TestCase):
    def test_tool_results_are_not_human_turns(self):
        # Each tool call generates a `user` record carrying only a tool_result.
        m = load([("user", "go"), shell(), shell(), shell()])
        self.assertEqual(m["human_turns"], 1)

    def test_subagent_work_is_separated(self):
        s = adapters.SessionIR("x", "p")
        for side in (False, True, True):
            s.tools.append(dict(idx=0, name="Bash", kind="shell", target=None,
                                input={"command": str(side)}, ts=None,
                                sidechain=side, error=False))
        m = retro.metrics(s)
        self.assertEqual(m["tools"], 1)
        self.assertEqual(m["sub_tools"], 2)


class Tokens(unittest.TestCase):
    def test_cache_reads_are_kept_separate_from_output(self):
        m = load([("user", "go"), shell(), shell()])
        self.assertEqual(m["tokens"]["output_tokens"], 40)
        self.assertEqual(m["tokens"]["cache_read_input_tokens"], 1000)


if __name__ == "__main__":
    unittest.main()
