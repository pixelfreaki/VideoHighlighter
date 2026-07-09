# Residual Review Findings

Branch: `feat/debug-log-rotation`
Source: `ce-code-review` (mode:agent), run against `main`, 8-reviewer roster (correctness, testing, maintainability, project-standards, reliability, adversarial, agent-native, learnings-researcher).

## Fixed during this review (not residual — for context)

- **P1/P2 — unsynchronized concurrent read-modify-write race in `modules/perf_summary.py`** (correctness, reliability, adversarial — 3-way corroborated). Fixed: added a lock mirroring `debug_console.py`'s existing pattern.
- **P1 — `mark_analysis_end()`'s `log_file_path()` call could escape the "never raises" contract** (reliability, adversarial). Fixed: wrapped in try/except.
- **P1 — `install()`'s unguarded `logs_dir()`/`os.makedirs()` call could crash app startup** (reliability). Fixed: wrapped, degrades gracefully like the existing "read-only install dir" fallback.
- **P1 — a test was rotating/pruning the real project's `logs/debug.log` on every test run** (testing reviewer; confirmed via a 623KB real log file found on disk from prior test runs). Fixed: the autouse fixture now redirects every test in the file to a per-test `tmp_path`.
- **P3 — `_RETENTION_DAYS` duplicated verbatim in two modules** (maintainability). Fixed: extracted to `modules/app_paths.LOGS_RETENTION_DAYS`.

## Residual Review Findings

- **P3 — `tests/test_run_highlighter_analysis_tracking.py:40`** — the wrapper tests assert `mock_impl.assert_called_once()` but never assert the forwarded call *arguments*, so a dropped/reordered parameter in the `run_highlighter()` -> `_run_highlighter_impl()` call wouldn't be caught. (testing-reviewer, confidence 50, `manual`/`human`)
  - Not applied: low value relative to effort: the wrapper is a thin, direct pass-through (`return _run_highlighter_impl(video_path, sample_rate, gui_config, log_fn, progress_fn, cancel_flag, preview_fn)`), and any future signature drift would surface immediately as a `TypeError` in every other existing pipeline test.

- **Testing gaps (informational, not blocking):**
  - No test exercises retention-cutoff boundary conditions (a file/entry whose age lands exactly at the 7-day cutoff) in either `debug_console._prune_old_logs` or `perf_summary._prune_old_entries`.
  - No test exercises cross-process contention (a multiprocessing child appending while the parent process rotates) — only same-process concurrency (threads) is covered.
  - `perf_summary._prune_old_entries()`'s truncate-then-rewrite (`open(path, "w")`) is not crash-atomic — a process kill mid-rewrite could lose the whole cross-run history instead of just the newest line. The new lock fixes the concurrent-access race; it does not add crash-atomicity. Considered out of scope for this pass (matches the debug-log side's own accepted "best-effort, not crash-proof" posture).

- **Suggested follow-up (learnings-researcher):** this session has now fixed two separate Python exception-handling bugs of a similar shape (a bare `raise` losing the wrong exception in `modules/device_utils.py`, and these `try`/`finally`-adjacent exception-escape gaps in `modules/debug_console.py`) with no durable `docs/solutions/` entry existing yet to capture the pattern. Worth a `ce-compound` pass after this branch ships.

All other findings from this review (agent-native: not applicable to this diff; project-standards: no CLAUDE.md/AGENTS.md exist in repo) required no action.
