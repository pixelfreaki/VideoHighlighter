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


# ---------------------------------------------------------------------------
# Advanced keyword scoring signature coverage (U2) -- match-affecting settings
# only, weight excluded (KTD5)
# ---------------------------------------------------------------------------

def _advanced_scoring_config(**overrides):
    config = {
        "enabled": True,
        "prevent_overlapping_matches": True,
        "cooldown_seconds": 5,
        "normalization": {"lowercase": True, "remove_accents": True,
                           "remove_punctuation": True, "collapse_whitespace": True},
        "groups": [{"id": "panic", "weight": 15, "words": ["vou morrer"]}],
    }
    config.update(overrides)
    return config


def test_advanced_scoring_enabled_flag_changes_signature():
    disabled = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(enabled=False)}},
        sample_rate=5, video_duration=60.0,
    )
    enabled = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(enabled=True)}},
        sample_rate=5, video_duration=60.0,
    )

    assert disabled != enabled


def test_advanced_scoring_word_list_change_changes_signature():
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config()}},
        sample_rate=5, video_duration=60.0,
    )
    changed_words = _advanced_scoring_config()
    changed_words["groups"][0]["words"] = ["morri"]
    other = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": changed_words}},
        sample_rate=5, video_duration=60.0,
    )

    assert base != other


def test_advanced_scoring_weight_only_change_does_not_change_signature():
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config()}},
        sample_rate=5, video_duration=60.0,
    )
    changed_weight = _advanced_scoring_config()
    changed_weight["groups"][0]["weight"] = 999
    other = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": changed_weight}},
        sample_rate=5, video_duration=60.0,
    )

    assert base == other


def test_advanced_scoring_yaml_reordering_does_not_change_signature():
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(
            groups=[
                {"id": "reaction", "weight": 6, "words": ["caraca", "bugou"]},
                {"id": "panic", "weight": 15, "words": ["vou morrer"]},
            ],
        )}},
        sample_rate=5, video_duration=60.0,
    )
    reordered = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(
            groups=[
                {"id": "panic", "weight": 15, "words": ["vou morrer"]},
                {"id": "reaction", "weight": 6, "words": ["bugou", "caraca"]},
            ],
        )}},
        sample_rate=5, video_duration=60.0,
    )

    assert base == reordered


def test_advanced_scoring_absent_and_explicitly_disabled_produce_same_signature():
    absent = build_analysis_cache_params({}, {}, sample_rate=5, video_duration=60.0)
    disabled_with_leftover_groups = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(enabled=False)}},
        sample_rate=5, video_duration=60.0,
    )

    assert absent["advanced_scoring"] == disabled_with_leftover_groups["advanced_scoring"]


def test_advanced_scoring_cooldown_seconds_change_changes_signature():
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(cooldown_seconds=5)}},
        sample_rate=5, video_duration=60.0,
    )
    changed = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(cooldown_seconds=30)}},
        sample_rate=5, video_duration=60.0,
    )

    assert base != changed


def test_advanced_scoring_prevent_overlapping_matches_change_changes_signature():
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(prevent_overlapping_matches=True)}},
        sample_rate=5, video_duration=60.0,
    )
    changed = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(prevent_overlapping_matches=False)}},
        sample_rate=5, video_duration=60.0,
    )

    assert base != changed


def test_advanced_scoring_each_normalization_flag_change_changes_signature():
    base_normalization = {"lowercase": True, "remove_accents": True,
                           "remove_punctuation": True, "collapse_whitespace": True}
    base = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(normalization=base_normalization)}},
        sample_rate=5, video_duration=60.0,
    )

    for flag in base_normalization:
        flipped = dict(base_normalization)
        flipped[flag] = not flipped[flag]
        other = build_analysis_cache_params(
            {}, {"keywords": {"advanced_scoring": _advanced_scoring_config(normalization=flipped)}},
            sample_rate=5, video_duration=60.0,
        )
        assert base != other, f"flipping normalization.{flag} did not change the signature"


def test_advanced_scoring_signature_defaults_match_keyword_scoring_defaults():
    """Drift guard: modules/video_cache.py duplicates modules/keyword_scoring.py's
    normalization/cooldown/overlap defaults (by design, to avoid the whisper
    dependency -- see the comment above _advanced_scoring's config read in
    video_cache.py). Pin the literal values here so a future default change in
    one file without the other fails a test instead of silently drifting."""
    params = build_analysis_cache_params(
        {}, {"keywords": {"advanced_scoring": {"enabled": True, "groups": []}}},
        sample_rate=5, video_duration=60.0,
    )

    assert params["advanced_scoring"]["normalization"] == {
        "lowercase": True, "remove_accents": True,
        "remove_punctuation": True, "collapse_whitespace": True,
    }
    assert params["advanced_scoring"]["prevent_overlapping_matches"] is True
    assert params["advanced_scoring"]["cooldown_seconds"] == 5.0


# ---------------------------------------------------------------------------
# R15: signature coverage against a realistic config shape (U4) -- proves the
# nested config.get("keywords", {}).get("advanced_scoring", {}) read (KTD2)
# actually reaches the section inside a full config.yaml-shaped document with
# sibling sections and an unrelated "keywords.interesting" list, and that a
# realistic, fully-populated gui_config (resembling main.py's
# build_pipeline_config() output, which never carries a "keywords" key at all)
# doesn't interfere. Not a restatement of U2's isolated-dict tests above.
# ---------------------------------------------------------------------------

def _realistic_gui_config(**overrides):
    # Mirrors the flat shape main.py's build_pipeline_config() actually returns --
    # no "keywords" key anywhere, since advanced_scoring has no GUI in this pass.
    gui_config = {
        "scene_points": 0, "motion_event_points": 2, "motion_peak_points": 4,
        "audio_peak_points": 3, "keyword_points": 8, "transcript_points": 1,
        "beginning_points": 0, "ending_points": 0, "object_points": 4, "action_points": 6,
        "clip_time": 240, "max_duration": 420, "multi_signal_boost": 1.2,
        "min_signals_for_boost": 2, "keep_temp": True,
        "highlight_objects": ["boss", "explosion"], "interesting_actions": ["killing boss"],
        "actions_require_objects": False, "use_transcript": True, "transcript_model": "base",
        "transcript_source_lang": "en", "search_keywords": ["estou morta", "vou morrer"],
        "create_subtitles": False, "sample_rate": 5, "force_reprocess": False,
    }
    gui_config.update(overrides)
    return gui_config


def _realistic_yaml_config(**advanced_scoring_overrides):
    # Mirrors config/config.yaml's real top-level shape: sibling sections plus a
    # pre-existing, unrelated "keywords.interesting" list alongside advanced_scoring.
    return {
        "video": {"paths": ["C:/videos/clip.mp4"]},
        "download": {"save_dir": "D:/movies", "auto_add": True},
        "highlights": {"clip_time": 240, "max_duration": 420, "keep_temp": True},
        "scoring": {"scene_points": 0, "keyword_points": 8, "object_points": 4},
        "actions": {"interesting": ["killing boss"], "require_objects": False},
        "objects": {"interesting": ["boss", "explosion"], "confidence": 30},
        "keywords": {
            "transcript_file": "transcript.txt",
            "interesting": ["estou morta", "vou morrer", "ai não"],
            "advanced_scoring": _advanced_scoring_config(**advanced_scoring_overrides),
        },
    }


def test_realistic_config_shape_reaches_advanced_scoring_section():
    params = build_analysis_cache_params(
        _realistic_gui_config(), _realistic_yaml_config(), sample_rate=5, video_duration=120.0,
    )

    assert params["advanced_scoring"]["enabled"] is True
    assert params["advanced_scoring"]["groups"][0]["id"] == "panic"


def test_realistic_config_shape_word_list_change_still_changes_signature():
    base = build_analysis_cache_params(
        _realistic_gui_config(), _realistic_yaml_config(), sample_rate=5, video_duration=120.0,
    )
    changed = build_analysis_cache_params(
        _realistic_gui_config(),
        _realistic_yaml_config(groups=[{"id": "panic", "weight": 15, "words": ["morri"]}]),
        sample_rate=5, video_duration=120.0,
    )

    assert base != changed


def test_realistic_config_shape_with_advanced_scoring_absent_is_disabled():
    yaml_config = _realistic_yaml_config()
    del yaml_config["keywords"]["advanced_scoring"]

    params = build_analysis_cache_params(
        _realistic_gui_config(), yaml_config, sample_rate=5, video_duration=120.0,
    )

    assert params["advanced_scoring"] == {"enabled": False}
