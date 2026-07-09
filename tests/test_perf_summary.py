"""
Tests for modules/perf_summary.py — structured end-of-run performance
summary combining ProgressTracker's per-stage durations and devices.
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


def test_build_summary_marks_duration_as_none_when_stage_has_no_recorded_duration():
    # e.g. diarization, whose device is registered separately but whose
    # duration is folded into the "transcript" stage's timing.
    tracker = _FakeTracker(
        stage_durations={},
        stage_devices={"diarization": "cuda:0"},
    )
    summary = perf_summary.build_summary(tracker)

    assert summary["stages"]["diarization"]["device"] == "cuda:0"
    assert summary["stages"]["diarization"]["duration_seconds"] is None


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


def test_append_summary_prunes_entries_older_than_retention_window(tmp_path):
    import json
    import time

    summary_path = tmp_path / "perf_summary.jsonl"
    old_entry = {"timestamp": time.time() - (10 * 86400), "video_path": "old.mp4", "stages": {}}
    recent_entry = {"timestamp": time.time() - (2 * 86400), "video_path": "recent.mp4", "stages": {}}
    summary_path.write_text(
        json.dumps(old_entry) + "\n" + json.dumps(recent_entry) + "\n", encoding="utf-8"
    )

    new_summary = {"timestamp": time.time(), "video_path": "new.mp4", "stages": {}}
    with patch.object(perf_summary, "_summary_file_path", return_value=str(summary_path)):
        perf_summary.append_summary(new_summary, log_fn=_no_op_log)

    lines = summary_path.read_text(encoding="utf-8").strip().splitlines()
    video_paths = [json.loads(line)["video_path"] for line in lines]
    assert video_paths == ["recent.mp4", "new.mp4"]


def test_prune_old_entries_drops_malformed_lines_without_raising(tmp_path):
    summary_path = tmp_path / "perf_summary.jsonl"
    summary_path.write_text("not valid json\n", encoding="utf-8")

    perf_summary._prune_old_entries(str(summary_path), log_fn=_no_op_log)  # must not raise

    assert summary_path.read_text(encoding="utf-8") == ""
