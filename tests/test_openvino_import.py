"""Regression tests for the openvino.runtime -> openvino import fix.

openvino==2026.2.1 removed the openvino.runtime submodule, so any
`from openvino.runtime import Core` silently breaks under the pinned
version. tests/conftest.py shims both `openvino` and `openvino.runtime`
as MagicMock, so an actual import can't distinguish the correct import
from the broken one in this test environment -- these tests check the
source text directly instead.
"""

from __future__ import annotations

import os

import pytest

from modules.device_utils import should_export_yolo_to_openvino

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXED_FILES = [
    "pipeline.py",
    "sorter.py",
    "training/train_action_recognition.py",
    "model_training/intel/model.py",
]


@pytest.mark.parametrize("relpath", FIXED_FILES)
def test_no_openvino_runtime_import(relpath):
    path = os.path.join(REPO_ROOT, relpath)
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "openvino.runtime" not in source, (
        f"{relpath} references the removed openvino.runtime submodule; "
        f"use 'from openvino import Core' instead."
    )


def test_pipeline_uses_top_level_openvino_import():
    path = os.path.join(REPO_ROOT, "pipeline.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "from openvino import Core" in source


class _FakeDevices:
    def __init__(self, use_openvino_yolo):
        self.use_openvino_yolo = use_openvino_yolo


def test_export_skipped_when_openvino_yolo_not_used(tmp_path):
    missing_folder = str(tmp_path / "does_not_exist")
    devices = _FakeDevices(use_openvino_yolo=False)

    assert should_export_yolo_to_openvino(devices, missing_folder) is False


def test_export_runs_when_openvino_yolo_used_and_folder_missing(tmp_path):
    missing_folder = str(tmp_path / "does_not_exist")
    devices = _FakeDevices(use_openvino_yolo=True)

    assert should_export_yolo_to_openvino(devices, missing_folder) is True


def test_export_skipped_when_folder_already_exists(tmp_path):
    existing_folder = tmp_path / "already_exported"
    existing_folder.mkdir()
    devices = _FakeDevices(use_openvino_yolo=True)

    assert should_export_yolo_to_openvino(devices, str(existing_folder)) is False


def test_export_gate_call_site_uses_should_export_yolo_to_openvino():
    """Regression guard for pipeline.py's actual call site (not just the
    extracted predicate above). A full behavioral integration test isn't
    practical here: reaching this code requires driving
    _run_highlighter_impl's ~2000-line body (video trim, transcript, motion
    detection, ...) with no callable seam before the YOLO setup block. This
    source check still catches the two concrete regressions this covers:
    the export gate reverting to a hand-rolled inline condition, or a
    redundant detect_best_device() call being reintroduced in this block.
    """
    path = os.path.join(REPO_ROOT, "pipeline.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    start = source.index("# Check OpenVINO devices")
    end = source.index("# Load YOLO model")
    block = source[start:end]

    assert "if should_export_yolo_to_openvino(devices, openvino_model_folder):" in block, (
        "Export gate no longer calls should_export_yolo_to_openvino() -- "
        "confirm the extraction wasn't reverted to an inline condition."
    )
    # Match the call syntax specifically (not "detect_best_device(") so the
    # explanatory comment mentioning detect_best_device() in prose doesn't
    # inflate the count.
    call_count = block.count("detect_best_device(log_fn=")
    assert call_count == 1, (
        f"Expected exactly one detect_best_device() call in the YOLO setup "
        f"block, found {call_count} -- a redundant call may have been "
        f"reintroduced."
    )
