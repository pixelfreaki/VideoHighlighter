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
    build_analysis_cache_params,
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


def test_sequential_stage_checkpoints_accumulate_and_each_is_durable(tmp_path):
    """Mirrors _checkpoint_stage's real call pattern: one save per completed
    stage, with completed_stages re-supplied as the accumulated list so far.
    Also stands in for Cancel-parity (KTD-Cancel): stopping after any of these
    saves leaves a valid, correctly-ordered partial state -- there is no
    separate code path for "cancelled here" vs "crashed here"."""
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)
    params = {"yolo_model_size": "n"}

    for stages_so_far in (["transcript"], ["transcript", "motion"], ["transcript", "motion", "audio_peaks"]):
        cache.save(video_path, {}, params=params, complete=False, completed_stages=stages_so_far)
        result = cache.load_partial(video_path, params=params)
        assert result["completed_stages"] == stages_so_far


def test_setting_outside_analysis_params_does_not_break_resume(tmp_path):
    """AE4: a setting that only affects an out-of-checkpoint-scope stage (e.g.
    MAX_DURATION affecting score_computation/video_cutting) is not part of
    analysis_params, so changing it must not change the signature -- the
    checkpointed stages resume normally regardless."""
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)

    # analysis_params has no "max_duration" key (build_analysis_cache_params
    # never includes it), so two "runs" with a different MAX_DURATION setting
    # still produce the identical params dict here.
    params_run_1 = {"yolo_model_size": "n", "use_transcript": True}
    params_run_2 = {"yolo_model_size": "n", "use_transcript": True}

    cache.save(
        video_path, {}, params=params_run_1, complete=False,
        completed_stages=["transcript", "motion", "audio_peaks", "object_detection", "action_detection"],
    )

    result = cache.load_partial(video_path, params=params_run_2)

    assert result is not None
    assert result["completed_stages"] == [
        "transcript", "motion", "audio_peaks", "object_detection", "action_detection",
    ]


# ---------------------------------------------------------------------------
# scene/motion point-settings signature coverage (code-review fix: these gate
# whether the checkpointed "motion" stage computes anything at all, so a
# resume must not silently reuse an empty motion checkpoint after the user
# raises them from 0)
# ---------------------------------------------------------------------------

def test_analysis_params_changes_when_motion_point_settings_change():
    base_config = {"scene_points": 0, "motion_event_points": 0, "motion_peak_points": 0}
    raised_config = {"scene_points": 5, "motion_event_points": 0, "motion_peak_points": 0}

    base_params = build_analysis_cache_params(base_config, {}, sample_rate=5, video_duration=60.0)
    raised_params = build_analysis_cache_params(raised_config, {}, sample_rate=5, video_duration=60.0)

    assert base_params != raised_params
    assert base_params["scene_points"] == 0
    assert raised_params["scene_points"] == 5


def test_motion_skipped_checkpoint_does_not_resume_under_raised_point_settings(tmp_path):
    cache = _cache(tmp_path)
    video_path = _make_video(tmp_path)

    skipped_config = {"scene_points": 0, "motion_event_points": 0, "motion_peak_points": 0}
    raised_config = {"scene_points": 5, "motion_event_points": 0, "motion_peak_points": 0}
    params_skipped = build_analysis_cache_params(skipped_config, {}, sample_rate=5, video_duration=60.0)
    params_raised = build_analysis_cache_params(raised_config, {}, sample_rate=5, video_duration=60.0)

    cache.save(
        video_path, {}, params=params_skipped, complete=False,
        completed_stages=["transcript", "motion"],
    )

    # Raising the points settings must produce a different signature, so the
    # empty-motion checkpoint saved under the old settings is invisible here --
    # motion (and everything after it) reruns instead of silently resuming
    # with stale empty data.
    assert cache.load_partial(video_path, params=params_raised) is None
