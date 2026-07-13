"""
Auto-segmentation module for highlight generation.

When CLIP_TIME=0, instead of using fixed-duration windows around high-scoring seconds,
this module builds variable-length "interest regions" from all signal types using their
natural boundaries, then merges overlapping regions and ranks them for selection.

Usage:
    from auto_segments import build_auto_segments

    segments = build_auto_segments(
        video_duration=video_duration,
        score=score,                   # np array of per-second scores
        scenes=scenes,                 # list of (start, end)
        motion_events=motion_events,   # list of timestamps
        motion_peaks=motion_peaks,     # list of timestamps
        audio_peaks=audio_peaks,       # list of timestamps
        object_detections=object_detections,  # dict {sec: [obj_names]}
        action_sequences=selected_sequences,  # list of (start, end, dur, conf, name)
        keyword_matches=keyword_matches,      # list of dicts with main_segment
        target_duration=target_duration,
        duration_mode=duration_mode,   # "EXACT" or "MAX"
        log_fn=print,
    )
"""

import numpy as np
from collections import defaultdict


# ---------------------------------------------------------------------------
# 1. Region dataclass-ish helper
# ---------------------------------------------------------------------------
class Region:
    """A candidate interest region with a time span, score, and provenance."""
    __slots__ = ("start", "end", "score", "sources", "_density")

    def __init__(self, start: float, end: float, score: float = 0.0, sources: list = None):
        self.start = float(start)
        self.end = float(end)
        self.score = float(score)
        self.sources = sources or []

    @property
    def duration(self):
        return max(0.0, self.end - self.start)

    def overlaps(self, other, gap_tolerance=1.5):
        """True if this region overlaps or is within gap_tolerance seconds of other."""
        return self.start <= other.end + gap_tolerance and other.start <= self.end + gap_tolerance

    def merge(self, other):
        """Return a new region that is the union of self and other."""
        return Region(
            start=min(self.start, other.start),
            end=max(self.end, other.end),
            score=self.score + other.score,
            sources=self.sources + other.sources,
        )

    def __repr__(self):
        s = f"{int(self.start)//60:02d}:{int(self.start)%60:02d}"
        e = f"{int(self.end)//60:02d}:{int(self.end)%60:02d}"
        return f"Region({s}-{e}, {self.duration:.1f}s, score={self.score:.1f}, src={self.sources})"


# ---------------------------------------------------------------------------
# 2. Cluster point-signals into contiguous runs
# ---------------------------------------------------------------------------
def cluster_points(timestamps, max_gap=2.0, min_pad=0.5, max_pad=2.0):
    """
    Group a sorted list of point-timestamps into (start, end) clusters.

    Points within *max_gap* seconds of each other are grouped together.
    Each cluster is padded by *min_pad* on each side (clamped to *max_pad* total
    padding so tiny clusters don't balloon).

    Returns list of (start, end) tuples.
    """
    if not timestamps:
        return []

    sorted_ts = sorted(float(t) for t in timestamps)
    clusters = []
    cluster_start = sorted_ts[0]
    cluster_end = sorted_ts[0]

    for t in sorted_ts[1:]:
        if t - cluster_end <= max_gap:
            cluster_end = t
        else:
            clusters.append((cluster_start, cluster_end))
            cluster_start = t
            cluster_end = t
    clusters.append((cluster_start, cluster_end))

    # Pad each cluster
    padded = []
    for cs, ce in clusters:
        raw_dur = ce - cs
        # Adaptive pad: shorter clusters get more relative padding
        pad = min(max_pad, max(min_pad, 1.0 if raw_dur < 1.0 else 0.5))
        padded.append((max(0.0, cs - pad), ce + pad))

    return padded


# ---------------------------------------------------------------------------
# 3. Snap a point-signal to the nearest scene boundary
# ---------------------------------------------------------------------------
def snap_to_scene(timestamp, scenes, max_snap=5.0):
    """
    If *timestamp* falls inside a scene (start, end), return that scene span.
    Otherwise return None — caller should fall back to clustering.
    """
    for s, e in scenes:
        if s - max_snap <= timestamp <= e + max_snap:
            return (s, e)
    return None


# ---------------------------------------------------------------------------
# 4. Build raw regions from every signal type
# ---------------------------------------------------------------------------
def _regions_from_actions(action_sequences, score_arr):
    """Action sequences already have natural (start, end) — use directly."""
    regions = []
    for start, end, dur, conf, name in action_sequences:
        # Score = sum of per-second score array within span (captures multi-signal boost)
        s_idx = max(0, int(start))
        e_idx = min(len(score_arr), int(end) + 1)
        region_score = float(np.sum(score_arr[s_idx:e_idx])) if e_idx > s_idx else conf
        regions.append(Region(start, end, region_score, [f"action:{name}"]))
    return regions


def _regions_from_scenes(scenes, score_arr):
    """Scenes already have (start, end)."""
    regions = []
    for start, end in scenes:
        s_idx = max(0, int(start))
        e_idx = min(len(score_arr), int(end) + 1)
        region_score = float(np.sum(score_arr[s_idx:e_idx])) if e_idx > s_idx else 0.0
        # Only include scenes that actually have some score
        if region_score > 0:
            regions.append(Region(start, end, region_score, ["scene"]))
    return regions


def _regions_from_keywords(keyword_matches, score_arr, context_pad=1.0):
    """Keyword matches have main_segment with start/end from transcript timing."""
    regions = []
    for match in (keyword_matches or []):
        seg = match.get("main_segment", {})
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start + 1))
        keyword = match.get("keyword", "keyword")
        # Small pad so we don't cut mid-word
        start = max(0, start - context_pad)
        end = end + context_pad
        s_idx = max(0, int(start))
        e_idx = min(len(score_arr), int(end) + 1)
        region_score = float(np.sum(score_arr[s_idx:e_idx])) if e_idx > s_idx else 1.0
        regions.append(Region(start, end, region_score, [f"keyword:{keyword}"]))
    return regions


def _regions_from_objects(object_detections, scenes, score_arr):
    """
    Object detections are per-second. Cluster consecutive seconds with detections,
    then try to snap to scene boundaries for cleaner cuts.
    """
    if not object_detections:
        return []

    obj_seconds = sorted(object_detections.keys())
    clusters = cluster_points(obj_seconds, max_gap=3.0, min_pad=1.0, max_pad=3.0)

    regions = []
    for cs, ce in clusters:
        # Try snapping to a scene boundary for a cleaner cut
        mid = (cs + ce) / 2.0
        scene_span = snap_to_scene(mid, scenes, max_snap=3.0)
        if scene_span:
            # Use scene boundaries but don't expand too much beyond the cluster
            start = min(cs, scene_span[0])
            end = max(ce, scene_span[1])
            # Clamp: don't let scene boundary add more than 5s on either side
            start = max(start, cs - 5.0)
            end = min(end, ce + 5.0)
        else:
            start, end = cs, ce

        s_idx = max(0, int(start))
        e_idx = min(len(score_arr), int(end) + 1)
        region_score = float(np.sum(score_arr[s_idx:e_idx])) if e_idx > s_idx else 0.0

        # Collect object names for provenance
        obj_names = set()
        for sec in range(int(cs), int(ce) + 1):
            for name in object_detections.get(sec, []):
                obj_names.add(name)

        if region_score > 0:
            regions.append(Region(start, end, region_score,
                                  [f"objects:{','.join(sorted(obj_names))}"] ))
    return regions


def _regions_from_point_signals(timestamps, signal_name, scenes, score_arr,
                                 max_gap=2.0, min_pad=0.5, max_pad=2.0):
    """Generic handler for point-signals (motion events, motion peaks, audio peaks)."""
    if not timestamps:
        return []

    clusters = cluster_points(timestamps, max_gap=max_gap, min_pad=min_pad, max_pad=max_pad)
    regions = []
    for cs, ce in clusters:
        mid = (cs + ce) / 2.0
        scene_span = snap_to_scene(mid, scenes, max_snap=3.0)
        if scene_span:
            start = min(cs, scene_span[0])
            end = max(ce, scene_span[1])
            start = max(start, cs - 5.0)
            end = min(end, ce + 5.0)
        else:
            start, end = cs, ce

        s_idx = max(0, int(start))
        e_idx = min(len(score_arr), int(end) + 1)
        region_score = float(np.sum(score_arr[s_idx:e_idx])) if e_idx > s_idx else 0.0

        if region_score > 0:
            regions.append(Region(start, end, region_score, [signal_name]))
    return regions


# ---------------------------------------------------------------------------
# 5. Merge overlapping / adjacent regions
# ---------------------------------------------------------------------------
def merge_regions(regions, gap_tolerance=1.5):
    """
    Merge all overlapping or near-adjacent regions.
    Uses iterative pass until stable (handles transitive overlaps).
    """
    if not regions:
        return []

    # Sort by start time
    regions = sorted(regions, key=lambda r: r.start)
    merged = [regions[0]]

    for region in regions[1:]:
        if merged[-1].overlaps(region, gap_tolerance):
            merged[-1] = merged[-1].merge(region)
        else:
            merged.append(region)

    # Second pass for transitive merges (rare but possible after first pass)
    changed = True
    while changed:
        changed = False
        new_merged = [merged[0]]
        for region in merged[1:]:
            if new_merged[-1].overlaps(region, gap_tolerance):
                new_merged[-1] = new_merged[-1].merge(region)
                changed = True
            else:
                new_merged.append(region)
        merged = new_merged

    return merged


# ---------------------------------------------------------------------------
# 6. Enforce min/max duration constraints on regions
# ---------------------------------------------------------------------------
def constrain_regions(regions, score_arr, video_duration, min_dur=1.5, max_dur=30.0):
    """
    - Regions shorter than min_dur get padded symmetrically.
    - Regions longer than max_dur get SPLIT into consecutive ≤max_dur windows,
      so one long merged blob yields many candidate clips instead of just one.
    """
    constrained = []
    for r in regions:
        # too short: pad
        if r.duration < min_dur:
            deficit = min_dur - r.duration
            half = deficit / 2.0
            new_start = max(0.0, r.start - half)
            new_end = min(video_duration, r.end + half)
            if new_end - new_start < min_dur:
                if new_start == 0:
                    new_end = min(video_duration, min_dur)
                else:
                    new_start = max(0, new_end - min_dur)
            constrained.append(Region(new_start, new_end, r.score, r.sources))
            continue

        # in range: keep
        if r.duration <= max_dur:
            constrained.append(r)
            continue

        # too long: split into back-to-back windows of up to max_dur
        seg_start = r.start
        while seg_start < r.end - 0.01:
            seg_end = min(r.end, seg_start + max_dur)
            if seg_end - seg_start >= min_dur:
                cs = max(0, int(seg_start))
                ce = min(len(score_arr), int(seg_end) + 1)
                win_score = float(np.sum(score_arr[cs:ce])) if ce > cs else 0.0
                constrained.append(Region(seg_start, seg_end, win_score, r.sources))
            seg_start = seg_end
    return constrained


# ---------------------------------------------------------------------------
# 7. Select non-overlapping regions to fill the duration budget
# ---------------------------------------------------------------------------
def select_regions(regions, target_duration, duration_mode="MAX"):
    """
    Greedy: pick highest-density regions first, skipping any that TRULY overlap
    (share time) with an already-selected one. Abutting clips (split sub-windows)
    are allowed so they can fill the budget back-to-back.
    """
    if not regions:
        return [], []

    def _shares_time(a, b):
        return min(a.end, b.end) - max(a.start, b.start) > 0.0

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


# ---------------------------------------------------------------------------
# 7b. Constrained selection: clip-count bounds, overflow tolerance, segment
# distribution -- the shared layer both the auto-segmentation path and the
# fixed-window path (pipeline.py) select through for adaptive highlight
# selection. Legacy (fixed/absent selection_mode) behavior is reproduced
# exactly by calling with the default parameters below (clip_count_max
# unbounded, overflow_pct=0, segments=None) -- there is no separate "legacy
# branch"; legacy IS the default-parameter case. Callers control ranking by
# the order of `candidates` -- this function never re-ranks; each selection
# path keeps its own existing ranking algorithm (density here, confidence+
# score in the fixed-window loop) in both legacy and adaptive mode, since
# nothing in the adaptive-selection requirements calls for changing ranking.
# ---------------------------------------------------------------------------
def select_regions_bounded(
    candidates,
    budget,
    duration_mode="MAX",
    clip_count_min=0,
    clip_count_max=None,
    overflow_pct=0.0,
    segments=None,
    segment_cap=None,
):
    """
    Greedy selection over pre-ranked candidates, filling `budget` with the
    same overlap/truncation/EXACT-cutoff semantics as `select_regions` (and,
    for integer-boundary windows, the fixed-window loop in pipeline.py) --
    plus, when the corresponding parameter is non-default, three additional
    behaviors: a minimum/maximum clip count, one-candidate budget overflow
    tolerance, and soft per-segment caps that relax when the minimum can't
    otherwise be met.

    Parameters
    ----------
    candidates : list of Region, already ranked by the caller (highest
        priority first). Overlap is resolved by candidate order, not score.
    budget : float -- seconds.
    duration_mode : "MAX" or "EXACT" -- same meaning as select_regions.
    clip_count_min : int -- supplement past budget until this many clips are
        selected (0 = no supplementing, matching legacy).
    clip_count_max : int or None -- stop even with budget remaining once this
        many clips are selected (None = unbounded, matching legacy).
    overflow_pct : float -- allow at most one candidate to push total
        duration up to budget*(1+overflow_pct) (0.0 = no overflow, matching
        legacy -- the hard `remaining <= 0` cutoff select_regions/the
        fixed-window loop already use).
    segments : list of (start, end) tuples or None -- 30-min buckets (or
        caller-defined spans) for distribution; None disables segment logic
        entirely, matching legacy.
    segment_cap : int or None -- max clips per segment when segments is set.

    Returns
    -------
    selected : list of Region, sorted chronologically.
    rejected : list of (Region, reason) for R11-style logging -- reason is
        one of "overlap", "clip_count_max reached", "segment cap",
        "budget exhausted".
    """
    if not candidates:
        return [], []

    def _shares_time(a, b):
        return min(a.end, b.end) - max(a.start, b.start) > 0.0

    def _segment_index(c):
        if not segments:
            return None
        for i, (s, e) in enumerate(segments):
            if s <= c.start < e:
                return i
        return len(segments) - 1  # trailing candidates clamp to the last segment

    max_clips = clip_count_max if clip_count_max is not None else float("inf")

    selected = []
    rejected = []
    total = 0.0
    overflow_used = False
    seg_counts = {}

    for pos, c in enumerate(candidates):
        if len(selected) >= max_clips:
            rejected.append((c, "clip_count_max reached"))
            continue
        if any(_shares_time(c, s) for s in selected):
            rejected.append((c, "overlap"))
            continue

        seg_idx = _segment_index(c)
        if segments is not None and segment_cap is not None and seg_idx is not None:
            if seg_counts.get(seg_idx, 0) >= segment_cap:
                still_selecting = list(candidates[pos + 1:])
                others_available = any(
                    _segment_index(o) != seg_idx
                    and not any(_shares_time(o, s) for s in selected)
                    for o in still_selecting
                )
                if len(selected) >= clip_count_min or others_available:
                    rejected.append((c, "segment cap"))
                    continue
                # else: last resort (R9) -- take it despite the cap

        remaining = budget - total
        if remaining <= 0:
            if overflow_pct > 0 and not overflow_used and total <= budget * (1 + overflow_pct):
                overflow_used = True
            elif len(selected) >= clip_count_min:
                rejected.append((c, "budget exhausted"))
                break
            # else: under the minimum -- fall through and take this
            # candidate as a supplement past budget (R6), full duration
            # (no truncation; truncation only applies while still in-budget)

        actual = c
        if remaining > 0 and c.duration > remaining:
            actual = Region(c.start, c.start + remaining, c.score, c.sources)

        selected.append(actual)
        total += actual.duration
        if seg_idx is not None:
            seg_counts[seg_idx] = seg_counts.get(seg_idx, 0) + 1

        if duration_mode == "EXACT" and total >= budget:
            break

    selected.sort(key=lambda r: r.start)
    return selected, rejected


# ---------------------------------------------------------------------------
# 8. Main entry point
# ---------------------------------------------------------------------------
def build_auto_segments(
    video_duration,
    score,
    scenes=None,
    motion_events=None,
    motion_peaks=None,
    audio_peaks=None,
    object_detections=None,
    action_sequences=None,
    keyword_matches=None,
    target_duration=420,
    duration_mode="MAX",
    min_clip=1.5,
    max_clip=30.0,
    merge_gap=1.5,
    log_fn=print,
):
    """
    Build variable-length highlight segments automatically.

    Parameters
    ----------
    video_duration : float
    score : np.ndarray — per-second score array (already computed by pipeline)
    scenes : list of (start, end)
    motion_events, motion_peaks, audio_peaks : lists of timestamps
    object_detections : dict {sec: [obj_names]}
    action_sequences : list of (start, end, dur, conf, action_name)
        — output of group_consecutive_adaptive / the selected_sequences list
    keyword_matches : list of dicts with "main_segment"
    target_duration : float — seconds budget
    duration_mode : "MAX" or "EXACT"
    min_clip : float — minimum region duration after constraining
    max_clip : float — maximum single region duration
    merge_gap : float — merge regions within this many seconds
    log_fn : callable

    Returns
    -------
    segments : list of (start, end) tuples, sorted chronologically
    regions_debug : list of Region objects (for logging/debug)
    """
    scenes = scenes or []
    motion_events = motion_events or []
    motion_peaks = motion_peaks or []
    audio_peaks = audio_peaks or []
    object_detections = object_detections or {}
    action_sequences = action_sequences or []
    keyword_matches = keyword_matches or []

    log_fn("🔧 Auto-segmentation: building interest regions from signals...")

    # --- Step 1: Build raw regions from each signal type ---
    all_regions = []

    r = _regions_from_actions(action_sequences, score)
    log_fn(f"   Actions  → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_scenes(scenes, score)
    log_fn(f"   Scenes   → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_keywords(keyword_matches, score)
    log_fn(f"   Keywords → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_objects(object_detections, scenes, score)
    log_fn(f"   Objects  → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_point_signals(motion_events, "motion_event", scenes, score,
                                     max_gap=2.0, min_pad=0.5, max_pad=2.0)
    log_fn(f"   Motion events → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_point_signals(motion_peaks, "motion_peak", scenes, score,
                                     max_gap=2.0, min_pad=0.5, max_pad=2.0)
    log_fn(f"   Motion peaks  → {len(r)} regions")
    all_regions.extend(r)

    r = _regions_from_point_signals(audio_peaks, "audio_peak", scenes, score,
                                     max_gap=2.0, min_pad=1.0, max_pad=3.0)
    log_fn(f"   Audio peaks   → {len(r)} regions")
    all_regions.extend(r)

    log_fn(f"   Total raw regions: {len(all_regions)}")

    if not all_regions:
        log_fn("⚠️ No interest regions found — falling back to empty segments")
        return [], []

    # --- Step 2: Merge overlapping / adjacent regions ---
    merged = merge_regions(all_regions, gap_tolerance=merge_gap)
    log_fn(f"   After merge: {len(merged)} regions")

    # --- Step 3: Enforce min/max duration constraints ---
    constrained = constrain_regions(merged, score, video_duration,
                                     min_dur=min_clip, max_dur=max_clip)
    log_fn(f"   After constrain: {len(constrained)} regions "
           f"(min={min_clip}s, max={max_clip}s)")

    # --- Step 4: Select best non-overlapping regions within budget ---
    segments, selected_regions = select_regions(constrained, target_duration, duration_mode)

    total_dur = sum(e - s for s, e in segments)
    log_fn(f"   Selected {len(segments)} segments, total {total_dur:.1f}s "
           f"(target: {target_duration}s, mode: {duration_mode})")

    # --- Debug: show what was selected ---
    from collections import Counter
    for i, reg in enumerate(selected_regions):
        s_mm = f"{int(reg.start)//60:02d}:{int(reg.start)%60:02d}"
        e_mm = f"{int(reg.end)//60:02d}:{int(reg.end)%60:02d}"
        src = ", ".join(f"{name} ×{n}" for name, n in Counter(reg.sources).most_common())
        log_fn(f"   Segment {i+1}: {s_mm}-{e_mm} ({reg.duration:.1f}s) "
               f"score={reg.score:.1f} sources=[{src}]")

    return segments, selected_regions