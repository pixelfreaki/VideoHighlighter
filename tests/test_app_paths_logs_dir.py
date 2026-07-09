"""Tests for modules/app_paths.py's logs_dir() helper."""

from __future__ import annotations

import os
from unittest.mock import patch

from modules import app_paths


def test_logs_dir_creates_directory_when_missing(tmp_path):
    with patch.object(app_paths, "user_data_dir", return_value=str(tmp_path)):
        result = app_paths.logs_dir()

    assert os.path.isdir(result)
    assert result == os.path.join(str(tmp_path), "logs")


def test_logs_dir_returns_existing_directory_without_error(tmp_path):
    existing = os.path.join(str(tmp_path), "logs")
    os.makedirs(existing)

    with patch.object(app_paths, "user_data_dir", return_value=str(tmp_path)):
        result = app_paths.logs_dir()

    assert result == existing


def test_logs_dir_is_under_user_data_dir(tmp_path):
    with patch.object(app_paths, "user_data_dir", return_value=str(tmp_path)):
        result = app_paths.logs_dir()

    assert os.path.dirname(result) == str(tmp_path)
