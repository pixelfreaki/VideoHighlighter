"""
Tests for modules/perf_summary.py — structured end-of-run performance
summary combining ProgressTracker's per-stage durations and devices (U5).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules import perf_summary


class _FakeTracker:
    def __init__(self, stage_durations=None, stage_devices=None):
        self.stage_durations = stage_durations or {}
        self.stage_devices = stage_devices or {}


def _no_op_log(_msg):
    pass


def test_build_summary_combines_duration_and_device_for_three_stages():
    tracker = _FakeTracker(
        stage_durations={"transcript": 12.5, "motion": 30.0, "object_detection": 45.2},
        stage_devices={"transcript": "cuda:0", "motion": "cuda:0", "object_detection": "cpu"},
    )
    summary = perf_summary.build_summary(tracker, video_path="video.mp4")

    assert set(summary["stages"].keys()) == {"transcript", "motion", "object_detection"}
    assert summary["stages"]["transcript"] == {"duration_seconds": 12.5, "device": "cuda:0"}
    assert summary["video_path"] == "video.mp4"


def test_build_summary_marks_device_as_none_when_stage_has_no_registered_device():
    # e.g. video cutting, which doesn't resolve a compute device.
    tracker = _FakeTracker(
        stage_durations={"video_cutting": 5.0},
        stage_devices={},
    )
    summary = perf_summary.build_summary(tracker)

    assert summary["stages"]["video_cutting"]["duration_seconds"] == 5.0
    assert summary["stages"]["video_cutting"]["device"] is None


def test_emit_summary_write_failure_is_logged_not_raised(tmp_path):
    tracker = _FakeTracker(stage_durations={"trim": 1.0}, stage_devices={})
    logs = []

    with patch.object(perf_summary, "_summary_file_path", return_value=str(tmp_path / "nonexistent" / "sub" / "perf.jsonl")), \
         patch("builtins.open", side_effect=OSError("permission denied")):
        perf_summary.emit_summary(tracker, log_fn=logs.append)

    assert any("Failed to append performance summary" in msg for msg in logs)


def test_emit_summary_appends_to_file(tmp_path):
    tracker = _FakeTracker(stage_durations={"trim": 1.0}, stage_devices={"trim": None})
    summary_path = tmp_path / "perf_summary.jsonl"

    with patch.object(perf_summary, "_summary_file_path", return_value=str(summary_path)):
        perf_summary.emit_summary(tracker, video_path="run1.mp4", log_fn=_no_op_log)
        perf_summary.emit_summary(tracker, video_path="run2.mp4", log_fn=_no_op_log)

    lines = summary_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    import json
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["video_path"] == "run1.mp4"
    assert second["video_path"] == "run2.mp4"


def test_emit_summary_creates_file_on_first_run(tmp_path):
    # First-ever run: the record doesn't exist yet — created cleanly, not an error.
    tracker = _FakeTracker(stage_durations={"trim": 1.0}, stage_devices={})
    summary_path = tmp_path / "does_not_exist_yet" / "perf_summary.jsonl"

    with patch.object(perf_summary, "_summary_file_path", return_value=str(summary_path)):
        perf_summary.emit_summary(tracker, log_fn=_no_op_log)

    assert summary_path.exists()
