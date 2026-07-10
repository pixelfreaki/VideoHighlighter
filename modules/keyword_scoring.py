# modules/keyword_scoring.py
"""
Advanced Keyword Scoring engine.

Opt-in, config-driven weighted keyword-group scoring for transcript keyword
matches (see docs/plans/2026-07-10-003-feat-advanced-keyword-scoring-plan.md).
Zero heavy dependencies -- stdlib only -- so this module is directly
unit-testable without pipeline.py's torch/cv2/ultralytics import chain,
following the same pattern as modules/video_cache.py and modules/device_utils.py.

Simple mode (the unchanged default) delegates matching to
modules.transcript.search_transcript_for_keywords and reshapes its output;
it does not alter that function's matching algorithm.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from modules.transcript import search_transcript_for_keywords


def normalize_text(
    text: str,
    *,
    lowercase: bool = True,
    remove_accents: bool = True,
    remove_punctuation: bool = True,
    collapse_whitespace: bool = True,
) -> str:
    """Normalize text for advanced-mode keyword matching per configurable flags."""
    if not text:
        return ""
    result = text
    if lowercase:
        result = result.lower()
    if remove_accents:
        result = "".join(
            ch for ch in unicodedata.normalize("NFKD", result)
            if not unicodedata.combining(ch)
        )
    if remove_punctuation:
        result = re.sub(r"[^\w\s]", " ", result, flags=re.UNICODE)
    if collapse_whitespace:
        result = re.sub(r"\s+", " ", result).strip()
    return result


def validate_advanced_scoring_config(advanced_scoring: Dict[str, Any]) -> List[str]:
    """Validate an *enabled* advanced_scoring config. Returns error strings; empty means valid.

    Only called when keywords.advanced_scoring.enabled is true (KTD4) -- an invalid
    but disabled config is never validated and never blocks simple mode (R14).
    """
    errors: List[str] = []
    groups = advanced_scoring.get("groups") or []

    if not groups:
        errors.append("advanced_scoring.enabled is true but no groups are configured")
        return errors

    seen_ids = set()
    seen_keywords_global: Dict[str, str] = {}  # normalized keyword -> owning group label

    for i, group in enumerate(groups):
        group_id = str(group.get("id") or "").strip()
        label = group_id or f"<group at index {i}>"

        if not group_id:
            errors.append(f"group at index {i} has no id")
        elif group_id in seen_ids:
            errors.append(f"duplicate group id '{group_id}'")
        else:
            seen_ids.add(group_id)

        weight = group.get("weight")
        try:
            if float(weight) < 0:
                errors.append(f"group '{label}' has a negative weight ({weight})")
        except (TypeError, ValueError):
            errors.append(f"group '{label}' has a non-numeric weight ({weight!r})")

        enabled = group.get("enabled", True)
        words = [str(w).strip() for w in (group.get("words") or []) if str(w).strip()]

        # R13: "at least one non-blank keyword" applies to enabled groups only --
        # a disabled group is preserved but never matched (R5), so an empty word
        # list on a disabled group is not an error.
        if enabled and not words:
            errors.append(f"enabled group '{label}' has no non-blank keywords")

        # Duplicate-normalized-keyword checks apply regardless of enabled state --
        # this is a static config-consistency check, not a runtime-matching one.
        seen_in_group = set()
        for word in words:
            norm = normalize_text(word)
            if not norm:
                continue
            if norm in seen_in_group:
                errors.append(f"duplicate normalized keyword '{norm}' within group '{label}'")
                continue
            seen_in_group.add(norm)
            if norm in seen_keywords_global:
                errors.append(
                    f"duplicate normalized keyword '{norm}' in groups "
                    f"'{seen_keywords_global[norm]}' and '{label}'"
                )
            else:
                seen_keywords_global[norm] = label

    return errors


def _spans_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def match_keywords_advanced(
    transcript_segments: List[Dict[str, Any]],
    advanced_scoring: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Complete-word/phrase matching against normalized text, with overlap prevention
    (longest phrase wins, R7) and per-keyword cooldown suppression (R8).

    Returns (matches, skip_events) -- skip_events record cooldown- and overlap-skips
    for R12 logging, shaped {"keyword", "group", "reason": "cooldown"|"overlap", "start", "end"}.
    Disabled groups (R5) are never matched.
    """
    norm_cfg = advanced_scoring.get("normalization") or {}
    norm_kwargs = dict(
        lowercase=norm_cfg.get("lowercase", True),
        remove_accents=norm_cfg.get("remove_accents", True),
        remove_punctuation=norm_cfg.get("remove_punctuation", True),
        collapse_whitespace=norm_cfg.get("collapse_whitespace", True),
    )
    prevent_overlap = advanced_scoring.get("prevent_overlapping_matches", True)
    cooldown_seconds = float(advanced_scoring.get("cooldown_seconds", 5) or 0)

    # Flatten enabled groups' words into (normalized, original, group_id, weight) entries,
    # longest normalized phrase first so overlap prevention naturally prefers longer matches.
    entries: List[Tuple[str, str, Optional[str], float]] = []
    for group in advanced_scoring.get("groups") or []:
        if not group.get("enabled", True):
            continue
        group_id = group.get("id")
        weight = float(group.get("weight", 0) or 0)
        for word in group.get("words") or []:
            word = str(word).strip()
            if not word:
                continue
            norm_word = normalize_text(word, **norm_kwargs)
            if norm_word:
                entries.append((norm_word, word, group_id, weight))
    entries.sort(key=lambda e: len(e[0]), reverse=True)

    matches: List[Dict[str, Any]] = []
    skip_events: List[Dict[str, Any]] = []
    last_seen_at: Dict[str, float] = {}

    for seg in transcript_segments:
        original_text = seg.get("text", "") or ""
        normalized_text = normalize_text(original_text, **norm_kwargs)
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)
        claimed_spans: List[Tuple[int, int]] = []

        for norm_word, original_word, group_id, weight in entries:
            pattern = r"(?<!\w)" + re.escape(norm_word) + r"(?!\w)"
            for m in re.finditer(pattern, normalized_text):
                span = (m.start(), m.end())

                if prevent_overlap and any(_spans_overlap(span, cs) for cs in claimed_spans):
                    skip_events.append({
                        "keyword": original_word, "group": group_id,
                        "reason": "overlap", "start": seg_start, "end": seg_end,
                    })
                    continue

                last_time = last_seen_at.get(norm_word)
                if last_time is not None and (seg_start - last_time) < cooldown_seconds:
                    skip_events.append({
                        "keyword": original_word, "group": group_id,
                        "reason": "cooldown", "start": seg_start, "end": seg_end,
                    })
                    continue

                claimed_spans.append(span)
                last_seen_at[norm_word] = seg_start
                matches.append({
                    "keyword": original_word,
                    "group": group_id,
                    "weight": weight,
                    "original_text": original_text,
                    "normalized_text": normalized_text,
                    "start": seg_start,
                    "end": seg_end,
                    "scoring_mode": "advanced",
                })

    return matches, skip_events


def match_keywords_simple(
    transcript_segments: List[Dict[str, Any]],
    search_keywords: List[str],
    keyword_points: float,
) -> List[Dict[str, Any]]:
    """Thin wrapper around modules.transcript.search_transcript_for_keywords (KTD3).

    Does not alter the underlying matching algorithm -- reshapes its output into
    the same R11 metadata shape advanced mode produces, with scoring_mode: "simple".
    """
    raw_matches = search_transcript_for_keywords(transcript_segments, search_keywords)
    reshaped = []
    for m in raw_matches:
        seg = m.get("main_segment") or {}
        reshaped.append({
            "keyword": m.get("keyword"),
            "group": None,
            "weight": keyword_points,
            "original_text": seg.get("text", ""),
            "normalized_text": (seg.get("text", "") or "").lower(),
            "start": seg.get("start", m.get("start", 0)),
            "end": seg.get("end", m.get("end", 0)),
            "scoring_mode": "simple",
        })
    return reshaped


def resolve_keyword_scoring(
    transcript_segments: List[Dict[str, Any]],
    gui_config: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Single entry point (KTD2) pipeline.py calls for keyword matching and scoring.

    Reads keywords.advanced_scoring via the nested config.yaml-only accessor
    (config.get("keywords", {}).get("advanced_scoring", {})), NOT the flat
    gui_config-fallback pattern used for GUI-backed settings -- this section has
    no GUI in this pass, so gui_config never carries it.

    Returns {"mode": "simple"|"advanced", "matches": [...], "score_by_second": {...},
    "skip_events": [...], "validation_errors": [...]}. score_by_second collapses
    matches per unique second to the MAXIMUM weight among that second's matches
    (not a sum -- see plan Key Decisions).
    """
    advanced_scoring = config.get("keywords", {}).get("advanced_scoring", {}) or {}
    enabled = bool(advanced_scoring.get("enabled", False))

    if not enabled:
        search_keywords = gui_config.get("search_keywords", config.get("search_keywords", [])) or []
        keyword_points = gui_config.get("keyword_points", config.get("keyword_points", 2))
        matches = match_keywords_simple(transcript_segments, search_keywords, keyword_points)
        return {
            "mode": "simple",
            "matches": matches,
            "score_by_second": _score_by_second(matches),
            "skip_events": [],
            "validation_errors": [],
        }

    validation_errors = validate_advanced_scoring_config(advanced_scoring)
    if validation_errors:
        return {
            "mode": "advanced",
            "matches": [],
            "score_by_second": {},
            "skip_events": [],
            "validation_errors": validation_errors,
        }

    matches, skip_events = match_keywords_advanced(transcript_segments, advanced_scoring)
    return {
        "mode": "advanced",
        "matches": matches,
        "score_by_second": _score_by_second(matches),
        "skip_events": skip_events,
        "validation_errors": [],
    }


def _score_by_second(matches: List[Dict[str, Any]]) -> Dict[int, float]:
    """Collapse matches per unique second to the max weight in that second (not a sum)."""
    score_by_second: Dict[int, float] = {}
    for m in matches:
        sec = int(m.get("start", 0))
        weight = m.get("weight", 0) or 0
        if sec not in score_by_second or weight > score_by_second[sec]:
            score_by_second[sec] = weight
    return score_by_second
