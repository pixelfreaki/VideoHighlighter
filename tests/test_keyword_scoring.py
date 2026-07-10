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
    validate_advanced_scoring_config_structured,
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
    assert result == "ai, nao!"


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


def test_match_keywords_advanced_disabled_group_with_words_is_never_matched():
    config = _two_group_config(groups=[
        {"id": "g", "weight": 10, "words": ["morri"], "enabled": False},
    ])

    matches, skips = match_keywords_advanced([_seg("eu morri", 0.0, 1.0)], config)

    assert matches == []


def test_match_keywords_advanced_overlap_prevention_disabled_scores_both_phrases():
    config = _two_group_config(groups=[
        {"id": "g", "weight": 10, "words": ["meu deus", "ai meu deus"]},
    ], prevent_overlapping_matches=False)

    matches, skips = match_keywords_advanced([_seg("ai meu deus", 0.0, 1.0)], config)

    assert {m["keyword"] for m in matches} == {"meu deus", "ai meu deus"}
    assert not any(s["reason"] == "overlap" for s in skips)


def test_match_keywords_advanced_cooldown_skipped_longer_phrase_still_blocks_shorter_overlap():
    """R7: a longer phrase textually occupies its span even when it's itself
    cooldown-skipped -- a shorter overlapping keyword must not sneak through."""
    config = _two_group_config(groups=[
        {"id": "g", "weight": 10, "words": ["ai meu deus", "deus"]},
    ], cooldown_seconds=100)
    segments = [
        _seg("ai meu deus", 0.0, 1.0),
        _seg("ai meu deus", 10.0, 11.0),  # within cooldown -- "ai meu deus" itself skips
    ]

    matches, skips = match_keywords_advanced(segments, config)

    # Only the first "ai meu deus" match; the second segment's "ai meu deus" is
    # cooldown-skipped, and "deus" must be overlap-skipped (not scored) there too.
    assert len(matches) == 1
    assert matches[0]["start"] == 0.0
    second_segment_skips = [s for s in skips if s["start"] == 10.0]
    assert any(s["reason"] == "cooldown" and s["keyword"] == "ai meu deus" for s in second_segment_skips)
    assert any(s["reason"] == "overlap" and s["keyword"] == "deus" for s in second_segment_skips)


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


def test_validate_rejects_non_list_groups_instead_of_crashing():
    # A plausible YAML mistake (forgetting the list dash: "groups: my_group")
    # must not raise -- it must surface as a clean validation error (R13).
    errors = validate_advanced_scoring_config({"groups": "my_group"})
    assert any("must be a list" in e for e in errors)


def test_validate_rejects_non_numeric_cooldown_seconds():
    config = {"groups": [{"id": "a", "weight": 1, "words": ["x"]}], "cooldown_seconds": "oops"}
    errors = validate_advanced_scoring_config(config)
    assert any("cooldown_seconds must be numeric" in e for e in errors)


def test_validate_rejects_non_list_words_instead_of_silently_iterating_characters():
    config = {"groups": [{"id": "a", "weight": 1, "words": "chef"}]}
    errors = validate_advanced_scoring_config(config)
    assert any("must be a list" in e for e in errors)


def test_validate_rejects_missing_group_id():
    config = {"groups": [{"weight": 1, "words": ["x"]}]}
    errors = validate_advanced_scoring_config(config)
    assert any("has no id" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_advanced_scoring_config_structured (U1 of the GUI plan, R7):
# field-addressable errors the GUI can pin to group cards. Each entry carries
# group_index (None for config-level errors), field, and the exact message
# string the plain validator produces.
# ---------------------------------------------------------------------------

def test_validate_structured_valid_two_group_config_is_empty():
    config = _two_group_config()
    assert validate_advanced_scoring_config_structured(config) == []
    assert validate_advanced_scoring_config(config) == []


def test_validate_structured_blank_id_pins_group_index_and_id_field():
    config = {"groups": [
        {"id": "a", "weight": 1, "words": ["x"]},
        {"id": "   ", "weight": 2, "words": ["y"]},
    ]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 1
    assert entries[0]["field"] == "id"


def test_validate_structured_negative_weight_pins_weight_field():
    config = {"groups": [
        {"id": "a", "weight": 1, "words": ["x"]},
        {"id": "b", "weight": -1, "words": ["y"]},
    ]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 1
    assert entries[0]["field"] == "weight"


def test_validate_structured_non_numeric_weight_pins_weight_field():
    config = {"groups": [{"id": "a", "weight": "heavy", "words": ["x"]}]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 0
    assert entries[0]["field"] == "weight"


def test_validate_structured_enabled_group_without_words_pins_words_field():
    config = {"groups": [{"id": "a", "weight": 1, "words": [], "enabled": True}]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 0
    assert entries[0]["field"] == "words"


def test_validate_structured_duplicate_keyword_within_group_pins_words_field():
    config = {"groups": [{"id": "a", "weight": 1, "words": ["morri", "Morri"]}]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 0
    assert entries[0]["field"] == "words"


def test_validate_structured_duplicate_keyword_across_groups_pins_later_group():
    config = {"groups": [
        {"id": "danger", "weight": 1, "words": ["morri"]},
        {"id": "panic", "weight": 2, "words": ["Morri"]},
    ]}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] == 1  # the group where the duplicate was encountered
    assert entries[0]["field"] == "words"


def test_validate_structured_non_list_groups_is_config_level():
    entries = validate_advanced_scoring_config_structured({"groups": "my_group"})
    assert len(entries) == 1
    assert entries[0]["group_index"] is None
    assert entries[0]["field"] == "groups"


def test_validate_structured_non_numeric_cooldown_seconds_is_config_level():
    config = {"groups": [{"id": "a", "weight": 1, "words": ["x"]}],
              "cooldown_seconds": "oops"}
    entries = validate_advanced_scoring_config_structured(config)
    assert len(entries) == 1
    assert entries[0]["group_index"] is None
    assert entries[0]["field"] == "cooldown_seconds"


def test_validate_structured_string_api_parity_on_invalid_fixtures():
    # The plain validator must be exactly the structured messages, in order --
    # pipeline.py logs those strings and existing tests assert on them.
    invalid_fixtures = [
        {"groups": [{"id": "a", "weight": 1, "words": ["x"]},
                    {"id": "   ", "weight": 2, "words": ["y"]}]},
        {"groups": [{"id": "a", "weight": -1, "words": ["x"]}]},
        {"groups": [{"id": "a", "weight": "heavy", "words": ["x"]}]},
        {"groups": [{"id": "a", "weight": 1, "words": [], "enabled": True}]},
        {"groups": [{"id": "a", "weight": 1, "words": ["morri", "Morri"]}]},
        {"groups": [{"id": "danger", "weight": 1, "words": ["morri"]},
                    {"id": "panic", "weight": 2, "words": ["Morri"]}]},
        {"groups": "my_group"},
        {"groups": [{"id": "a", "weight": 1, "words": ["x"]}], "cooldown_seconds": "oops"},
        {"groups": []},
        {"groups": [{"id": "a", "weight": 1, "words": ["x"]},
                    {"id": "a", "weight": 2, "words": ["y"]}]},
        {"groups": [{"id": "a", "weight": 1, "words": "chef"}]},
        {"groups": ["not a mapping"]},
    ]
    for config in invalid_fixtures:
        structured = validate_advanced_scoring_config_structured(config)
        assert structured, f"fixture expected to be invalid: {config!r}"
        assert validate_advanced_scoring_config(config) == [e["message"] for e in structured]


def test_validate_structured_extra_label_key_is_not_an_error():
    config = {"groups": [{"id": "a", "weight": 1, "words": ["x"], "label": "Reaction"}]}
    assert validate_advanced_scoring_config_structured(config) == []


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
    baseline_seconds = set()
    for m in baseline_raw:
        seg = m["main_segment"]
        baseline_seconds.update(range(int(seg["start"]), int(seg["end"]) + 1))

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


# ---------------------------------------------------------------------------
# R1 (U4): resolve_keyword_scoring()'s matches carry enough information for a
# consumer to reconstruct pipeline.py's pre-feature per-second keyword score
# byte-for-byte. pipeline.py's old logic expanded each match's full
# [main_segment.start, main_segment.end] range to a flat KEYWORD_POINTS per
# second (deduped via a set); this proves that reconstruction still holds
# through resolve_keyword_scoring()'s reshaped "matches" list (not just its
# point-only score_by_second, which collapses to a single second per match
# and is not what pipeline.py's per-second scoring actually consumes).
# ---------------------------------------------------------------------------

def test_resolve_keyword_scoring_matches_reconstruct_pre_feature_per_second_score():
    segments = [
        _seg("estou morta agora mesmo", 5.0, 7.0),   # multi-second segment
        _seg("nada relevante aqui", 20.0, 20.5),
        _seg("vou morrer", 30.0, 30.0),
    ]
    search_keywords = ["estou morta", "vou morrer"]
    keyword_points = 8

    # Pre-feature baseline: pipeline.py's old keyword_set/KEYWORD_POINTS logic,
    # computed directly against search_transcript_for_keywords.
    baseline_raw = search_transcript_for_keywords(segments, search_keywords)
    baseline_score = {}
    for match in baseline_raw:
        seg = match["main_segment"]
        for sec in range(int(seg["start"]), int(seg["end"]) + 1):
            baseline_score[sec] = keyword_points

    result = resolve_keyword_scoring(
        segments,
        gui_config={"search_keywords": search_keywords, "keyword_points": keyword_points},
        config={},
    )

    # Reconstruct the same range-expansion + max-per-second reduction pipeline.py
    # now performs against resolve_keyword_scoring()'s matches.
    reconstructed_score = {}
    for m in result["matches"]:
        for sec in range(int(m["start"]), int(m["end"]) + 1):
            weight = m["weight"]
            if sec not in reconstructed_score or weight > reconstructed_score[sec]:
                reconstructed_score[sec] = weight

    assert reconstructed_score == baseline_score
