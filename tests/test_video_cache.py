"""
Tests for VideoAnalysisCache's partial-state persistence (U1).

modules/video_cache.py has zero heavy dependencies (stdlib only), so it is
tested directly against real files rather than through pipeline.py's heavy
shim chain -- this is the repo's established pattern for testing extracted,
dependency-light seams (see tests/test_progress_tracker_timing.py for the
sibling pattern applied to ProgressTracker).
"""

from __future__ import annotations

from modules.video_cache import VideoAnalysisCache


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
