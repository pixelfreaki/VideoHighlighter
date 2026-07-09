"""
Tests for modules/speaker_utils.py's device selection.

Covers modules/speaker_utils.py's extract_speaker_embeddings — Resemblyzer's
VoiceEncoder now resolves its device through
device_utils.detect_best_device().general_torch_device instead of a
hard-coded "cpu", with a CPU fallback if loading on the resolved device raises
(Resemblyzer-on-XPU is unvalidated on real hardware in this environment).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np

from modules import speaker_utils


def _no_op_log(_msg):
    pass


def _run_with_device(resolved_device, voice_encoder_mock):
    fake_device_info = MagicMock()
    fake_device_info.general_torch_device = resolved_device

    fake_resemblyzer = MagicMock()
    fake_resemblyzer.VoiceEncoder = voice_encoder_mock
    fake_resemblyzer.preprocess_wav = MagicMock(side_effect=lambda chunk, source_sr: chunk)

    fake_librosa = MagicMock()
    fake_librosa.load = MagicMock(return_value=(np.zeros(16000, dtype=np.float32), 16000))

    with patch.object(speaker_utils, "detect_best_device", return_value=fake_device_info), \
         patch.dict(sys.modules, {"resemblyzer": fake_resemblyzer, "librosa": fake_librosa}):
        return speaker_utils.extract_speaker_embeddings(
            "fake_audio.wav", speech_segments=[], log_fn=_no_op_log,
        )


def test_cuda_available_loads_encoder_on_cuda():
    voice_encoder_mock = MagicMock(return_value=MagicMock())
    _run_with_device("cuda:0", voice_encoder_mock)
    voice_encoder_mock.assert_called_once_with(device="cuda:0")


def test_no_gpu_loads_encoder_on_cpu_unchanged():
    voice_encoder_mock = MagicMock(return_value=MagicMock())
    _run_with_device("cpu", voice_encoder_mock)
    voice_encoder_mock.assert_called_once_with(device="cpu")


def test_xpu_load_failure_falls_back_to_cpu_without_crashing():
    voice_encoder_mock = MagicMock(
        side_effect=[RuntimeError("xpu backend not available"), MagicMock()]
    )
    _run_with_device("xpu:0", voice_encoder_mock)

    assert voice_encoder_mock.call_count == 2
    first_call, second_call = voice_encoder_mock.call_args_list
    assert first_call.kwargs["device"] == "xpu:0"
    assert second_call.kwargs["device"] == "cpu"


def test_cpu_load_failure_returns_none_without_crashing():
    voice_encoder_mock = MagicMock(side_effect=RuntimeError("out of memory"))
    result = _run_with_device("cpu", voice_encoder_mock)

    assert result is None
    assert voice_encoder_mock.call_count == 1
