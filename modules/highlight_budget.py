"""Adaptive highlight-selection budget: tier resolution and budget calculation.

Dependency-light module (stdlib only) so it's testable without the heavy
pipeline import chain. Pure functions over dicts/lists; yaml/config-file
access stays at the edge in pipeline.py/main.py, matching the modules/
convention (see modules/dataset_import.py, modules/cli_args.py).
"""

from __future__ import annotations

DEFAULT_MAX_DURATION = 420  # matches pipeline.py's own fixed-mode default


def resolve_tier(tiers: list[dict], source_duration: float) -> dict:
    """First tier (ascending by max_source_duration) whose threshold the
    duration meets. A tier with max_source_duration=None is a fallback that
    always matches. Assumes tiers are pre-sorted ascending (GUI/config-load
    validation's job, not this function's)."""
    for tier in tiers:
        max_source = tier.get("max_source_duration")
        if max_source is None or source_duration <= max_source:
            return tier
    # No tier matched (no fallback present) -- last resort: the last tier.
    return tiers[-1]


def compute_budget(tier: dict, source_duration: float) -> float:
    """min(tier.max_duration, max(tier.min_duration, source_duration * pct))."""
    pct = float(tier.get("percentage", 0.0))
    min_dur = float(tier.get("min_duration", 0.0))
    max_dur = float(tier.get("max_duration", float("inf")))
    raw = source_duration * pct
    return min(max_dur, max(min_dur, raw))


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
    if not tiers:
        # Malformed/empty tiers: fall back to the existing fixed-mode default
        # rather than crashing -- adaptive mode with no tiers configured
        # should degrade gracefully, not break analysis.
        return float(highlights.get("max_duration", DEFAULT_MAX_DURATION)), "MAX"

    tier = resolve_tier(tiers, source_duration)
    return compute_budget(tier, source_duration), "MAX"
