"""
Tests for modules/transcript.py's device selection.

Covers modules/transcript.py's get_transcript_segments — Whisper now resolves
its device through device_utils.detect_best_device().general_torch_device
instead of an isolated torch.cuda.is_available() check, with a CPU fallback
if loading on the resolved device raises (Whisper-on-XPU is unvalidated on
real hardware in this environment).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules import transcript


def _no_op_log(_msg):
    pass


def _run_with_device(monkeypatch, resolved_device, load_model_mock):
    fake_device_info = MagicMock()
    fake_device_info.general_torch_device = resolved_device

    with patch.object(transcript, "detect_best_device", return_value=fake_device_info), \
         patch.object(transcript, "split_audio", return_value=[]), \
         patch("whisper.load_model", load_model_mock):
        transcript.get_transcript_segments(
            "fake_video.mp4", log_fn=_no_op_log, cleanup=False,
        )


def test_cuda_available_loads_whisper_on_cuda(monkeypatch):
    load_model_mock = MagicMock(return_value=MagicMock())
    _run_with_device(monkeypatch, "cuda:0", load_model_mock)
    load_model_mock.assert_called_once_with("small", device="cuda:0")


def test_no_gpu_loads_whisper_on_cpu_unchanged(monkeypatch):
    load_model_mock = MagicMock(return_value=MagicMock())
    _run_with_device(monkeypatch, "cpu", load_model_mock)
    load_model_mock.assert_called_once_with("small", device="cpu")


def test_xpu_load_failure_falls_back_to_cpu_without_crashing(monkeypatch):
    # First call (xpu:0) raises; second call (cpu) succeeds.
    load_model_mock = MagicMock(side_effect=[RuntimeError("xpu backend not available"), MagicMock()])
    fake_device_info = MagicMock()
    fake_device_info.general_torch_device = "xpu:0"

    with patch.object(transcript, "detect_best_device", return_value=fake_device_info), \
         patch.object(transcript, "split_audio", return_value=[]), \
         patch("whisper.load_model", load_model_mock):
        transcript.get_transcript_segments(
            "fake_video.mp4", log_fn=_no_op_log, cleanup=False,
        )

    assert load_model_mock.call_count == 2
    first_call, second_call = load_model_mock.call_args_list
    assert first_call.kwargs["device"] == "xpu:0"
    assert second_call.kwargs["device"] == "cpu"


def test_cpu_load_failure_raises_without_retry_loop(monkeypatch):
    # If CPU itself fails to load, there is no further fallback — raise.
    load_model_mock = MagicMock(side_effect=RuntimeError("out of memory"))
    fake_device_info = MagicMock()
    fake_device_info.general_torch_device = "cpu"

    with patch.object(transcript, "detect_best_device", return_value=fake_device_info), \
         patch.object(transcript, "split_audio", return_value=[]), \
         patch("whisper.load_model", load_model_mock):
        try:
            transcript.get_transcript_segments(
                "fake_video.mp4", log_fn=_no_op_log, cleanup=False,
            )
            assert False, "expected RuntimeError to propagate"
        except RuntimeError:
            pass

    assert load_model_mock.call_count == 1
