"""Packaging invariants.

Getting these wrong is quiet and nasty: a flat layout installs top-level
`adapters` and `privacy` modules into site-packages where they can shadow other
people's imports, and an installed copy that writes next to its own code puts a
growing archive inside site-packages.
"""
import os, sys, tempfile, unittest

import helpers
from retro_agent import cli

REPO = helpers.REPO
PYPROJECT = os.path.join(REPO, "pyproject.toml")

try:
    import tomllib
except ImportError:  # Python 3.9 / 3.10
    tomllib = None


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class Pyproject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PYPROJECT, "rb") as fh:
            cls.cfg = tomllib.load(fh)

    def test_the_distribution_name_is_the_one_that_is_free_on_pypi(self):
        self.assertEqual(self.cfg["project"]["name"], "retro-agent")

    def test_only_the_package_is_installed_no_top_level_modules(self):
        self.assertEqual(self.cfg["tool"]["setuptools"]["packages"], ["retro_agent"])

    def test_there_are_no_required_dependencies(self):
        # `uvx retro-agent` should not have to resolve anything.
        self.assertEqual(self.cfg["project"]["dependencies"], [])

    def test_encryption_is_the_only_optional_extra(self):
        extras = self.cfg["project"]["optional-dependencies"]
        self.assertEqual(list(extras), ["encryption"])

    def test_the_console_script_points_at_a_real_callable(self):
        target = self.cfg["project"]["scripts"]["retro"]
        module, _, attr = target.partition(":")
        self.assertEqual(module, "retro_agent.cli")
        self.assertTrue(callable(getattr(sys.modules[module], attr, None)),
                        f"{target} is not callable")

    def test_the_declared_version_matches_the_package(self):
        import retro_agent
        self.assertEqual(self.cfg["project"]["version"], retro_agent.__version__)


class DataLocation(unittest.TestCase):
    """An installed copy must never write into its own install directory."""

    def setUp(self):
        self.checkout = cli.CHECKOUT
        self.home = os.environ.pop("RETRO_HOME", None)

    def tearDown(self):
        cli.CHECKOUT = self.checkout
        if self.home is None:
            os.environ.pop("RETRO_HOME", None)
        else:
            os.environ["RETRO_HOME"] = self.home

    def test_retro_home_wins_when_set(self):
        d = tempfile.mkdtemp()
        os.environ["RETRO_HOME"] = d
        self.assertEqual(cli._data_root(), d)

    def test_a_clean_install_falls_back_to_a_user_directory(self):
        cli.CHECKOUT = tempfile.mkdtemp()  # stands in for site-packages
        self.assertEqual(cli._data_root(), os.path.expanduser("~/.retro-agent"))

    def test_an_existing_checkout_archive_keeps_being_used(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "archive"))
        cli.CHECKOUT = d
        self.assertEqual(cli._data_root(), d)

    def test_an_existing_checkout_database_keeps_being_used(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "retro.db"), "w").close()
        cli.CHECKOUT = d
        self.assertEqual(cli._data_root(), d)

    def test_the_package_directory_is_never_the_data_root(self):
        cli.CHECKOUT = tempfile.mkdtemp()
        self.assertNotEqual(cli._data_root(), cli.PKG_DIR)


class Layout(unittest.TestCase):
    def test_the_modules_live_inside_the_package(self):
        for name in ("cli.py", "adapters.py", "privacy.py", "__init__.py"):
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(os.path.join(REPO, "retro_agent", name)))

    def test_no_stale_flat_modules_remain_at_the_repo_root(self):
        for name in ("retro.py", "adapters.py", "privacy.py"):
            with self.subTest(name=name):
                self.assertFalse(os.path.exists(os.path.join(REPO, name)),
                                 f"{name} should have moved into retro_agent/")

    def test_the_dev_wrapper_runs_the_package_not_a_script(self):
        # The wrapper is a checkout convenience. A distribution ships the
        # console script instead, so its absence is correct, not a failure.
        wrapper = os.path.join(REPO, "retro")
        if not os.path.exists(wrapper):
            self.skipTest("no dev wrapper: running from a built distribution")
        with open(wrapper) as fh:
            body = fh.read()
        self.assertIn("retro_agent.cli", body)
        self.assertNotIn("retro.py", body)


if __name__ == "__main__":
    unittest.main()
