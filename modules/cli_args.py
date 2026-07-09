"""Command-line argument parsing for main.py.

Kept as a small, dependency-light module (stdlib only) so it's testable
without importing main.py's heavy Qt/CV/ML transitive imports.
"""

from __future__ import annotations

import argparse


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse this app's own CLI flags, ignoring anything unrecognized.

    Uses parse_known_args() rather than parse_args() so Qt's own flags
    (e.g. -style) pass through untouched to QApplication(sys.argv), called
    later with the original, unmodified sys.argv -- argparse never mutates
    sys.argv itself.

    --help/-h is handled entirely by argparse's built-in behavior: it
    prints usage and raises SystemExit.
    """
    parser = argparse.ArgumentParser(
        description="VideoHighlighter - video highlight generation.",
    )
    parser.add_argument(
        "--conf",
        metavar="<path>",
        default=None,
        help="Path to a config.yaml to use instead of the default.",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args
