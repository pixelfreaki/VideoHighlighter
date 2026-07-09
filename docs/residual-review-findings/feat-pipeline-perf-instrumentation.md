# Residual Review Findings

Branch: `feat/pipeline-perf-instrumentation`
Source: `ce-code-review` (mode:agent), run against `dev`, 9-reviewer roster (correctness, testing, maintainability, project-standards, performance, reliability, adversarial, agent-native, learnings-researcher).

## Residual Review Findings

- **P2 — `modules/device_utils.py:182`** — `load_with_cpu_fallback` has no timeout, so a hang (not an exception) on the newly-introduced, self-described "unvalidated" XPU device path blocks the pipeline indefinitely. On Intel Arc/Xe hardware where `torch.xpu.is_available()` is true, `whisper.load_model`/`VoiceEncoder(device="xpu:0")` could hang during driver init instead of raising — `load_with_cpu_fallback` only guards against an exception, not a hang, so there is no CPU fallback in that case. The codebase already uses `timeout=` elsewhere (`downloader.py`, `action_recognition.py`, `modules/motion_scene_detect_optimized.py`) for this class of risk. (reliability-reviewer, confidence 75, `manual`/`human`)
  - Not auto-applied: wrapping the load in a `concurrent.futures` timeout changes behavior (could abort a legitimate slow first-time model download) and needs a deliberately chosen timeout value — a human call, not a mechanical fix.

All other findings from this review were either resolved during the review (see the confirmed-and-fixed exception-reraise bug in `modules/device_utils.py`'s `load_with_cpu_fallback`, commit `8becd8b`) or routed to `residual_risks`/`testing_gaps` as informational/non-blocking:

- Predicted-vs-actual device divergence for the `transcript`/`diarization` stages in the perf summary (already documented in-code as a known limitation).
- `perf_summary.jsonl`'s unbounded growth across runs (accepted v1 limitation per the plan).
- A handful of `detect_best_device()` calls per run are somewhat redundant (not thread-through-one-DeviceInfo optimized), but bounded and negligible against a 6+ hour run — not worth the added risk of a deeper refactor here.
- No pipeline-level integration test exercises the new `start_stage`/`end_stage`/`record_stage_device` call sites inside a real `run_highlighter()` run — consistent with this repo's existing testing philosophy (pure/deterministic unit tests only, no full-pipeline integration tests).
