"""Tests for modules/app_paths.py's --conf override support in config_path()."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from modules import app_paths


@pytest.fixture(autouse=True)
def _reset_config_override():
    """_config_override is process-global state; reset it around every test
    so the override-set and override-unset scenarios don't leak."""
    saved = app_paths._config_override
    app_paths._config_override = None
    yield
    app_paths._config_override = saved


def test_config_path_returns_override_when_set(tmp_path):
    override_path = str(tmp_path / "custom.yaml")
    app_paths.set_config_override(override_path)

    assert app_paths.config_path("config.yaml") == override_path


def test_config_path_returns_override_regardless_of_filename_argument(tmp_path):
    override_path = str(tmp_path / "custom.yaml")
    app_paths.set_config_override(override_path)

    assert app_paths.config_path("some_other_name.yaml") == override_path
    assert app_paths.config_path() == override_path


def test_config_path_without_override_preserves_default_behavior(tmp_path):
    with patch.object(app_paths, "user_data_dir", return_value=str(tmp_path)):
        result = app_paths.config_path("config.yaml")

    assert result == os.path.join(str(tmp_path), "config.yaml")


def test_config_path_with_override_skips_bundled_seed_copy(tmp_path):
    override_path = str(tmp_path / "custom.yaml")
    app_paths.set_config_override(override_path)

    with patch("shutil.copy2") as mock_copy:
        app_paths.config_path("config.yaml")

    mock_copy.assert_not_called()
