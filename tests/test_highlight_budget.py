"""Tests for modules/highlight_budget.py -- tier resolution and budget calc.

Covers AE1-AE3 from docs/plans/2026-07-13-001-feat-adaptive-top-x-selection-plan.md.
"""

import pytest

from modules import highlight_budget as hb

TIERS = [
    {"max_source_duration": 3600, "percentage": 0.10, "min_duration": 120, "max_duration": 600},
    {"max_source_duration": None, "percentage": 0.025, "min_duration": 300, "max_duration": 1800},
]


def test_resolve_adaptive_budget_exact_duration_overrides():
    # Covers AE1
    config = {"highlights": {"exact_duration": 300, "tiers": TIERS}}
    budget, mode = hb.resolve_adaptive_budget(config, source_duration=99999)
    assert (budget, mode) == (300.0, "EXACT")


def test_resolve_adaptive_budget_45min_source():
    # Covers AE2: min(600, max(120, 2700*0.10)) = 270
    config = {"highlights": {"exact_duration": None, "tiers": TIERS}}
    budget, mode = hb.resolve_adaptive_budget(config, source_duration=2700)
    assert budget == pytest.approx(270.0)
    assert mode == "MAX"


def test_resolve_adaptive_budget_8hour_source_uses_fallback():
    # Covers AE3: min(1800, max(300, 28800*0.025)) = 720
    config = {"highlights": {"exact_duration": 0, "tiers": TIERS}}
    budget, mode = hb.resolve_adaptive_budget(config, source_duration=28800)
    assert budget == pytest.approx(720.0)
    assert mode == "MAX"


def test_resolve_tier_fallback_only():
    tiers = [{"max_source_duration": None, "percentage": 0.05, "min_duration": 60, "max_duration": 900}]
    assert hb.resolve_tier(tiers, source_duration=1) is tiers[0]
    assert hb.resolve_tier(tiers, source_duration=99999) is tiers[0]


def test_resolve_tier_inclusive_boundary():
    tier = hb.resolve_tier(TIERS, source_duration=3600)
    assert tier is TIERS[0]  # exactly at threshold matches the finite tier


def test_compute_budget_clamped_below_min():
    tier = {"percentage": 0.01, "min_duration": 200, "max_duration": 600}
    assert hb.compute_budget(tier, source_duration=100) == 200  # 1s raw, clamped up to min


def test_compute_budget_clamped_above_max():
    tier = {"percentage": 0.5, "min_duration": 10, "max_duration": 300}
    assert hb.compute_budget(tier, source_duration=10000) == 300  # 5000s raw, clamped down to max


def test_compute_budget_in_range():
    tier = {"percentage": 0.10, "min_duration": 10, "max_duration": 1000}
    assert hb.compute_budget(tier, source_duration=1000) == 100


def test_empty_tiers_falls_back_to_fixed_default():
    config = {"highlights": {"exact_duration": None, "tiers": []}}
    budget, mode = hb.resolve_adaptive_budget(config, source_duration=1000)
    assert (budget, mode) == (hb.DEFAULT_MAX_DURATION, "MAX")


def test_empty_tiers_falls_back_to_configured_max_duration():
    config = {"highlights": {"exact_duration": None, "tiers": [], "max_duration": 900}}
    budget, mode = hb.resolve_adaptive_budget(config, source_duration=1000)
    assert (budget, mode) == (900.0, "MAX")


def test_missing_highlights_block_degrades_gracefully():
    budget, mode = hb.resolve_adaptive_budget({}, source_duration=1000)
    assert (budget, mode) == (hb.DEFAULT_MAX_DURATION, "MAX")


# --- resolve_selection_constraints (pipeline.py's single entry point) ------

def test_resolve_selection_constraints_legacy_zeroes_adaptive_settings():
    # Even if adaptive-only fields are present in config, legacy mode ignores
    # them entirely (R14) -- proves the "zero regardless of config" guarantee.
    config = {"highlights": {
        "selection_mode": "fixed", "max_duration": 420, "exact_duration": 0,
        "clip_count_min": 5, "clip_count_max": 10, "overflow_pct": 0.5,
        "segment_distribution_enabled": True, "tiers": TIERS,
    }}
    result = hb.resolve_selection_constraints({}, config, video_duration=1000)
    assert result["selection_mode"] == "fixed"
    assert result["target_duration"] == 420
    assert result["duration_mode"] == "MAX"
    assert result["clip_count_min"] == 0
    assert result["clip_count_max"] is None
    assert result["overflow_pct"] == 0.0
    assert result["segment_bounds"] is None
    assert result["segment_cap"] is None


def test_resolve_selection_constraints_legacy_exact_duration():
    config = {"highlights": {"selection_mode": "fixed", "exact_duration": 200}}
    result = hb.resolve_selection_constraints({}, config, video_duration=1000)
    assert (result["target_duration"], result["duration_mode"]) == (200, "EXACT")


def test_resolve_selection_constraints_adaptive_uses_tiers_and_bounds():
    config = {"highlights": {
        "selection_mode": "adaptive", "tiers": TIERS,
        "clip_count_min": 3, "clip_count_max": 20, "overflow_pct": 0.10,
    }}
    result = hb.resolve_selection_constraints({}, config, video_duration=2700)
    assert result["selection_mode"] == "adaptive"
    assert result["target_duration"] == pytest.approx(270.0)
    assert result["duration_mode"] == "MAX"
    assert result["clip_count_min"] == 3
    assert result["clip_count_max"] == 20
    assert result["overflow_pct"] == pytest.approx(0.10)
    assert result["segment_bounds"] is None  # disabled by default


def test_resolve_selection_constraints_adaptive_exact_duration_overrides():
    config = {"highlights": {"selection_mode": "adaptive", "exact_duration": 300, "tiers": TIERS}}
    result = hb.resolve_selection_constraints({}, config, video_duration=99999)
    assert (result["target_duration"], result["duration_mode"]) == (300.0, "EXACT")


def test_resolve_selection_constraints_segment_bounds_computed():
    config = {"highlights": {
        "selection_mode": "adaptive", "tiers": TIERS,
        "segment_distribution_enabled": True, "segment_minutes": 30, "segment_cap": 2,
    }}
    result = hb.resolve_selection_constraints({}, config, video_duration=5000)
    bounds = result["segment_bounds"]
    assert bounds[0] == (0.0, 1800.0)
    assert bounds[-1][1] == 5000  # last segment clamps to video_duration
    assert result["segment_cap"] == 2


def test_resolve_selection_constraints_gui_config_overrides_yaml():
    config = {"highlights": {"selection_mode": "fixed", "max_duration": 420}}
    gui_config = {"selection_mode": "adaptive", "tiers": TIERS}
    result = hb.resolve_selection_constraints(gui_config, config, video_duration=2700)
    assert result["selection_mode"] == "adaptive"
    assert result["target_duration"] == pytest.approx(270.0)
