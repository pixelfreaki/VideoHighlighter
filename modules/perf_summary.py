"""
modules/perf_summary.py
========================
Structured per-run performance summary: combines a ProgressTracker's
per-stage durations (pipeline.py ProgressTracker.start_stage/end_stage) and
devices (record_stage_device) into one row logged at run end and appended to
a local cross-run record.

Best-effort by design: instrumentation must never be the reason a real run
fails, so every public entrypoint here swallows its own exceptions and logs
instead of raising.
"""

import json
import os
import time

from modules.app_paths import user_data_dir


def _summary_file_path() -> str:
    return os.path.join(user_data_dir(), "perf_summary.jsonl")


def build_summary(progress_tracker, video_path=None) -> dict:
    """Assemble one run's structured summary from a ProgressTracker.

    Stages with a recorded duration but no registered device (e.g. video
    cutting, which doesn't resolve a compute device) still appear, with
    device set to None.
    """
    stage_names = set(progress_tracker.stage_durations) | set(progress_tracker.stage_devices)
    stages = {
        name: {
            "duration_seconds": progress_tracker.stage_durations.get(name),
            "device": progress_tracker.stage_devices.get(name),
        }
        for name in sorted(stage_names)
    }
    return {
        "timestamp": time.time(),
        "video_path": video_path,
        "stages": stages,
    }


def log_summary(summary: dict, log_fn=print) -> None:
    """Log one structured, human-readable table for the run."""
    log_fn("📊 Performance summary:")
    for name, info in summary["stages"].items():
        duration = info["duration_seconds"]
        duration_str = f"{duration:.1f}s" if duration is not None else "n/a"
        device = info["device"] or "n/a"
        log_fn(f"   {name:<20} {duration_str:>10}   device={device}")


def append_summary(summary: dict, log_fn=print) -> None:
    """Append one run's summary to the local cross-run record."""
    try:
        path = _summary_file_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
    except Exception as e:
        log_fn(f"⚠️ Failed to append performance summary: {e}")


def emit_summary(progress_tracker, video_path=None, log_fn=print) -> None:
    """Build, log, and append one run's structured performance summary.

    Never raises — a failed summary must not fail the pipeline run.
    """
    try:
        summary = build_summary(progress_tracker, video_path=video_path)
        log_summary(summary, log_fn=log_fn)
        append_summary(summary, log_fn=log_fn)
    except Exception as e:
        log_fn(f"⚠️ Performance summary failed: {e}")
