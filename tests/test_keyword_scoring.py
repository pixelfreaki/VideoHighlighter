"""
Tests for modules/keyword_scoring.py -- the Advanced Keyword Scoring engine (U1).

modules/keyword_scoring.py has zero heavy dependencies (stdlib only, plus
modules.transcript for the simple-mode wrapper -- also heavy-dep-free), so it
is tested directly, following this repo's established pattern for testing
extracted, dependency-light seams (see tests/test_video_cache.py,
tests/test_progress_tracker_timing.py).
"""

from __future__ import annotations

from modules.keyword_scoring import (
    normalize_text,
    validate_advanced_scoring_config,
    match_keywords_advanced,
    match_keywords_simple,
    resolve_keyword_scoring,
)
from modules.transcript import search_transcript_for_keywords


def _seg(text, start, end):
    return {"text": text, "start": start, "end": end}


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------

def test_normalize_text_all_flags_on():
    assert normalize_text("AI, NÃO!") == "ai nao"


def test_normalize_text_lowercase_flag_off():
    result = normalize_text("AI, NÃO!", lowercase=False, remove_accents=True,
                             remove_punctuation=True, collapse_whitespace=True)
    assert result == "AI NAO"


def test_normalize_text_remove_accents_flag_off():
    result = normalize_text("AI, NÃO!", lowercase=True, remove_accents=False,
                             remove_punctuation=True, collapse_whitespace=True)
    assert result == "ai não"


def test_normalize_text_remove_punctuation_flag_off():
    result = normalize_text("AI, NÃO!", lowercase=True, remove_accents=True,
                             remove_punctuation=False, collapse_whitespace=True)
    assert "," in result or "!" in result


def test_normalize_text_collapse_whitespace_flag_off():
    result = normalize_text("ai   nao", lowercase=True, remove_accents=True,
                             remove_punctuation=True, collapse_whitespace=False)
    assert result == "ai   nao"


def test_normalize_text_empty_input():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


# ---------------------------------------------------------------------------
# match_keywords_advanced
# ---------------------------------------------------------------------------

def _two_group_config(**overrides):
    config = {
        "enabled": True,
        "prevent_overlapping_matches": True,
        "cooldown_seconds": 5,
        "normalization": {
            "lowercase": True, "remove_accents": True,
            "remove_punctuation": True, "collapse_whitespace": True,
        },
        "groups": [
            {"id": "reaction", "weight": 6, "words": ["caraca"]},
            {"id": "panic", "weight": 15, "words": ["vou morrer"]},
        ],
    }
    config.update(overrides)
    return config


def test_match_keywords_advanced_attributes_group_and_weight():
    segments = [
        _seg("caraca que isso", 10.0, 12.0),
        _seg("vou morrer agora", 20.0, 22.0),
    ]
    matches, skips = match_keywords_advanced(segments, _two_group_config())

    by_keyword = {m["keyword"]: m for m in matches}
    assert by_keyword["caraca"]["group"] == "reaction"
    assert by_keyword["caraca"]["weight"] == 6
    assert by_keyword["vou morrer"]["group"] == "panic"
    assert by_keyword["vou morrer"]["weight"] == 15
    assert all(m["scoring_mode"] == "advanced" for m in matches)


def test_match_keywords_advanced_complete_word_matching():
    config = _two_group_config(groups=[{"id": "g", "weight": 10, "words": ["morri"]}])

    matches_hit, _ = match_keywords_advanced([_seg("eu morri", 0.0, 1.0)], config)
    matches_miss, _ = match_keywords_advanced([_seg("cachorrinho", 0.0, 1.0)], config)

    assert len(matches_hit) == 1
    assert len(matches_miss) == 0


def test_match_keywords_advanced_overlap_longest_wins():
    config = _two_group_config(groups=[
        {"id": "g", "weight": 10, "words": ["meu deus", "ai meu deus"]},
    ])

    matches, skips = match_keywords_advanced(
        [_seg("ai meu deus", 0.0, 1.0)], config,
    )

    assert len(matches) == 1
    assert matches[0]["keyword"] == "ai meu deus"
    assert any(s["reason"] == "overlap" for s in skips)


def test_match_keywords_advanced_cooldown_suppresses_repeats():
    config = _two_group_config(groups=[{"id": "g", "weight": 10, "words": ["morri"]}],
                                cooldown_seconds=5)
    segments = [
        _seg("morri", 0.0, 1.0),
        _seg("morri", 1.0, 2.0),
        _seg("morri", 2.0, 3.0),
        _seg("morri", 3.0, 4.0),
    ]

    matches, skips = match_keywords_advanced(segments, config)

    assert len(matches) == 1
    assert sum(1 for s in skips if s["reason"] == "cooldown") == 3


def test_match_keywords_advanced_different_keywords_score_independently_in_cooldown():
    config = _two_group_config(
        groups=[{"id": "g", "weight": 10, "words": ["morri", "caraca"]}],
        cooldown_seconds=5,
    )
    segments = [_seg("morri", 0.0, 1.0), _seg("caraca", 0.5, 1.5)]

    matches, skips = match_keywords_advanced(segments, config)

    assert len(matches) == 2


def test_score_by_second_takes_max_not_sum_across_groups():
    segments = [_seg("caraca vou morrer", 10.0, 11.0)]
    result = resolve_keyword_scoring(
        segments,
        gui_config={},
        config={"keywords": {"advanced_scoring": _two_group_config()}},
    )

    assert result["score_by_second"][10] == 15  # max(6, 15), not 21


# ---------------------------------------------------------------------------
# validate_advanced_scoring_config
# ---------------------------------------------------------------------------

def test_validate_rejects_duplicate_group_ids():
    config = {"groups": [
        {"id": "a", "weight": 1, "words": ["x"]},
        {"id": "a", "weight": 2, "words": ["y"]},
    ]}
    errors = validate_advanced_scoring_config(config)
    assert any("duplicate group id" in e for e in errors)


def test_validate_rejects_negative_weight():
    config = {"groups": [{"id": "a", "weight": -1, "words": ["x"]}]}
    errors = validate_advanced_scoring_config(config)
    assert any("negative weight" in e for e in errors)


def test_validate_rejects_empty_keyword_enabled_group():
    config = {"groups": [{"id": "a", "weight": 1, "words": [], "enabled": True}]}
    errors = validate_advanced_scoring_config(config)
    assert any("no non-blank keywords" in e for e in errors)


def test_validate_allows_empty_keyword_disabled_group():
    config = {"groups": [{"id": "a", "weight": 1, "words": [], "enabled": False}]}
    errors = validate_advanced_scoring_config(config)
    assert errors == []


def test_validate_rejects_duplicate_keyword_within_group():
    config = {"groups": [{"id": "a", "weight": 1, "words": ["morri", "Morri"]}]}
    errors = validate_advanced_scoring_config(config)
    assert any("duplicate normalized keyword" in e and "within group" in e for e in errors)


def test_validate_rejects_duplicate_keyword_across_groups():
    config = {"groups": [
        {"id": "danger", "weight": 1, "words": ["morri"]},
        {"id": "panic", "weight": 2, "words": ["Morri"]},
    ]}
    errors = validate_advanced_scoring_config(config)
    assert any("duplicate normalized keyword" in e and "danger" in e and "panic" in e for e in errors)


# ---------------------------------------------------------------------------
# resolve_keyword_scoring
# ---------------------------------------------------------------------------

def test_resolve_keyword_scoring_enabled_and_invalid_returns_errors_no_matches():
    result = resolve_keyword_scoring(
        [_seg("caraca", 0.0, 1.0)],
        gui_config={},
        config={"keywords": {"advanced_scoring": {"enabled": True, "groups": []}}},
    )

    assert result["mode"] == "advanced"
    assert result["matches"] == []
    assert result["validation_errors"] != []


def test_resolve_keyword_scoring_disabled_and_broken_config_still_works_simple():
    result = resolve_keyword_scoring(
        [_seg("test keyword here", 0.0, 1.0)],
        gui_config={"search_keywords": ["keyword"], "keyword_points": 8},
        config={"keywords": {"advanced_scoring": {"enabled": False, "groups": "not even a list"}}},
    )

    assert result["mode"] == "simple"
    assert result["validation_errors"] == []
    assert len(result["matches"]) == 1


def test_resolve_keyword_scoring_absent_advanced_scoring_key_is_simple_mode():
    result = resolve_keyword_scoring(
        [_seg("test keyword here", 0.0, 1.0)],
        gui_config={"search_keywords": ["keyword"], "keyword_points": 8},
        config={},
    )

    assert result["mode"] == "simple"


def test_resolve_keyword_scoring_simple_mode_matches_search_transcript_for_keywords_baseline():
    segments = [_seg("estou morta agora", 5.0, 6.0), _seg("nada aqui", 10.0, 11.0)]
    search_keywords = ["estou morta"]
    keyword_points = 8

    result = resolve_keyword_scoring(
        segments,
        gui_config={"search_keywords": search_keywords, "keyword_points": keyword_points},
        config={},
    )

    baseline_raw = search_transcript_for_keywords(segments, search_keywords)
    baseline_seconds = {int(m["main_segment"]["start"]) for m in baseline_raw}

    result_seconds = set(result["score_by_second"].keys())
    assert result_seconds == baseline_seconds
    for sec in result_seconds:
        assert result["score_by_second"][sec] == keyword_points


# ---------------------------------------------------------------------------
# match_keywords_simple
# ---------------------------------------------------------------------------

def test_match_keywords_simple_wraps_existing_function_unmodified():
    segments = [_seg("vou morrer agora", 1.0, 2.0)]
    matches = match_keywords_simple(segments, ["vou morrer"], keyword_points=8)

    assert len(matches) == 1
    assert matches[0]["scoring_mode"] == "simple"
    assert matches[0]["weight"] == 8
    assert matches[0]["group"] is None
