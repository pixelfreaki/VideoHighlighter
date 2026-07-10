"""
Tests for modules/keyword_scoring_editor.py -- the pure (non-Qt) editor model
for the Advanced Keyword Scoring GUI (U2 of
docs/plans/2026-07-10-004-feat-advanced-scoring-gui-plan.md).

Dict-in/dict-out only: these tests never touch yaml (conftest shims it) or Qt.
Style mirrors tests/test_keyword_scoring.py (plain functions, local helpers).
"""

from __future__ import annotations

from modules.keyword_scoring_editor import (
    parse_section,
    serialize_section,
    import_simple_keywords,
    new_group,
    reorder_group,
    reset_section,
    should_persist,
    resolve_section_for_save,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _well_formed_section():
    """A fully-populated, valid advanced_scoring section, including an unknown
    per-group key (`future_key`) that must survive parse/serialize verbatim."""
    return {
        "enabled": True,
        "cooldown_seconds": 5,
        "prevent_overlapping_matches": True,
        "normalization": {
            "lowercase": True, "remove_accents": True,
            "remove_punctuation": True, "collapse_whitespace": True,
        },
        "groups": [
            {"id": "reaction", "label": "Reação", "weight": 6, "enabled": True,
             "words": ["caraca"], "future_key": {"nested": [1, 2]}},
            {"id": "panic", "weight": 15, "enabled": True,
             "words": ["vou morrer", "estou morta"]},
        ],
    }


def _config(section):
    return {"keywords": {"advanced_scoring": section}}


# ---------------------------------------------------------------------------
# parse_section: happy path
# ---------------------------------------------------------------------------

def test_parse_well_formed_section_preserves_everything_with_no_flags():
    model, flags = parse_section(_config(_well_formed_section()))

    assert flags == []
    assert model["enabled"] is True
    assert model["cooldown_seconds"] == 5
    assert model["prevent_overlapping_matches"] is True
    assert model["normalization"] == {
        "lowercase": True, "remove_accents": True,
        "remove_punctuation": True, "collapse_whitespace": True,
    }
    assert [g["id"] for g in model["groups"]] == ["reaction", "panic"]
    assert model["groups"][0]["label"] == "Reação"
    assert model["groups"][0]["weight"] == 6
    assert model["groups"][1]["weight"] == 15
    assert model["groups"][0]["enabled"] is True
    assert model["groups"][0]["words"] == ["caraca"]
    assert model["groups"][1]["words"] == ["vou morrer", "estou morta"]


def test_serialize_round_trips_well_formed_section_including_unknown_keys():
    section = _well_formed_section()
    model, flags = parse_section(_config(section))

    assert flags == []
    assert serialize_section(model) == section
    # the unknown key rode through verbatim
    assert serialize_section(model)["groups"][0]["future_key"] == {"nested": [1, 2]}
    # the second group never had a label -- serialization must not invent one
    assert "label" not in serialize_section(model)["groups"][1]


# ---------------------------------------------------------------------------
# parse_section: missing / empty section -> defaults, no flags
# ---------------------------------------------------------------------------

def _assert_default_model(model):
    assert model["enabled"] is False
    assert model["cooldown_seconds"] == 5
    assert model["prevent_overlapping_matches"] is True
    assert model["normalization"] == {
        "lowercase": True, "remove_accents": True,
        "remove_punctuation": True, "collapse_whitespace": True,
    }
    assert model["groups"] == []


def test_parse_missing_keywords_key_yields_default_model_no_flags():
    model, flags = parse_section({})
    assert flags == []
    _assert_default_model(model)


def test_parse_missing_advanced_scoring_key_yields_default_model_no_flags():
    model, flags = parse_section({"keywords": {"search_keywords": ["x"]}})
    assert flags == []
    _assert_default_model(model)


def test_parse_empty_or_none_section_yields_default_model_no_flags():
    for section in ({}, None):
        model, flags = parse_section(_config(section))
        assert flags == []
        _assert_default_model(model)


# ---------------------------------------------------------------------------
# parse_section: defensive coercion (KTD5) -- flagged, never raising
# ---------------------------------------------------------------------------

def test_parse_non_list_groups_coerces_to_empty_and_flags():
    model, flags = parse_section(_config({"enabled": True, "groups": "oops"}))
    assert model["groups"] == []
    assert flags != []


def test_parse_non_dict_group_entry_is_dropped_and_flagged():
    section = {"groups": [
        {"id": "a", "weight": 1, "enabled": True, "words": ["x"]},
        "bare string",
    ]}
    model, flags = parse_section(_config(section))
    assert [g["id"] for g in model["groups"]] == ["a"]
    assert flags != []


def test_parse_non_numeric_weight_coerces_to_zero_and_flags():
    section = {"groups": [{"id": "a", "weight": "abc", "enabled": True, "words": ["x"]}]}
    model, flags = parse_section(_config(section))
    assert model["groups"][0]["weight"] == 0
    assert flags != []


def test_parse_non_list_words_is_coerced_and_flagged():
    section = {"groups": [{"id": "a", "weight": 1, "enabled": True, "words": "a,b"}]}
    model, flags = parse_section(_config(section))
    assert isinstance(model["groups"][0]["words"], list)
    assert flags != []


def test_parse_non_dict_section_yields_default_model_and_flags():
    model, flags = parse_section(_config("total garbage"))
    _assert_default_model(model)
    assert flags != []


def test_parse_never_raises_on_a_pile_of_garbage():
    horrors = [
        _config({"groups": [None, 42, [], {"weight": object()}]}),
        _config({"normalization": "nope", "groups": [{"id": "a", "words": None}]}),
        {"keywords": "not a dict"},
        _config([1, 2, 3]),
    ]
    for cfg in horrors:
        model, flags = parse_section(cfg)  # must not raise
        assert isinstance(model, dict)
        assert isinstance(flags, list)


# ---------------------------------------------------------------------------
# import_simple_keywords (AE5, R11)
# ---------------------------------------------------------------------------

def test_import_simple_keywords_builds_group_from_simple_list():
    group = import_simple_keywords(["estou morta", " vou morrer "], 8, [])

    assert group["weight"] == 8
    assert group["words"] == ["estou morta", "vou morrer"]  # stripped, both present
    assert group["enabled"] is True  # it has words
    assert group["id"] == "imported"
    assert "label" not in group


def test_import_simple_keywords_id_collision_yields_distinct_unique_id():
    group = import_simple_keywords(["x"], 2, ["imported"])
    assert group["id"] == "imported_2"

    group = import_simple_keywords(["x"], 2, ["imported", "imported_2"])
    assert group["id"] == "imported_3"


def test_import_simple_keywords_dedupes_and_drops_blank_words():
    group = import_simple_keywords(["morri", " morri ", "", "   ", "caraca"], 3, [])
    assert group["words"] == ["morri", "caraca"]


def test_import_simple_keywords_leaves_input_list_untouched():
    simple = ["estou morta", " vou morrer "]
    import_simple_keywords(simple, 8, [])
    assert simple == ["estou morta", " vou morrer "]


# ---------------------------------------------------------------------------
# new_group factory (KTD6)
# ---------------------------------------------------------------------------

def test_new_group_assigns_first_unused_group_n_id():
    assert new_group([])["id"] == "group_1"
    assert new_group(["group_1"])["id"] == "group_2"
    assert new_group(["group_1", "group_2"])["id"] == "group_3"


def test_new_group_is_born_disabled_with_empty_words():
    group = new_group(["group_1"])
    assert group["enabled"] is False
    assert group["words"] == []


# ---------------------------------------------------------------------------
# reorder_group (R4)
# ---------------------------------------------------------------------------

def test_reorder_group_moves_a_group_preserving_all_entries():
    model, _ = parse_section(_config(_well_formed_section()))
    model["groups"].append({"id": "third", "weight": 1, "enabled": False, "words": []})

    reordered = reorder_group(model, 0, 2)

    assert [g["id"] for g in reordered["groups"]] == ["panic", "third", "reaction"]
    # nothing lost, nothing mutated beyond order
    assert sorted(g["id"] for g in reordered["groups"]) == ["panic", "reaction", "third"]
    moved = [g for g in reordered["groups"] if g["id"] == "reaction"][0]
    assert moved["label"] == "Reação"
    assert moved["future_key"] == {"nested": [1, 2]}


def test_reorder_group_is_reflected_in_serialization():
    model, _ = parse_section(_config(_well_formed_section()))
    reordered = reorder_group(model, 1, 0)
    serialized = serialize_section(reordered)
    assert [g["id"] for g in serialized["groups"]] == ["panic", "reaction"]


# ---------------------------------------------------------------------------
# reset_section (KTD5 reset action)
# ---------------------------------------------------------------------------

def test_reset_section_equals_missing_section_default():
    default_model, default_flags = parse_section({})
    assert default_flags == []
    assert reset_section() == default_model


def test_reset_section_serializes_to_default_shape():
    serialized = serialize_section(reset_section())
    assert serialized["enabled"] is False
    assert serialized["cooldown_seconds"] == 5
    assert serialized["prevent_overlapping_matches"] is True
    assert serialized["normalization"] == {
        "lowercase": True, "remove_accents": True,
        "remove_punctuation": True, "collapse_whitespace": True,
    }
    assert serialized["groups"] == []


# ---------------------------------------------------------------------------
# should_persist gate (R8; AE1 / AE2)
# ---------------------------------------------------------------------------

def _invalid_duplicate_across_groups_model():
    """Cleanly-parseable model: enabled, but the same normalized word lives in
    two groups -- invalid per the engine's validation."""
    model, flags = parse_section(_config({
        "enabled": True,
        "cooldown_seconds": 5,
        "prevent_overlapping_matches": True,
        "normalization": {
            "lowercase": True, "remove_accents": True,
            "remove_punctuation": True, "collapse_whitespace": True,
        },
        "groups": [
            {"id": "danger", "weight": 1, "enabled": True, "words": ["morri"]},
            {"id": "panic", "weight": 2, "enabled": True, "words": ["Morri"]},
        ],
    }))
    assert flags == []
    return model


def test_should_persist_toggle_off_with_invalid_groups_saves_freely():
    model = _invalid_duplicate_across_groups_model()
    model["enabled"] = False  # master toggle off -> WIP saves freely (AE2)

    ok, errors = should_persist(model)

    assert ok is True


def test_should_persist_toggle_on_with_duplicate_word_blocks_with_structured_error():
    model = _invalid_duplicate_across_groups_model()

    ok, errors = should_persist(model)

    assert ok is False
    assert any(
        e["field"] == "words" and "duplicate normalized keyword" in e["message"]
        for e in errors
    )


def test_should_persist_toggle_on_with_valid_groups_allows():
    model, flags = parse_section(_config(_well_formed_section()))
    assert flags == []

    ok, errors = should_persist(model)

    assert ok is True
    assert errors == []


# ---------------------------------------------------------------------------
# resolve_section_for_save (R10; AE1 / AE3 / KTD2 / KTD3)
# ---------------------------------------------------------------------------

def test_resolve_with_no_model_returns_on_disk_subtree_unchanged():
    section = _well_formed_section()
    config_data = _config(section)

    assert resolve_section_for_save(None, None, config_data) == section


def test_resolve_with_no_model_and_no_section_returns_empty_subtree():
    assert resolve_section_for_save(None, None, {}) == {}


def test_resolve_with_clean_valid_model_returns_serialized_model():
    section = _well_formed_section()
    config_data = _config(section)
    model, flags = parse_section(config_data)
    model["cooldown_seconds"] = 9  # a real edit that must be persisted

    resolved = resolve_section_for_save(model, flags, config_data)

    assert resolved["cooldown_seconds"] == 9
    assert [g["id"] for g in resolved["groups"]] == ["reaction", "panic"]


def test_resolve_with_coercion_flagged_parse_returns_on_disk_subtree():
    garbled = {"enabled": True, "groups": "oops"}
    config_data = _config(garbled)
    model, flags = parse_section(config_data)
    assert flags != []

    resolved = resolve_section_for_save(model, flags, config_data)

    # never a lossy coerced rewrite -- the on-disk subtree stands verbatim
    assert resolved == garbled


def test_resolve_enabled_model_with_validation_errors_returns_on_disk_subtree():
    # AE1 / KTD3: a cleanly-parsed but invalid enabled model must never be
    # written; the last-valid on-disk subtree stands.
    on_disk = _well_formed_section()
    config_data = _config(on_disk)
    model = _invalid_duplicate_across_groups_model()

    resolved = resolve_section_for_save(model, [], config_data)

    assert resolved == on_disk


def test_resolve_toggle_off_invalid_wip_model_is_written():
    # AE2: with the master toggle off, invalid WIP groups persist freely.
    on_disk = _well_formed_section()
    config_data = _config(on_disk)
    model = _invalid_duplicate_across_groups_model()
    model["enabled"] = False

    resolved = resolve_section_for_save(model, [], config_data)

    assert resolved["enabled"] is False
    assert [g["id"] for g in resolved["groups"]] == ["danger", "panic"]
