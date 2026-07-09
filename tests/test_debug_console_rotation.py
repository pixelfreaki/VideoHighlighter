"""
Tests for modules/debug_console.py's daily rotation, 7-day retention, and
the analysis-in-progress counter that defers rotation while a video
analysis is running.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from modules import debug_console


@pytest.fixture(autouse=True)
def _reset_debug_console_state():
    """debug_console holds module-level singleton state; reset it around
    every test so tests don't leak into each other."""
    saved = {
        "_installed": debug_console._installed,
        "_log_fh": debug_console._log_fh,
        "_is_child": debug_console._is_child,
        "_analysis_counter": debug_console._analysis_counter,
        "_current_log_date": debug_console._current_log_date,
    }
    debug_console._installed = False
    debug_console._log_fh = None
    debug_console._is_child = False
    debug_console._analysis_counter = 0
    debug_console._current_log_date = None
    yield
    if debug_console._log_fh is not None:
        try:
            debug_console._log_fh.close()
        except Exception:
            pass
    for k, v in saved.items():
        setattr(debug_console, k, v)


def _no_op_log(_msg):
    pass


def test_mark_analysis_start_then_end_returns_counter_to_zero():
    debug_console.mark_analysis_start()
    assert debug_console._analysis_counter == 1
    debug_console.mark_analysis_end()
    assert debug_console._analysis_counter == 0


def test_nested_start_end_only_reaches_zero_after_outer_end():
    debug_console.mark_analysis_start()
    debug_console.mark_analysis_start()
    with patch.object(debug_console, "_maybe_rotate") as mock_rotate:
        debug_console.mark_analysis_end()
        assert debug_console._analysis_counter == 1
        mock_rotate.assert_not_called()
        debug_console.mark_analysis_end()
        assert debug_console._analysis_counter == 0
        mock_rotate.assert_called_once()


def test_maybe_rotate_rotates_when_day_changed_and_idle(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text("old content\n", encoding="utf-8")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    old_time = time.time() - 86400
    os.utime(str(log_path), (old_time, old_time))

    debug_console._maybe_rotate(str(log_path))

    assert debug_console._current_log_date == date.today().isoformat()
    rotated = tmp_path / f"debug-{yesterday}.log"
    assert rotated.exists()
    assert rotated.read_text(encoding="utf-8") == "old content\n"
    assert not log_path.exists()


def test_maybe_rotate_no_rotate_when_same_day(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text("today's content\n", encoding="utf-8")
    # mtime defaults to "now" -> today

    debug_console._maybe_rotate(str(log_path))

    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == "today's content\n"


def test_maybe_rotate_deferred_while_analysis_in_progress(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text("content\n", encoding="utf-8")
    old_time = time.time() - 86400
    os.utime(str(log_path), (old_time, old_time))
    debug_console._analysis_counter = 1

    debug_console._maybe_rotate(str(log_path))

    # Still there, untouched -- rotation was deferred.
    assert log_path.exists()
    assert debug_console._current_log_date is None


def test_maybe_rotate_skipped_for_multiprocessing_child(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text("content\n", encoding="utf-8")
    old_time = time.time() - 86400
    os.utime(str(log_path), (old_time, old_time))
    debug_console._is_child = True

    debug_console._maybe_rotate(str(log_path))

    assert log_path.exists()
    assert debug_console._current_log_date is None


def test_prune_deletes_files_older_than_retention_window_keeps_recent(tmp_path):
    old_file = tmp_path / "debug-2020-01-01.log"
    recent_file = tmp_path / "debug-2020-06-01.log"
    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("recent", encoding="utf-8")

    old_time = time.time() - (10 * 86400)
    recent_time = time.time() - (2 * 86400)
    os.utime(str(old_file), (old_time, old_time))
    os.utime(str(recent_file), (recent_time, recent_time))

    debug_console._prune_old_logs(str(tmp_path))

    assert not old_file.exists()
    assert recent_file.exists()


def test_fresh_process_state_starts_with_zero_counter():
    # A crash mid-analysis in a prior process cannot leave a stale in-memory
    # flag -- module state always starts at 0 (nothing persists it).
    assert debug_console._analysis_counter == 0


def test_mid_session_rotation_closes_and_reopens_handle_without_permission_error(tmp_path):
    """Regression: renaming a file with an open write handle in the same
    process raises PermissionError on Windows. Rotation must close the
    handle before renaming and reopen a fresh one afterward."""
    log_path = tmp_path / "debug.log"

    debug_console._log_fh = open(str(log_path), "a", encoding="utf-8", errors="replace", buffering=1)
    debug_console._log_fh.write("yesterday's content\n")
    debug_console._log_fh.flush()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    debug_console._current_log_date = yesterday

    debug_console._maybe_rotate(str(log_path))  # must not raise

    assert debug_console._current_log_date == date.today().isoformat()
    assert debug_console._log_fh is not None

    rotated_path = tmp_path / f"debug-{yesterday}.log"
    assert rotated_path.exists()
    assert "yesterday's content" in rotated_path.read_text(encoding="utf-8")

    debug_console._log_fh.write("today's content\n")
    debug_console._log_fh.close()
    assert "today's content" in log_path.read_text(encoding="utf-8")
    debug_console._log_fh = None


def test_rotation_failure_is_logged_and_swallowed_not_raised(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text("content\n", encoding="utf-8")
    old_time = time.time() - 86400
    os.utime(str(log_path), (old_time, old_time))

    with patch("os.replace", side_effect=OSError("simulated locked file")):
        debug_console._maybe_rotate(str(log_path))  # must not raise


def test_true_concurrent_start_end_never_leaves_counter_negative(tmp_path):
    errors = []

    def worker():
        try:
            debug_console.mark_analysis_start()
            time.sleep(0.001)
            debug_console.mark_analysis_end()
        except Exception as e:
            errors.append(e)

    with patch.object(debug_console, "_maybe_rotate"):
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors
    assert debug_console._analysis_counter == 0
