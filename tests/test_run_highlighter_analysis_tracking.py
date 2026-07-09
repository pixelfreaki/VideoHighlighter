"""
Tests for pipeline.py's run_highlighter() wrapper bracketing every real run
with modules.debug_console's mark_analysis_start()/mark_analysis_end(),
across every exit path (batch, single-file success, cancellation, exception).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

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


def test_single_run_calls_start_and_end_exactly_once():
    import pipeline

    with patch.object(pipeline, "_run_highlighter_impl", return_value="output.mp4") as mock_impl, \
         patch("modules.debug_console.mark_analysis_start") as mock_start, \
         patch("modules.debug_console.mark_analysis_end") as mock_end:
        result = pipeline.run_highlighter("video.mp4")

    assert result == "output.mp4"
    mock_impl.assert_called_once()
    mock_start.assert_called_once()
    mock_end.assert_called_once()


def test_exception_during_run_still_calls_mark_analysis_end():
    import pipeline

    with patch.object(pipeline, "_run_highlighter_impl", side_effect=RuntimeError("boom")), \
         patch("modules.debug_console.mark_analysis_start") as mock_start, \
         patch("modules.debug_console.mark_analysis_end") as mock_end:
        with pytest.raises(RuntimeError):
            pipeline.run_highlighter("video.mp4")

    mock_start.assert_called_once()
    mock_end.assert_called_once()


def test_cancellation_path_still_calls_mark_analysis_end():
    import pipeline

    # _run_highlighter_impl itself catches RuntimeError internally and
    # returns None on cancellation -- exercise that as a normal return.
    with patch.object(pipeline, "_run_highlighter_impl", return_value=None), \
         patch("modules.debug_console.mark_analysis_start") as mock_start, \
         patch("modules.debug_console.mark_analysis_end") as mock_end:
        result = pipeline.run_highlighter("video.mp4", cancel_flag=object())

    assert result is None
    mock_start.assert_called_once()
    mock_end.assert_called_once()


def test_recursive_batch_style_calls_keep_counter_above_zero_until_all_finish():
    # The real batch branch (inside _run_highlighter_impl, unchanged by this
    # plan) recursively calls the public run_highlighter() wrapper once per
    # video (pipeline.py:410). Simulate that recursion pattern directly
    # against the wrapper, without needing the real batch/config machinery,
    # to prove the counter nests correctly (KTD1).
    import pipeline
    from modules import debug_console

    counter_snapshots = []

    def fake_impl(video_path, *args, **kwargs):
        if video_path == "outer_batch":
            for v in ("a.mp4", "b.mp4", "c.mp4"):
                pipeline.run_highlighter(v)
            return "batch_done"
        # Probe the real counter mid-batch: the outer wrapper call has
        # already incremented once, and this video's own recursive wrapper
        # call increments again before reaching here.
        counter_snapshots.append(debug_console._analysis_counter)
        return f"{video_path}_highlight.mp4"

    saved_counter = debug_console._analysis_counter
    debug_console._analysis_counter = 0
    try:
        with patch.object(pipeline, "_run_highlighter_impl", side_effect=fake_impl):
            result = pipeline.run_highlighter("outer_batch")
    finally:
        debug_console._analysis_counter = saved_counter

    assert result == "batch_done"
    assert len(counter_snapshots) == 3
    # Counter was >= 2 during each per-video call: 1 for the outer batch
    # wrapper + 1 for that video's own recursive wrapper call.
    assert all(c >= 2 for c in counter_snapshots)
    assert debug_console._analysis_counter == saved_counter
