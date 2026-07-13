"""Tests for modules/auto_segments.py -- select_regions_bounded (U2).

Covers AE4-AE6 from docs/plans/2026-07-13-001-feat-adaptive-top-x-selection-plan.md.
"""

from modules.auto_segments import Region, select_regions, select_regions_bounded


def _regions(*specs):
    """specs: (start, end, score) tuples -> list[Region]."""
    return [Region(s, e, sc) for s, e, sc in specs]


# --- legacy-mode byte-identical proof (KTD1 / U2's core correctness claim) --

def test_legacy_defaults_match_select_regions_exactly():
    regions = _regions((0, 10, 5), (20, 30, 3), (50, 60, 8), (12, 18, 1))
    legacy_pairs, legacy_selected = select_regions(list(regions), target_duration=25, duration_mode="MAX")

    # Reproduce select_regions' own ranking (density desc, score desc) as the
    # caller-supplied order -- select_regions_bounded never re-ranks itself.
    for r in regions:
        r._density = r.score / max(0.5, r.duration)
    ranked = sorted(regions, key=lambda r: (r._density, r.score), reverse=True)

    bounded_selected, _ = select_regions_bounded(ranked, budget=25, duration_mode="MAX")
    bounded_pairs = [(r.start, r.end) for r in bounded_selected]

    assert bounded_pairs == legacy_pairs
    assert len(bounded_selected) == len(legacy_selected)
    for a, b in zip(bounded_selected, legacy_selected):
        assert (a.start, a.end, a.score) == (b.start, b.end, b.score)


def test_legacy_defaults_exact_mode_matches_select_regions():
    regions = _regions((0, 10, 5), (10, 20, 3), (20, 30, 8))
    legacy_pairs, _ = select_regions(list(regions), target_duration=15, duration_mode="EXACT")

    for r in regions:
        r._density = r.score / max(0.5, r.duration)
    ranked = sorted(regions, key=lambda r: (r._density, r.score), reverse=True)
    bounded_selected, _ = select_regions_bounded(ranked, budget=15, duration_mode="EXACT")

    assert [(r.start, r.end) for r in bounded_selected] == legacy_pairs


def test_no_candidates_returns_empty_no_error():
    selected, rejected = select_regions_bounded([], budget=100)
    assert selected == []
    assert rejected == []


# --- adaptive clip-count bounds and overflow (AE4, AE5) ---------------------

def test_ae4_supplements_to_minimum_past_budget():
    # First two candidates alone already exhaust the 30s budget; min_clips=3
    # forces a 3rd (lower-scored) candidate in past budget.
    candidates = _regions((0, 15, 10), (15, 30, 9), (30, 40, 1))
    selected, _ = select_regions_bounded(
        candidates, budget=30, clip_count_min=3, clip_count_max=20)
    assert len(selected) == 3
    assert sum(r.duration for r in selected) > 30


def test_ae5_at_most_one_overflow_clip():
    candidates = _regions((0, 10, 10), (10, 20, 9), (20, 30, 8), (30, 33, 1))
    selected, _ = select_regions_bounded(
        candidates, budget=30, clip_count_min=0, clip_count_max=20, overflow_pct=0.10)
    total = sum(r.duration for r in selected)
    assert total <= 30 * 1.10
    # the 4th candidate (outside budget, small enough to fit the 10% overflow)
    # may be included once; a 5th overflow candidate must not be.
    candidates2 = _regions((0, 10, 10), (10, 20, 9), (20, 30, 8), (30, 32, 2), (32, 34, 1))
    selected2, _ = select_regions_bounded(
        candidates2, budget=30, clip_count_min=0, clip_count_max=20, overflow_pct=0.10)
    assert len(selected2) <= 4  # at most one candidate past the 3 that fit budget


def test_clip_count_max_stops_selection_before_budget_exhausted():
    candidates = _regions((0, 5, 10), (5, 10, 9), (10, 15, 8), (15, 20, 7))
    selected, _ = select_regions_bounded(
        candidates, budget=1000, clip_count_min=0, clip_count_max=2)
    assert len(selected) == 2


def test_fewer_than_minimum_candidates_in_entire_pool_returns_all_available():
    candidates = _regions((0, 5, 10), (5, 10, 9))
    selected, _ = select_regions_bounded(
        candidates, budget=5, clip_count_min=5, clip_count_max=20)
    assert len(selected) == 2  # can't reach 5; returns what exists, no crash/loop


# --- segment distribution (AE6) ---------------------------------------------

def test_ae6_segment_cap_relaxes_when_minimum_not_met_and_no_alternative():
    # 3 candidates, all in the same 30-min segment; cap=2, min_clips=3.
    candidates = _regions((0, 10, 10), (10, 20, 9), (20, 30, 8))
    segments = [(0, 1800)]
    selected, _ = select_regions_bounded(
        candidates, budget=1000, clip_count_min=3, clip_count_max=20,
        segments=segments, segment_cap=2)
    assert len(selected) == 3  # 3rd clip taken despite the cap (last resort)


def test_segment_cap_holds_when_other_segments_have_candidates():
    seg_a = _regions((0, 10, 10), (10, 20, 9), (20, 30, 8))  # segment 0
    seg_b = _regions((1900, 1910, 5))                        # segment 1
    candidates = seg_a + seg_b
    segments = [(0, 1800), (1800, 3600)]
    selected, rejected = select_regions_bounded(
        candidates, budget=1000, clip_count_min=1, clip_count_max=20,
        segments=segments, segment_cap=2)
    # segment 0 capped at 2; segment 1's candidate is a viable alternative,
    # so the cap holds and the 3rd segment-0 candidate is rejected.
    seg0_selected = [r for r in selected if r.start < 1800]
    assert len(seg0_selected) == 2
    assert any(reason == "segment cap" for _, reason in rejected)


def test_segment_distribution_disabled_by_default_matches_unsegmented_path():
    candidates = _regions((0, 10, 10), (10, 20, 9), (20, 30, 8))
    selected, _ = select_regions_bounded(candidates, budget=1000, clip_count_min=0, clip_count_max=20)
    assert len(selected) == 3  # no segment logic applied when segments=None


# --- overlap preserved -------------------------------------------------------

def test_overlapping_candidates_second_one_rejected():
    candidates = _regions((0, 10, 10), (5, 15, 9))  # overlap [5,10)
    selected, rejected = select_regions_bounded(candidates, budget=1000)
    assert len(selected) == 1
    assert selected[0].start == 0
    assert any(reason == "overlap" for _, reason in rejected)
