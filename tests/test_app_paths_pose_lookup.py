"""latest_custom_pose_model() must only return pose models (keypoint_names.json
sidecar) -- a freshly trained object detector's best.pt (classes.json sidecar)
must never hijack pose-model resolution."""

import os

from modules import app_paths


def _fake_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: str(tmp_path))


def _make_run(tmp_path, name, sidecar, mtime):
    weights = tmp_path / "models" / name / "weights"
    weights.mkdir(parents=True)
    best = weights / "best.pt"
    best.write_bytes(b"pt")
    if sidecar:
        (weights / sidecar).write_text("{}")
    os.utime(best, (mtime, mtime))
    return best


def test_pose_run_with_sidecar_is_returned(monkeypatch, tmp_path):
    _fake_roots(monkeypatch, tmp_path)
    best = _make_run(tmp_path, "pose", "keypoint_names.json", 1_000_000)
    assert app_paths.latest_custom_pose_model() == str(best)


def test_newer_detect_run_is_ignored(monkeypatch, tmp_path):
    _fake_roots(monkeypatch, tmp_path)
    pose = _make_run(tmp_path, "pose", "keypoint_names.json", 1_000_000)
    _make_run(tmp_path, "detector", "classes.json", 2_000_000)  # newer
    assert app_paths.latest_custom_pose_model() == str(pose)


def test_sidecarless_run_is_ignored(monkeypatch, tmp_path):
    _fake_roots(monkeypatch, tmp_path)
    _make_run(tmp_path, "mystery", None, 1_000_000)
    assert app_paths.latest_custom_pose_model() is None


def test_dropin_returned_without_sidecar(monkeypatch, tmp_path):
    _fake_roots(monkeypatch, tmp_path)
    dropin = tmp_path / "custom_keypoints.pt"
    dropin.write_bytes(b"pt")
    assert app_paths.latest_custom_pose_model() == str(dropin)


def test_no_candidates_returns_none(monkeypatch, tmp_path):
    _fake_roots(monkeypatch, tmp_path)
    assert app_paths.latest_custom_pose_model() is None
