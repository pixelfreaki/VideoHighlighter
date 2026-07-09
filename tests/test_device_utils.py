"""
Tests for modules/device_utils.py's detect_best_device() and the
general_torch_device field added for Whisper/diarization consumers.

Covers: modules/device_utils.py:26-127 (detect_best_device branches) and
:165-182 (DeviceInfo).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules import device_utils


def _silent_log(_msg):
    pass


def test_cuda_available_sets_general_torch_device_cuda():
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    fake_torch.cuda.device_count.return_value = 1
    fake_torch.cuda.get_device_name.return_value = "Fake GPU"
    fake_torch.cuda.get_device_properties.return_value.total_mem = 8 * (1024 ** 3)

    with patch.object(device_utils, "_TORCH_AVAILABLE", True), \
         patch.object(device_utils, "torch", fake_torch):
        info = device_utils.detect_best_device(log_fn=_silent_log)

    assert info.general_torch_device == "cuda:0"
    assert info.pytorch_device == "cuda"
    assert info.backend_name == "CUDA"


def test_intel_xpu_available_sets_general_torch_device_xpu():
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.xpu.is_available.return_value = True
    fake_torch.xpu.device_count.return_value = 1
    fake_torch.xpu.get_device_name.return_value = "Fake Arc GPU"

    with patch.object(device_utils, "_TORCH_AVAILABLE", True), \
         patch.object(device_utils, "torch", fake_torch):
        info = device_utils.detect_best_device(log_fn=_silent_log)

    assert info.general_torch_device == "xpu:0"
    # pytorch_device stays "cpu" on Intel XPU — R3D is deliberately routed
    # through OpenVINO instead, and general_torch_device must not disturb that.
    assert info.pytorch_device == "cpu"
    assert info.backend_name == "Intel XPU (OpenVINO)"


def test_openvino_intel_gpu_without_torch_xpu_sets_general_torch_device_cpu():
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.xpu.is_available.return_value = False

    fake_core_instance = MagicMock()
    fake_core_instance.available_devices = ["CPU", "GPU.0"]
    fake_core_cls = MagicMock(return_value=fake_core_instance)

    with patch.object(device_utils, "_TORCH_AVAILABLE", True), \
         patch.object(device_utils, "torch", fake_torch), \
         patch("openvino.Core", fake_core_cls):
        info = device_utils.detect_best_device(log_fn=_silent_log)

    assert info.general_torch_device == "cpu"
    assert info.pytorch_device == "cpu"
    assert info.backend_name == "Intel GPU (OpenVINO)"


def test_no_gpu_signal_sets_general_torch_device_cpu():
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.xpu.is_available.return_value = False

    fake_core_instance = MagicMock()
    fake_core_instance.available_devices = ["CPU"]
    fake_core_cls = MagicMock(return_value=fake_core_instance)

    with patch.object(device_utils, "_TORCH_AVAILABLE", True), \
         patch.object(device_utils, "torch", fake_torch), \
         patch("openvino.Core", fake_core_cls):
        info = device_utils.detect_best_device(log_fn=_silent_log)

    assert info.general_torch_device == "cpu"
    assert info.pytorch_device == "cpu"
    assert info.backend_name == "CPU"


def test_pytorch_device_unaffected_by_general_torch_device_across_all_branches():
    # Regression: adding general_torch_device must not change pytorch_device's
    # existing values in any branch (it stays R3D/OpenVINO-specific).
    expected = {
        "CUDA": "cuda",
        "Intel XPU (OpenVINO)": "cpu",
        "Intel GPU (OpenVINO)": "cpu",
        "CPU": "cpu",
    }

    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    fake_torch.cuda.device_count.return_value = 1
    fake_torch.cuda.get_device_name.return_value = "Fake GPU"
    fake_torch.cuda.get_device_properties.return_value.total_mem = 8 * (1024 ** 3)
    with patch.object(device_utils, "_TORCH_AVAILABLE", True), \
         patch.object(device_utils, "torch", fake_torch):
        info = device_utils.detect_best_device(log_fn=_silent_log)
    assert info.pytorch_device == expected[info.backend_name]


def test_device_info_general_torch_device_is_a_slot():
    info = device_utils.DeviceInfo(
        yolo_pt_device="cpu",
        yolo_ov_device="cpu",
        openvino_device="CPU",
        pytorch_device="cpu",
        motion_device="cpu",
        general_torch_device="cpu",
        use_openvino_yolo=True,
        gpu_available=False,
        backend_name="CPU",
    )
    assert info.general_torch_device == "cpu"
