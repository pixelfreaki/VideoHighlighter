"""Tests for modules/cli_args.py's parse_args()."""

from __future__ import annotations

import pytest

from modules import cli_args


def test_parse_args_returns_conf_path_when_given():
    args = cli_args.parse_args(["--conf", "/some/path.yaml"])
    assert args.conf == "/some/path.yaml"


def test_parse_args_conf_defaults_to_none():
    args = cli_args.parse_args([])
    assert args.conf is None


def test_parse_args_ignores_unrecognized_qt_style_flags():
    # Must not raise -- parse_known_args(), not parse_args().
    args = cli_args.parse_args(["-style", "bb10dark"])
    assert args.conf is None


def test_parse_args_help_flag_exits():
    with pytest.raises(SystemExit):
        cli_args.parse_args(["--help"])


def test_parse_args_short_help_flag_exits():
    with pytest.raises(SystemExit):
        cli_args.parse_args(["-h"])
