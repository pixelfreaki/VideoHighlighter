# modules/keyword_scoring_editor.py
"""
Pure editor model for the Advanced Keyword Scoring GUI.

All non-Qt logic behind the keywords.advanced_scoring editor panel (see
docs/plans/2026-07-10-004-feat-advanced-scoring-gui-plan.md, unit U2):
defensive parse, YAML-ready serialization, the simple-keywords import
transform, the new-group factory, reorder, reset, the persist-gate decision,
and the single save-merge choke point resolve_section_for_save().

Zero heavy dependencies -- stdlib only -- so this module is directly
unit-testable without pipeline.py's torch/cv2/ultralytics import chain,
following the same pattern as modules/video_cache.py and
modules/keyword_scoring.py. modules.keyword_scoring is imported LAZILY inside
should_persist() because it transitively imports whisper via
modules.transcript; this module must stay import-light for the GUI's startup
path.

The model is a plain dict mirroring the config schema:

    {"enabled": bool, "cooldown_seconds": number,
     "prevent_overlapping_matches": bool,
     "normalization": {lowercase, remove_accents, remove_punctuation,
                       collapse_whitespace},
     "groups": [{id, label?, weight, enabled, words, ...unknown keys}]}

Unknown per-group keys (and unknown top-level / normalization keys) are
preserved verbatim through parse/serialize so future engine fields survive
GUI round-trips (plan Assumptions).
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

_NORMALIZATION_FLAGS = (
    "lowercase", "remove_accents", "remove_punctuation", "collapse_whitespace",
)

_KNOWN_TOP_LEVEL_KEYS = (
    "enabled", "cooldown_seconds", "prevent_overlapping_matches",
    "normalization", "groups",
)


def _default_model() -> Dict[str, Any]:
    return {
        "enabled": False,
        "cooldown_seconds": 5,
        "prevent_overlapping_matches": True,
        "normalization": {flag: True for flag in _NORMALIZATION_FLAGS},
        "groups": [],
    }


def parse_section(config_data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Defensively parse keywords.advanced_scoring out of a loaded config dict (KTD5).

    Reads via the nested accessor pattern (R9) -- this section is config.yaml-only
    and never carried through the flat gui_config dict. NEVER raises on malformed
    input: every unexpected shape is coerced to a safe default and recorded as a
    human-readable string in coercion_flags. A missing or empty section parses to
    the default model (enabled off, no groups) with NO flags -- that is a normal
    fresh install, not a malformed config.

    Returns (model, coercion_flags); an empty coercion_flags list means the parse
    was clean and the model is eligible for write-through serialization.
    """
    flags: List[str] = []

    keywords = config_data.get("keywords", {}) if isinstance(config_data, dict) else {}
    if not isinstance(keywords, dict):
        flags.append(f"config 'keywords' section is not a mapping (got {type(keywords).__name__})")
        keywords = {}

    section = keywords.get("advanced_scoring", {}) or {}
    if not isinstance(section, dict):
        flags.append(
            f"keywords.advanced_scoring is not a mapping (got {type(section).__name__}); using defaults"
        )
        section = {}

    model = _default_model()

    model["enabled"] = bool(section.get("enabled", False))

    cooldown = section.get("cooldown_seconds", 5)
    try:
        float(cooldown)
        model["cooldown_seconds"] = cooldown
    except (TypeError, ValueError):
        flags.append(
            f"advanced_scoring.cooldown_seconds is not numeric ({cooldown!r}); coerced to 5"
        )
        model["cooldown_seconds"] = 5

    model["prevent_overlapping_matches"] = bool(section.get("prevent_overlapping_matches", True))

    normalization = section.get("normalization", {})
    if normalization is None:
        normalization = {}
    if not isinstance(normalization, dict):
        flags.append(
            f"advanced_scoring.normalization is not a mapping (got {type(normalization).__name__}); using defaults"
        )
        normalization = {}
    norm_model = {flag: bool(normalization.get(flag, True)) for flag in _NORMALIZATION_FLAGS}
    for key, value in normalization.items():
        if key not in _NORMALIZATION_FLAGS:
            norm_model[key] = copy.deepcopy(value)  # unknown flag preserved verbatim
    model["normalization"] = norm_model

    raw_groups = section.get("groups", [])
    if raw_groups is None:
        raw_groups = []
    if not isinstance(raw_groups, list):
        flags.append(
            f"advanced_scoring.groups is not a list (got {type(raw_groups).__name__}); coerced to empty"
        )
        raw_groups = []

    groups: List[Dict[str, Any]] = []
    for i, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            flags.append(
                f"group at index {i} is not a mapping (got {type(raw_group).__name__}); dropped"
            )
            continue

        group = copy.deepcopy(raw_group)  # unknown per-group keys preserved verbatim (R5)

        weight = group.get("weight", 0)
        try:
            float(weight)
        except (TypeError, ValueError):
            flags.append(
                f"group at index {i} has a non-numeric weight ({weight!r}); coerced to 0"
            )
            group["weight"] = 0

        words = group.get("words", [])
        if words is None:
            words = []
            group["words"] = words
        if not isinstance(words, list):
            # Conservative coercion: never guess at a split -- empty list + flag.
            flags.append(
                f"group at index {i} has words that is not a list (got {type(words).__name__}); coerced to empty"
            )
            group["words"] = []

        groups.append(group)
    model["groups"] = groups

    # Unknown top-level keys survive the round-trip too.
    for key, value in section.items():
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            model[key] = copy.deepcopy(value)

    return model, flags


def serialize_section(model: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize an editor model back into the YAML-ready advanced_scoring dict.

    Preserves group order (R4 -- order is cosmetic but user-owned), optional
    per-group `label`s (R5), and unknown keys verbatim, so
    serialize_section(parse_section(cfg)[0]) round-trips a well-formed cfg.
    """
    section: Dict[str, Any] = {
        "enabled": bool(model.get("enabled", False)),
        "cooldown_seconds": model.get("cooldown_seconds", 5),
        "prevent_overlapping_matches": bool(model.get("prevent_overlapping_matches", True)),
        "normalization": copy.deepcopy(model.get("normalization")
                                       or {flag: True for flag in _NORMALIZATION_FLAGS}),
        "groups": copy.deepcopy(model.get("groups") or []),
    }
    for key, value in model.items():
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            section[key] = copy.deepcopy(value)
    return section


def _unique_id(base: str, existing_ids) -> str:
    """First free id in the sequence base, base_2, base_3, ..."""
    existing = set(existing_ids or [])
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def import_simple_keywords(
    words: List[Any], keyword_points: Any, existing_ids: List[str],
) -> Dict[str, Any]:
    """Build the one-shot migration group from the simple keyword list (R11, AE5).

    Words are stripped, blanks dropped, and duplicates removed (first occurrence
    wins); the inputs are never mutated -- the simple list stays untouched. The
    group is enabled (it has words, unlike the blank new_group factory) and gets
    a unique id starting at "imported".
    """
    cleaned: List[str] = []
    seen = set()
    for word in words or []:
        word = str(word).strip()
        if word and word not in seen:
            seen.add(word)
            cleaned.append(word)
    return {
        "id": _unique_id("imported", existing_ids),
        "weight": keyword_points,
        "enabled": True,
        "words": cleaned,
    }


def new_group(existing_ids: List[str]) -> Dict[str, Any]:
    """Factory for the panel's "Add group" action (KTD6).

    Born DISABLED with an auto-assigned unique group_N id (first unused N from 1)
    and no words -- an enabled empty group would be instantly invalid and lock
    the persist gate the moment the user clicks Add.
    """
    existing = set(existing_ids or [])
    n = 1
    while f"group_{n}" in existing:
        n += 1
    return {"id": f"group_{n}", "weight": 1, "enabled": False, "words": []}


def reorder_group(model: Dict[str, Any], from_index: int, to_index: int) -> Dict[str, Any]:
    """Move the group at from_index to to_index, preserving every entry (R4).

    Pure: returns a new model; the input is not mutated. Out-of-range indices
    return the model unchanged (never raises -- consistent with KTD5).
    """
    result = copy.deepcopy(model)
    groups = result.get("groups") or []
    if not (0 <= from_index < len(groups)) or not (0 <= to_index < len(groups)):
        return result
    group = groups.pop(from_index)
    groups.insert(to_index, group)
    result["groups"] = groups
    return result


def reset_section() -> Dict[str, Any]:
    """The malformed-section card's reset action: the default empty section model,
    identical to a missing-section parse (KTD5). Does not write anything -- the
    fresh model becomes eligible for write-through only on the next gated edit."""
    return _default_model()


def should_persist(model: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """The persist-gate decision (R8, KTD2/KTD3).

    True when the master toggle is off (work-in-progress groups save freely,
    mirroring the engine, which never validates disabled config) OR the enabled
    model passes the engine's validation. False otherwise, with the structured
    errors ({group_index, field, message}) for inline card display.
    """
    if not bool(model.get("enabled", False)):
        return True, []

    # Lazy import (KTD4): modules.keyword_scoring transitively imports whisper
    # via modules.transcript; this module must stay import-light.
    from modules.keyword_scoring import validate_advanced_scoring_config_structured

    errors = validate_advanced_scoring_config_structured(serialize_section(model))
    return not errors, errors


def resolve_section_for_save(
    panel_model: Optional[Dict[str, Any]],
    coercion_flags: Optional[List[str]],
    config_data: Dict[str, Any],
) -> Dict[str, Any]:
    """The single choke point save_config()/closeEvent route the section through (R10).

    Serialize the panel model ONLY when a model exists AND its parse was clean
    (no coercion flags) AND the persist gate passes. In every other case -- no
    panel constructed, coercion-flagged parse (never a lossy coerced rewrite),
    or enabled-with-validation-errors (AE1/KTD3) -- return the on-disk subtree
    verbatim from config_data, so an invalid enabled state is never written and
    the wholesale config rewrite can never drop the section (AE3).
    """
    keywords = config_data.get("keywords", {}) if isinstance(config_data, dict) else {}
    on_disk = keywords.get("advanced_scoring", {}) if isinstance(keywords, dict) else {}
    on_disk = on_disk if on_disk is not None else {}

    if panel_model is None:
        return on_disk
    if coercion_flags:
        return on_disk
    ok, _errors = should_persist(panel_model)
    if not ok:
        return on_disk
    return serialize_section(panel_model)


__all__ = [
    "parse_section",
    "serialize_section",
    "import_simple_keywords",
    "new_group",
    "reorder_group",
    "reset_section",
    "should_persist",
    "resolve_section_for_save",
]
