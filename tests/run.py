#!/usr/bin/env python3
"""Run the whole suite.

    python3 tests/run.py            # everything
    python3 tests/run.py -v         # verbose
    python3 tests/run.py metrics    # only files matching *metrics*

ResourceWarning is promoted to an error on purpose: leaking one file handle per
session is invisible in a unit test and turns into hundreds of open descriptors
on a real corpus. That bug shipped once already.
"""
import os
import sys
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    names = [a for a in argv if not a.startswith("-")]
    pattern = f"test_*{names[0]}*.py" if names else "test_*.py"

    warnings.simplefilter("error", ResourceWarning)
    suite = unittest.defaultTestLoader.discover(HERE, pattern=pattern, top_level_dir=HERE)
    if suite.countTestCases() == 0:
        print(f"no tests matched {pattern!r}")
        return 1
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
