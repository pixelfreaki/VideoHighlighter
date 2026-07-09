"""
Tests for ProgressTracker's per-stage duration tracking.

Covers pipeline.py's ProgressTracker.start_stage/end_stage — an explicit
stage-boundary API rather than inferring durations from task_name transitions,
since task_name is "Pipeline" for most real stages and several stages bypass
ProgressTracker's update_progress entirely.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def _shim_heavy_for_pipeline_import() -> None:
    for name in (
        "action_recognition",
        "object_recognition",
        "modules.audio_peaks",
        "modules.motion_scene_detect_optimized",
        "modules.video_cache",
        "modules.video_cutter",
        "modules.transcript",
        "modules.transcript_srt",
    ):
        sys.modules.setdefault(name, MagicMock())


@pytest.fixture(scope="module", autouse=True)
def _prepare_pipeline_imports():
    _shim_heavy_for_pipeline_import()


def test_start_then_end_records_a_nonnegative_duration():
    from pipeline import ProgressTracker
    tracker = ProgressTracker()

    tracker.start_stage("motion")
    tracker.end_stage("motion")

    assert "motion" in tracker.stage_durations
    assert tracker.stage_durations["motion"] >= 0


def test_end_stage_without_matching_start_does_not_crash():
    from pipeline import ProgressTracker
    tracker = ProgressTracker()

    tracker.end_stage("never_started")  # must not raise

    assert "never_started" not in tracker.stage_durations


def test_double_start_without_intervening_end_uses_latest_start():
    from pipeline import ProgressTracker
    import time

    tracker = ProgressTracker()
    tracker.start_stage("object_detection")
    first_start = tracker._open_stage_starts["object_detection"]
    time.sleep(0.01)
    tracker.start_stage("object_detection")
    second_start = tracker._open_stage_starts["object_detection"]

    assert second_start > first_start

    tracker.end_stage("object_detection")
    # Duration measured from the second (most recent) start, not the first.
    assert tracker.stage_durations["object_detection"] < (time.time() - first_start)


def test_multiple_stages_tracked_independently():
    from pipeline import ProgressTracker
    tracker = ProgressTracker()

    tracker.start_stage("trim")
    tracker.end_stage("trim")
    tracker.start_stage("transcript")
    tracker.end_stage("transcript")

    assert set(tracker.stage_durations.keys()) == {"trim", "transcript"}


def test_record_stage_device_stores_device_by_name():
    from pipeline import ProgressTracker
    tracker = ProgressTracker()

    tracker.record_stage_device("transcript", "cuda:0")

    assert tracker.stage_devices["transcript"] == "cuda:0"


def test_progress_tracker_still_works_without_gui_callback():
    # Regression: existing "works with or without GUI callback" resilience.
    from pipeline import ProgressTracker
    tracker = ProgressTracker()
    tracker.update_progress(1, 10, "Pipeline", "Initializing...")  # must not raise
    tracker.start_stage("trim")
    tracker.end_stage("trim")  # must not raise either
