# Residual Review Findings — feat/advanced-scoring-gui

Source: ce-code-review run 20260710-205907-1b4e3723 (LFG pipeline, 2026-07-10), plan
docs/plans/2026-07-10-004-feat-advanced-scoring-gui-plan.md. Both findings carry
concrete fixes but sat at confidence 75 without cross-persona agreement, below the
pipeline's auto-apply bar. Defer failed for both: no tracker sink (no gh CLI, no
tracker configured) — this file is the durable record.

## Residual Review Findings

- **P2 — tests/test_keyword_scoring_editor.py:378 — gate_ok override test cannot distinguish honoring the parameter from ignoring it** (testing, confidence 75). Both `gate_ok=True` and `gate_ok=False` assertions compare equal to the same on-disk value, so a regression that ignores the parameter would pass. Fix: split into two diverging cases — an enabled+invalid model with `gate_ok=True` must return the invalid serialization (cached True skips re-validation), and a valid-but-edited model with `gate_ok=False` must return the on-disk subtree (cached False forces fallback).
- **P2 — modules/keyword_scoring_editor.py:154 — unknown top-level and normalization key preservation documented but untested** (testing, confidence 75). `parse_section` preserves unrecognized top-level and normalization keys, but only the per-group `future_key` case has a round-trip test. Fix: extend the well-formed fixture with an unrecognized top-level key and normalization key and assert both survive `parse_section` -> `serialize_section`.
