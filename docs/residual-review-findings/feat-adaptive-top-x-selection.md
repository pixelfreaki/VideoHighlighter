# Residual Review Findings — feat/adaptive-top-x-selection

Source: multi-persona code review (8 reviewers: correctness, testing, maintainability,
project-standards, agent-native, learnings, performance, adversarial), plan
docs/plans/2026-07-13-001-feat-adaptive-top-x-selection-plan.md. All P0/P1 findings and
the mechanically-fixable P2s were applied in commit a851c9f. The items below were left
unapplied — either they need a design decision this pipeline shouldn't make unattended,
or they're low-value test-coverage gaps below the auto-apply bar. No tracker sink
available (no authenticated `gh` CLI, no issue tracker configured) — this file is the
durable record.

## Residual Review Findings

- **P2 — modules/auto_segments.py:420 — `select_regions_bounded`'s overlap check is O(already-selected) per candidate, down from the O(1) set-membership test the old pipeline.py loop used** (performance, confidence 100). `select_fixed_window_segments` now routes every `CLIP_TIME>0` run (adaptive or legacy fixed-mode) through this function, so this is a real complexity regression on the default path, not an adaptive-only cost — both candidate count (video length) and accepted-clip count (budget/clip_time, tier `max_duration` up to 999999s via the GUI) are user-configurable and unbounded, giving O(n·m) instead of O(n). No fix applied: a proper fix needs a data-structure decision (e.g. an interval-indexed structure for `_shares_time` lookups) rather than a mechanical patch, and no test exercises realistic multi-hour candidate counts to prove impact first. Worth profiling against a real multi-hour video before investing in a fix.
- **P3 — modules/highlight_budget.py — `resolve_tier`'s "no fallback tier present, falls to last valid tier" branch has no dedicated test** (testing, confidence 75). All existing fixtures include a `max_source_duration: None` fallback tier; this branch is reachable only via a hand-edited config.yaml that omits one (GUI validation prevents it in the normal flow). Low value given `validate_tiers()` now guards the GUI path, but worth a one-line test if this module gets touched again.
- **P3 — main.py — `self._tiers_valid` is write-only, never consulted anywhere** (adversarial, confidence 75). The plan's stated "Save gated on validity" doesn't exist as an actual read of this flag — the real safety net is that `self._tiers` (the value Save reads) only updates when the table is valid, so persistence is not actually at risk. Purely a documentation/naming mismatch between the flag's name and its (unused) value; not a functional bug.
- **Advisory — docs/solutions/ doesn't exist in this repo yet** (learnings-researcher). This feature is a strong first candidate: the "legacy behavior reproduced exactly via default parameters, proven with a byte-identical characterization test against a verbatim pre-refactor reference copy" pattern, and the "pure scoring/selection multiplier excluded from the analysis-cache signature" pattern, have each now been used twice in this repo's recent history (advanced keyword scoring, then adaptive selection) with no central record of either.
