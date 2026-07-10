"""
Tests for VideoAnalysisCache's partial-state persistence (U1).

modules/video_cache.py has zero heavy dependencies (stdlib only), so it is
tested directly against real files rather than through pipeline.py's heavy
shim chain -- this is the repo's established pattern for testing extracted,
dependency-light seams (see tests/test_progress_tracker_timing.py for the
sibling pattern applied to ProgressTracker).
"""

from __future__ import annotations

import time

from modules.video_cache import (
    VideoAnalysisCache,
    CHECKPOINTED_STAGES,
    resolve_completed_stages,
)


def _make_video(tmp_path, name="video.mp4", content=b"fake video bytes"):
    video_path = tmp_path / name
    video_path.write_bytes(content)
    return str(video_path)


def _cache(tmp_path):
    return VideoAnalysisCache(cache_dir=str(tmp_path / "cache"))


def test_partial_save_then_load_partial_round_trips_completed_stages(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)
    params = {"yolo_model_size": "n"}

    cache.save(
        video_path,
        {"transcript": {"segments": []}},
        params=params,
        complete=False,
        completed_stages=["transcript", "motion"],
    )

    result = cache.load_partial(video_path, params=params)

    assert result is not None
    assert result["completed_stages"] == ["transcript", "motion"]
    assert result["cache_complete"] is False


def test_default_complete_save_still_round_trips_through_load(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)
    params = {"yolo_model_size": "n"}

    cache.save(video_path, {"transcript": {"segments": []}}, params=params)

    result = cache.load(video_path, params=params)

    assert result is not None
    assert result["cache_complete"] is True
    assert "completed_stages" not in result


def test_load_partial_with_no_cache_file_returns_none(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)

    assert cache.load_partial(video_path, params={"yolo_model_size": "n"}) is None


def test_load_partial_with_mismatched_signature_returns_none(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)

    cache.save(
        video_path,
        {},
        params={"yolo_model_size": "n"},
        complete=False,
        completed_stages=["transcript"],
    )

    result = cache.load_partial(video_path, params={"yolo_model_size": "s"})

    assert result is None


def test_load_partial_reads_a_fully_complete_cache_too(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)
    params = {"yolo_model_size": "n"}

    cache.save(video_path, {"transcript": {"segments": []}}, params=params)

    result = cache.load_partial(video_path, params=params)

    assert result is not None
    assert result["cache_complete"] is True


# ---------------------------------------------------------------------------
# resolve_completed_stages (U2/U4 resume-decision logic, tested in isolation
# per the plan's U5 instruction rather than by driving _run_highlighter_impl)
# ---------------------------------------------------------------------------

def test_resolve_completed_stages_full_cache_hit_marks_every_checkpointed_stage():
    completed, full_hit = resolve_completed_stages(cache_is_complete=True, on_disk_completed_stages=None)

    assert full_hit is True
    assert completed == set(CHECKPOINTED_STAGES)


def test_resolve_completed_stages_partial_checkpoint_resumes_at_first_incomplete_stage():
    # AE1: checkpointed through motion, interrupted mid-audio_peaks
    completed, full_hit = resolve_completed_stages(
        cache_is_complete=False, on_disk_completed_stages=["transcript", "motion"]
    )

    assert full_hit is False
    assert completed == {"transcript", "motion"}
    remaining = [s for s in CHECKPOINTED_STAGES if s not in completed]
    assert remaining[0] == "audio_peaks"


def test_resolve_completed_stages_ignores_unrecognized_stage_names():
    # Defensive intersection: "trim" is never a checkpointed stage, and an
    # unrecognized future stage name on disk should not be trusted blindly.
    completed, full_hit = resolve_completed_stages(
        cache_is_complete=False, on_disk_completed_stages=["trim", "transcript", "some_future_stage"]
    )

    assert full_hit is False
    assert completed == {"transcript"}


def test_resolve_completed_stages_with_no_on_disk_stages_resumes_nothing():
    completed, full_hit = resolve_completed_stages(cache_is_complete=False, on_disk_completed_stages=None)

    assert full_hit is False
    assert completed == set()


# ---------------------------------------------------------------------------
# Time-range identity stability (KTD3 fix) and batch independence (KTD7)
# ---------------------------------------------------------------------------

def test_checkpoint_identity_is_stable_across_a_simulated_re_trim(tmp_path):
    """A checkpoint keyed on the ORIGINAL video must still resolve after the
    re-trimmed temp file changes on disk -- re-trimming must never break resume
    for use_time_range videos (AE3)."""
    cache = _cache(tmp_path)
    original_video = _make_video(tmp_path, name="source.mp4")
    params = {"use_time_range": True, "range_start": 0, "range_end": 30}

    cache.save(
        original_video, {"transcript": {"segments": []}}, params=params,
        complete=False, completed_stages=["transcript", "motion"],
    )

    # Simulate Cancel + re-trim: a *different* file (the temp trimmed video) is
    # rewritten with a new mtime. The original source video is untouched.
    trimmed_video = tmp_path / "source_temp_trimmed.mp4"
    trimmed_video.write_bytes(b"first trim contents")
    time.sleep(0.01)
    trimmed_video.write_bytes(b"second trim contents, different mtime")

    result = cache.load_partial(original_video, params=params)

    assert result is not None
    assert result["completed_stages"] == ["transcript", "motion"]


def test_checkpoint_for_one_video_does_not_affect_anothers_resume_decision(tmp_path):
    cache = _cache(tmp_path)
    video_a = _make_video(tmp_path, name="a.mp4", content=b"video a")
    video_b = _make_video(tmp_path, name="b.mp4", content=b"video b")
    params = {"yolo_model_size": "n"}

    cache.save(
        video_a, {}, params=params, complete=False,
        completed_stages=["transcript", "motion", "audio_peaks"],
    )

    assert cache.load_partial(video_b, params=params) is None
    result_a = cache.load_partial(video_a, params=params)
    assert result_a["completed_stages"] == ["transcript", "motion", "audio_peaks"]
