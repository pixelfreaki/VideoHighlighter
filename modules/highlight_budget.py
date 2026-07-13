"""Adaptive highlight-selection budget: tier resolution and budget calculation.

Dependency-light module (stdlib only) so it's testable without the heavy
pipeline import chain. Pure functions over dicts/lists; yaml/config-file
access stays at the edge in pipeline.py/main.py, matching the modules/
convention (see modules/dataset_import.py, modules/cli_args.py).
"""

from __future__ import annotations

DEFAULT_MAX_DURATION = 420  # matches pipeline.py's own fixed-mode default

DEFAULT_TIERS = [
    {"max_source_duration": 3600, "percentage": 0.10, "min_duration": 120, "max_duration": 600},
    {"max_source_duration": 14400, "percentage": 0.05, "min_duration": 300, "max_duration": 1200},
    {"max_source_duration": None, "percentage": 0.025, "min_duration": 300, "max_duration": 1800},
]


def resolve_tier(tiers: list[dict], source_duration: float) -> dict:
    """First tier (ascending by max_source_duration) whose threshold the
    duration meets. A tier with max_source_duration=None is a fallback that
    always matches. Assumes tiers are pre-sorted ascending (GUI/config-load
    validation's job, not this function's). Non-dict entries (a hand-edited
    config.yaml with a malformed tiers shape) are skipped rather than raising,
    so a bad entry degrades gracefully instead of crashing the caller."""
    valid = [t for t in tiers if isinstance(t, dict)]
    for tier in valid:
        max_source = tier.get("max_source_duration")
        if max_source is None or source_duration <= max_source:
            return tier
    # No tier matched (no fallback present) -- last resort: the last valid tier.
    return valid[-1] if valid else {}


def compute_budget(tier: dict, source_duration: float) -> float:
    """min(tier.max_duration, max(tier.min_duration, source_duration * pct))."""
    pct = float(tier.get("percentage", 0.0))
    min_dur = float(tier.get("min_duration", 0.0))
    max_dur = float(tier.get("max_duration", float("inf")))
    raw = source_duration * pct
    return min(max_dur, max(min_dur, raw))


def resolve_selection_constraints(gui_config: dict, config: dict, video_duration: float) -> dict:
    """Resolve every adaptive-selection setting pipeline.py needs from
    gui_config/config, in one place, so the derivation is unit-testable
    without pipeline.py's heavy ML import chain.

    Returns a dict with keys: selection_mode, target_duration, duration_mode,
    tier (the matched tier dict, or None outside adaptive/tier-computed mode
    -- callers needing a description for logging read this instead of
    re-deriving tiers and re-calling resolve_tier), clip_count_min,
    clip_count_max, overflow_pct, segment_bounds, segment_cap.
    Legacy (fixed/absent selection_mode) always resolves clip_count_min=0,
    clip_count_max=None, overflow_pct=0.0, segment_bounds=None, segment_cap=None
    regardless of config, so fixed-mode selection is unaffected by these
    settings (R14) even if a user has adaptive-only fields set in config.
    """
    gui_config = gui_config or {}
    config = config or {}
    highlights_cfg = config.get("highlights", {}) or {}

    def _cfg(key, default):
        return gui_config.get(key, highlights_cfg.get(key, default))

    selection_mode = _cfg("selection_mode", "fixed")
    exact_duration = _cfg("exact_duration", None)
    max_duration = _cfg("max_duration", DEFAULT_MAX_DURATION)

    tier = None
    if selection_mode == "adaptive":
        tiers = _cfg("tiers", [])
        target_duration, duration_mode = resolve_adaptive_budget(
            {"highlights": {"exact_duration": exact_duration, "max_duration": max_duration, "tiers": tiers}},
            source_duration=video_duration,
        )
        if duration_mode != "EXACT" and tiers:
            tier = resolve_tier(tiers, video_duration)

        clip_count_min = int(_cfg("clip_count_min", 0) or 0)
        clip_count_max = _cfg("clip_count_max", None)
        clip_count_max = int(clip_count_max) if clip_count_max else None
        if clip_count_max is not None and clip_count_min > clip_count_max:
            # Only the GUI validates min <= max; a hand-edited config.yaml can
            # bypass it entirely. Clamp rather than silently making R6's
            # minimum-clip guarantee unreachable (clip_count_max would
            # otherwise always win since it's checked first in the selector).
            clip_count_min = clip_count_max
        overflow_pct = float(_cfg("overflow_pct", 0.0) or 0.0)
        segment_enabled = _cfg("segment_distribution_enabled", False)
        segment_minutes = float(_cfg("segment_minutes", 30) or 30)
        if segment_minutes <= 0:
            # A non-positive value (typo, hand-edited config) would otherwise
            # drive an unbounded/reversed while-loop below -- fall back to
            # the shipped default rather than hanging.
            segment_minutes = 30.0
        segment_cap = _cfg("segment_cap", None)
        segment_cap = int(segment_cap) if segment_cap else None
        segment_bounds = None
        if segment_enabled:
            seg_secs = segment_minutes * 60
            segment_bounds = []
            pos = 0.0
            while pos < video_duration:
                segment_bounds.append((pos, min(video_duration, pos + seg_secs)))
                pos += seg_secs
    else:
        target_duration = float(exact_duration) if exact_duration else float(max_duration)
        duration_mode = "EXACT" if exact_duration else "MAX"
        clip_count_min, clip_count_max = 0, None
        overflow_pct = 0.0
        segment_bounds, segment_cap = None, None

    return {
        "selection_mode": selection_mode,
        "target_duration": target_duration,
        "duration_mode": duration_mode,
        "tier": tier,
        "clip_count_min": clip_count_min,
        "clip_count_max": clip_count_max,
        "overflow_pct": overflow_pct,
        "segment_bounds": segment_bounds,
        "segment_cap": segment_cap,
    }


def resolve_adaptive_budget(config: dict, source_duration: float) -> tuple[float, str]:
    """Entry point: returns (budget, duration_mode).

    exact_duration (when set and nonzero) overrides the tier lookup outright
    and resolves to duration_mode="EXACT" (R1). Otherwise resolves the
    matching tier and computes the budget, with duration_mode="MAX" -- a
    tier-computed budget is a ceiling to fill up to, not an exact target,
    matching how pipeline.py's existing MAX mode already treats max_duration.
    """
    highlights = (config or {}).get("highlights", {}) or {}
    exact_duration = highlights.get("exact_duration")
    if exact_duration:
        return float(exact_duration), "EXACT"

    tiers = highlights.get("tiers") or []
    valid_tiers = [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []
    if not valid_tiers:
        # Malformed/empty tiers: fall back to the existing fixed-mode default
        # rather than crashing -- adaptive mode with no tiers configured
        # should degrade gracefully, not break analysis.
        return float(highlights.get("max_duration", DEFAULT_MAX_DURATION)), "MAX"

    tier = resolve_tier(valid_tiers, source_duration)
    return compute_budget(tier, source_duration), "MAX"


def validate_tiers(rows: list[dict], clip_min: int, clip_max: int) -> str | None:
    """Validate tier rows plus clip-count bounds for the GUI tier editor.
    Returns an error message describing the first violation, or None when
    valid. Pure logic (no Qt) so it's unit-testable without instantiating
    the GUI -- the tier-editor widget reads rows via its own row-reading
    helper and calls this to decide whether to persist them."""
    if not rows:
        return "At least one tier is required."
    for i, t in enumerate(rows):
        if t["min_duration"] > t["max_duration"]:
            return f"Tier {i+1}: min budget must be ≤ max budget."
    finite = [t["max_source_duration"] for t in rows if t["max_source_duration"] is not None]
    if finite != sorted(finite):
        return "Tiers must be ordered ascending by 'Up to source'."
    if rows[-1]["max_source_duration"] is not None:
        return "The last tier must be the fallback (No limit)."
    if any(t["max_source_duration"] is None for t in rows[:-1]):
        return "Only the last tier may be the fallback (No limit)."
    if clip_min > clip_max:
        return "Min clips must be ≤ max clips."
    return None
