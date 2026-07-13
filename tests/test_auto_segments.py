"""Tests for modules/auto_segments.py -- select_regions_bounded (U2) and
select_fixed_window_segments (U3's fixed-window integration).

Covers AE4-AE6 from docs/plans/2026-07-13-001-feat-adaptive-top-x-selection-plan.md.
"""

import numpy as np
import pytest

from modules.auto_segments import (
    Region, select_regions_bounded, select_fixed_window_segments,
    build_auto_segments,
)


def _regions(*specs):
    """specs: (start, end, score) tuples -> list[Region]."""
    return [Region(s, e, sc) for s, e, sc in specs]


def _original_select_regions(regions, target_duration, duration_mode="MAX"):
    """Verbatim reference copy of the deleted modules.auto_segments.select_regions
    (pre-U2 refactor), kept here as the byte-identical behavior baseline that
    select_regions_bounded's legacy defaults must reproduce exactly (KTD1)."""
    def _shares_time(a, b):
        return min(a.end, b.end) - max(a.start, b.start) > 0.0

    if not regions:
        return [], []

    for r in regions:
        r._density = r.score / max(0.5, r.duration)

    ranked = sorted(regions, key=lambda r: (r._density, r.score), reverse=True)

    selected = []
    total_dur = 0.0
    for r in ranked:
        if any(_shares_time(r, sel) for sel in selected):
            continue
        remaining = target_duration - total_dur
        if remaining <= 0:
            break
        actual_end = r.end
        if r.duration > remaining:
            actual_end = r.start + remaining
        selected.append(Region(r.start, actual_end, r.score, r.sources))
        total_dur += actual_end - r.start
        if duration_mode == "EXACT" and total_dur >= target_duration:
            break

    selected.sort(key=lambda r: r.start)
    return [(r.start, r.end) for r in selected], selected


# --- legacy-mode byte-identical proof (KTD1 / U2's core correctness claim) --

def test_legacy_defaults_match_select_regions_exactly():
    regions = _regions((0, 10, 5), (20, 30, 3), (50, 60, 8), (12, 18, 1))
    legacy_pairs, legacy_selected = _original_select_regions(list(regions), target_duration=25, duration_mode="MAX")

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
    legacy_pairs, _ = _original_select_regions(list(regions), target_duration=15, duration_mode="EXACT")

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


def test_ae5_overflow_candidate_is_truncated_to_the_10pct_ceiling():
    # Regression test: a single oversized overflow candidate must be
    # truncated to the overflow ceiling, not admitted at full duration
    # (previously this could push total to several times the intended
    # budget*(1+overflow_pct) cap).
    candidates = _regions((0, 100, 10), (100, 300, 1))
    selected, _ = select_regions_bounded(
        candidates, budget=100, clip_count_min=0, clip_count_max=20, overflow_pct=0.10)
    total = sum(r.duration for r in selected)
    assert total == pytest.approx(110.0)  # budget * (1 + overflow_pct), not 300


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


def test_segment_cap_last_resort_requires_alternate_segment_itself_under_cap():
    # Regression test: the "last resort" relaxation must check whether the
    # alternate-segment candidate's own segment is under its cap, not just
    # that a same-segment-distinct candidate exists later in the list --
    # otherwise a higher-score candidate can be wrongly rejected in favor of
    # a lower-score one that only got selected first by list position.
    seg0_a = _regions((0, 10, 5))[0]
    seg0_b = _regions((20, 30, 9))[0]      # higher score -- must survive the cap
    seg1_a = _regions((1810, 1820, 1))[0]  # fills segment 1's cap first
    seg1_b = _regions((1830, 1840, 1))[0]  # leftover -- segment 1 is ALSO capped
    candidates = [seg0_a, seg1_a, seg0_b, seg1_b]
    segments = [(0, 1800), (1800, 3600)]
    selected, rejected = select_regions_bounded(
        candidates, budget=1000, clip_count_min=3, clip_count_max=20,
        segments=segments, segment_cap=1)
    assert any(r.score == 9 for r in selected)
    assert len(selected) == 3


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


# --- select_fixed_window_segments: characterization vs. the original -------
# pipeline.py loop (pipeline.py:1982-2033, pre-refactor). This is the U3 P0
# fix's core correctness claim: legacy defaults must reproduce pipeline.py's
# original fixed-window selection byte-for-byte (segment boundaries, not
# just aggregate duration/count).

def _original_fixed_window_loop(score, video_duration, target_duration,
                                 duration_mode, clip_time, detections_by_sec,
                                 exact_duration=None, max_duration=None):
    """Verbatim reference copy of pipeline.py's pre-refactor fixed-window
    loop (pipeline.py:1982-2033), parameterized instead of reading globals."""
    if duration_mode == "EXACT":
        candidate_indices = np.arange(len(score))
    else:
        candidate_indices = np.where(score > 0)[0]

    candidate_scores = score[candidate_indices]
    candidate_confidences = np.zeros(len(candidate_indices))
    for idx, sec in enumerate(candidate_indices):
        if sec in detections_by_sec:
            candidate_confidences[idx] = max(conf for _, conf in detections_by_sec[sec])

    sorted_indices = np.lexsort((-candidate_confidences, -candidate_scores))
    top_indices_all = candidate_indices[sorted_indices]

    segments = []
    used_seconds = set()

    for sec in top_indices_all:
        if sec in used_seconds:
            continue

        start = max(0, sec - clip_time // 2)
        end = min(video_duration, start + clip_time)

        if end - start < clip_time and end < video_duration:
            end = min(video_duration, start + clip_time)
        if end - start < clip_time and start > 0:
            start = max(0, end - clip_time)

        if any(s in used_seconds for s in range(int(start), int(end))):
            continue

        current_duration = sum(e - s for s, e in segments)
        remaining = target_duration - current_duration
        if remaining <= 0:
            break
        if end - start > remaining:
            end = start + remaining

        segments.append((start, end))
        for s in range(int(start), int(end)):
            used_seconds.add(s)

        current_duration = sum(e - s for s, e in segments)
        if duration_mode == "EXACT" and current_duration >= exact_duration:
            break
        elif duration_mode == "MAX" and current_duration >= max_duration:
            break

    segments.sort(key=lambda x: x[0])
    return segments


def _make_score(length, peaks):
    score = np.zeros(length)
    for sec, val in peaks.items():
        score[sec] = val
    return score


def test_fixed_window_legacy_defaults_match_original_loop_max_mode():
    score = _make_score(120, {5: 8, 20: 6, 40: 9, 70: 3, 90: 5})
    detections_by_sec = {20: [("obj", 0.9)], 40: [("obj", 0.4)]}
    original = _original_fixed_window_loop(
        score, video_duration=120, target_duration=30, duration_mode="MAX",
        clip_time=10, detections_by_sec=detections_by_sec, max_duration=30)

    new_segments, _ = select_fixed_window_segments(
        score, video_duration=120, target_duration=30, duration_mode="MAX",
        clip_time=10, detections_by_sec=detections_by_sec)

    assert new_segments == original


def test_fixed_window_legacy_defaults_match_original_loop_exact_mode():
    score = _make_score(60, {2: 1, 15: 4, 30: 2, 45: 7})
    detections_by_sec = {}
    original = _original_fixed_window_loop(
        score, video_duration=60, target_duration=25, duration_mode="EXACT",
        clip_time=8, detections_by_sec=detections_by_sec, exact_duration=25)

    new_segments, _ = select_fixed_window_segments(
        score, video_duration=60, target_duration=25, duration_mode="EXACT",
        clip_time=8, detections_by_sec=detections_by_sec)

    assert new_segments == original


def test_fixed_window_legacy_defaults_match_original_loop_dense_scores():
    rng_scores = {i: (i % 7) for i in range(0, 200, 3)}
    score = _make_score(200, rng_scores)
    detections_by_sec = {i: [("x", (i % 5) / 5.0)] for i in range(0, 200, 11)}
    original = _original_fixed_window_loop(
        score, video_duration=200, target_duration=60, duration_mode="MAX",
        clip_time=6, detections_by_sec=detections_by_sec, max_duration=60)

    new_segments, _ = select_fixed_window_segments(
        score, video_duration=200, target_duration=60, duration_mode="MAX",
        clip_time=6, detections_by_sec=detections_by_sec)

    assert new_segments == original


def test_fixed_window_no_candidates_returns_empty():
    score = np.zeros(50)
    segments, rejected = select_fixed_window_segments(
        score, video_duration=50, target_duration=30, duration_mode="MAX",
        clip_time=10, detections_by_sec={})
    assert segments == []
    assert rejected == []


def test_fixed_window_adaptive_clip_count_bounds():
    score = _make_score(300, {i: 5 for i in range(0, 300, 20)})
    segments, _ = select_fixed_window_segments(
        score, video_duration=300, target_duration=10000, duration_mode="MAX",
        clip_time=5, detections_by_sec={}, clip_count_min=0, clip_count_max=3)
    assert len(segments) == 3


# --- build_auto_segments Step 4 rewiring (select_regions -> select_regions_bounded) --

def test_build_auto_segments_legacy_defaults_produces_sane_output():
    score = _make_score(300, {10: 5, 45: 8, 120: 3, 200: 6, 250: 4})
    segments, regions = build_auto_segments(
        video_duration=300, score=score,
        scenes=[(8, 15), (43, 50)],
        motion_events=[118, 119],
        audio_peaks=[198, 199, 249],
        target_duration=60, duration_mode="MAX",
        log_fn=lambda *a, **k: None,
    )
    assert segments  # non-empty: real signals should produce real segments
    assert sum(e - s for s, e in segments) <= 60 + 1e-6  # legacy: no overflow
    assert len(regions) == len(segments)
    # chronological, non-overlapping
    for (s1, e1), (s2, e2) in zip(segments, segments[1:]):
        assert e1 <= s2


def test_build_auto_segments_adaptive_clip_count_bounds():
    score = _make_score(300, {i: 5 for i in range(5, 295, 15)})
    segments, _ = build_auto_segments(
        video_duration=300, score=score,
        motion_events=list(range(5, 295, 15)),
        target_duration=10000, duration_mode="MAX",
        clip_count_min=0, clip_count_max=3,
        log_fn=lambda *a, **k: None,
    )
    assert len(segments) <= 3
