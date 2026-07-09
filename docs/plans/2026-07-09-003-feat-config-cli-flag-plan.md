---
title: Config File CLI Flag - Plan
type: feat
date: 2026-07-09
topic: config-cli-flag
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Config File CLI Flag - Plan

## Goal Capsule

- **Objective:** Let the user point the app at a custom config file via an optional `--conf <path>` CLI flag, with standard `--help` output, while leaving default behavior unchanged.
- **Product authority:** This document. No upstream brainstorm or requirements doc exists for this work.
- **Open blockers:** None.

---

## Product Contract

### Summary

Add an optional `--conf <path>` CLI flag that loads the application's configuration from the given file instead of the default; add `--help`/`-h` to print usage. Behavior is unchanged when neither flag is given.

### Requirements

- R1. The application shall accept an optional `--conf <path>` command-line argument.
- R2. When `--conf` is specified, the application shall load its configuration from the provided file path.
- R3. When `--conf` is omitted, the application shall use the default configuration file resolved via `config_path("config.yaml")`.
- R4. The application shall accept `--help`/`-h`, printing usage information that names the available arguments, and exit without launching the GUI.
- R5. A `--conf` path that does not exist or cannot be read shall cause the application to print a clear error and exit, rather than silently falling back to the default.
- R6. The `--conf` override shall apply consistently everywhere the application reads its configuration — both the GUI's own startup load and the pipeline's internal config load during processing — not just at one call site.

### Key Decisions

- **Parse arguments with `argparse`, not hand-rolled `sys.argv` scanning.** `argparse` gives correct, standard `--help` output for free and is the idiomatic approach. It needs `parse_known_args()` (or an equivalent split) so `QApplication(sys.argv)`'s own Qt-flag handling (e.g. `-style`) still receives the arguments it expects.
- **The `--conf` override lives in `config_path()`'s own resolution — a single shared source of truth — rather than being threaded through as an explicit parameter to every caller.** Two independent call sites already read the default config (`main.py`'s GUI startup and `pipeline.py`'s internal config load during a real processing run); both need to agree on the same resolved path without each needing to know about `--conf` individually.
- **A missing or unreadable `--conf` path is a hard error, not a silent fallback.** The user explicitly named a path; silently using different settings than requested would be more surprising than failing loudly.

### Scope Boundaries

**Outside this plan:**
- Auto-seeding a custom `--conf` path from the bundled default if it doesn't exist. `config_path()` already does this for the *default* location; a missing `--conf` path is simply an error per R5, not a seed-then-continue case.

### Dependencies / Assumptions

- Argument parsing must happen very early in `main.py` — ahead of the module-level `CONFIG_FILE = config_path("config.yaml")` assignment (`main.py:44`) and ahead of the heavy Qt/CV imports already at the top of the file — so `--help` can exit immediately and `--conf` can influence config resolution before anything reads it.
- No existing CLI argument parsing exists in `main.py` today (confirmed: `sys.argv` is currently only passed to `QApplication(sys.argv)`) — this is net-new infrastructure, not an extension of an existing parser.

### Sources & Research

- `main.py:1-44` — `CONFIG_FILE` is a module-level constant computed at import time via `config_path("config.yaml")`, before `QApplication` exists; `debug_console.install()` at `main.py:10` is the only existing precedent for "run something before the heavy imports."
- `main.py:4162` — `sys.argv` is currently only passed to `QApplication(sys.argv)`; no argparse/CLI parsing exists anywhere in `main.py` today.
- `modules/app_paths.py:152-159` — `config_path()`'s existing behavior: resolves under `user_data_dir()`, seeds from the bundled default via `resource_path()` if missing.
- `pipeline.py:451-464` — a second, independent call to `config_path("config.yaml")` inside `_run_highlighter_impl`, reached during a real pipeline run in the same process.

---

**Product Contract preservation:** Unchanged. Planning added the sections below without modifying any R-ID or existing Product Contract text.

## Planning Contract

### Key Technical Decisions

- **KTD1 — The override lives inside `config_path()` itself, as module-level state in `modules/app_paths.py`, not threaded as an explicit parameter.** This makes `pipeline.py`'s existing call site (`pipeline.py:455`) automatically respect `--conf` with zero changes there, satisfying R6 for free — both callers already go through the same function.
- **KTD2 — Argument parsing is extracted into a new, dependency-light `modules/cli_args.py` rather than inlined directly in `main.py`.** This makes it independently unit-testable without importing `main.py`'s heavy Qt/CV/ML transitive imports, matching this repo's existing pattern of small, testable helper modules (`modules/app_paths.py`, `modules/debug_console.py`) that `main.py` calls into.
- **KTD3 — Use `argparse.ArgumentParser.parse_known_args()`, not `parse_args()`.** Qt's own CLI flags (e.g. `-style`) must pass through untouched to `QApplication(sys.argv)`, called later in `main.py`; `parse_args()` would error on any flag it doesn't recognize. `argparse` does not mutate `sys.argv` itself, so `QApplication(sys.argv)` still sees the full original argv regardless of what this parser consumed.
- **KTD4 — `--conf` existence/readability validation happens once, immediately after parsing, in `main.py`'s startup block — not deferred into `config_path()`.** This fails fast with a clear error before any GUI or pipeline code runs, rather than surfacing confusingly deep inside a later config load.
- **KTD5 — The override bypasses `config_path()`'s existing bundled-seed-on-missing logic entirely.** KTD4 already guarantees the path exists and is readable by the time the override is set, so `config_path()` can return it directly — matching R2 ("load the specified file"), not "seed a new one there" (explicitly out of scope per the Product Contract).
- **KTD6 — CLI parsing runs immediately *after* `debug_console.install()`, not before it.** In the packaged `--windowed` Windows build, `sys.stdout`/`sys.stderr` are `None` until `install()` replaces them (`modules/debug_console.py:250-254`) — argparse's `--help` output and this unit's own error printing would either raise on `None.write()` or be silently lost if parsed first. `install()` is stdlib-only and cheap, so this ordering has no real cost. This does mean `--help`/error text in the frozen build lands only in `logs/debug.log`, not an invoking terminal (a pre-existing Windows GUI-subsystem constraint, not a new one); dev-mode (`python main.py --help`) is unaffected since a real console is attached throughout.

### High-Level Technical Design

```mermaid
flowchart TB
  A[main.py starts] --> Z["debug_console.install()\n(stdout/stderr become safe\nto write to, even in --windowed)"]
  Z --> B["cli_args.parse_args(sys.argv[1:])\n(parse_known_args)"]
  B -- "-h/--help" --> C[argparse prints usage,\nexits -- no GUI launched]
  B -- "--conf given" --> D{Path opens\nfor reading?}
  D -- no --> E[Print error, exit 1]
  D -- yes --> F[app_paths.set_config_override(path)]
  B -- "--conf omitted" --> G[Continue unchanged]
  F --> H[Heavy imports continue...]
  G --> H
  H --> I["Both main.py:44 and pipeline.py:455\ncall config_path('config.yaml')"]
  I --> J{Override set?}
  J -- yes --> K[Return override path directly]
  J -- no --> L["Existing default resolution\n(user_data_dir + bundled seed)"]
```

---

## Implementation Units

### U1. Add override support to `config_path()`

**Goal:** Give `modules/app_paths.py` a settable override that `config_path()` checks first, so every caller transparently respects a custom config path.

**Requirements:** R2, R3, R6

**Dependencies:** none

**Files:**
- `modules/app_paths.py` (extends)
- `tests/test_app_paths_config_override.py` (new)

**Approach:** Add module-level state `_config_override: str | None = None` and a setter `set_config_override(path: str) -> None`. Change `config_path(filename: str = "config.yaml")` to return `_config_override` immediately when set, before any of the existing `user_data_dir()`/bundled-seed logic runs. Because this is process-global state and the test suite runs in one process, tests must reset `_config_override` to `None` around each test (e.g. an autouse fixture, mirroring `tests/test_debug_console_rotation.py`'s existing reset-fixture pattern for `modules/debug_console.py`'s own module-level state) so the override-set and override-unset test scenarios below don't leak into each other.

**Patterns to follow:** The existing module-level-state style already used elsewhere in this codebase for similar single-process settings (e.g. `modules/debug_console.py`'s `_is_child`/`_analysis_counter` globals).

**Test scenarios:**
- Happy path: `set_config_override(path)` then `config_path("config.yaml")` returns that exact path.
- Happy path: `set_config_override(path)` then `config_path("config.yaml")` returns the override regardless of the `filename` argument passed (the override is a full path, not a filename to re-resolve).
- Regression: `config_path()` without ever calling `set_config_override()` preserves today's exact behavior (default resolution under `user_data_dir()`, bundled-seed-on-missing).
- Regression: when the override is set, `config_path()` does not perform the bundled-seed copy (no `shutil.copy2` call) — covers KTD5.

**Verification:** New tests pass; `pytest -q` stays green, including any existing test that exercises `config_path()`'s default behavior.

---

### U2. Add `modules/cli_args.py` for testable argument parsing

**Goal:** A small, dependency-light module that parses `--conf` and `--help`, independent of `main.py`'s heavy imports.

**Requirements:** R1, R4

**Dependencies:** none

**Files:**
- `modules/cli_args.py` (new)
- `tests/test_cli_args.py` (new)

**Approach:** `parse_args(argv: list[str]) -> argparse.Namespace` builds an `argparse.ArgumentParser` with one optional `--conf <path>` argument (built-in `-h`/`--help` comes from `argparse` automatically) and calls `parse_known_args(argv)`, returning just the namespace (the "unknown args" half is discarded — those are Qt's flags, handled downstream by `QApplication(sys.argv)` reading the original argv, not this module).

**Patterns to follow:** None specific to this repo (net-new CLI infrastructure per the Product Contract's Dependencies/Assumptions) — standard library `argparse` usage.

**Test scenarios:**
- Happy path: `parse_args(["--conf", "/some/path.yaml"])` returns a namespace with `conf == "/some/path.yaml"`.
- Happy path: `parse_args([])` returns a namespace with `conf is None`.
- Edge case (covers KTD3): `parse_args(["-style", "bb10dark"])` — an unrecognized, Qt-shaped flag — does not raise; it's silently accepted as an unknown arg rather than causing an argparse error.
- Edge case: `parse_args(["--help"])` raises `SystemExit` (argparse's built-in behavior) — covers R4's "exits without launching the GUI."

**Verification:** New tests pass; `pytest -q` stays green.

---

### U3. Wire CLI parsing into `main.py`'s startup

**Goal:** Every real app launch parses `--conf`/`--help` before anything else runs, validates `--conf`, and sets the override.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** U1, U2

**Files:**
- `main.py` (extends, top of file only)

**Approach:** Call `modules.cli_args.parse_args(sys.argv[1:])` immediately **after** `debug_console.install()` (`main.py:9-10`), not before. In the packaged `--windowed` Windows build, `sys.stdout`/`sys.stderr` are `None` until `install()` runs (`modules/debug_console.py:250-254`); anything argparse or this code prints before that point either raises on `None.write()` or is silently lost — `install()` itself is stdlib-only and cheap, so this ordering costs nothing while eliminating that crash risk. `--help`'s exit is handled entirely by `argparse` inside `parse_args()`. If `args.conf` is set: attempt to open it for reading (`try: open(args.conf, "r").close() except OSError as e: ...`) — this single check covers "does not exist," "is a directory," and "permission denied" uniformly (R5); on failure, print a clear error naming the path and the underlying `OSError` and `sys.exit(1)`; on success, call `modules.app_paths.set_config_override(args.conf)`. The existing `CONFIG_FILE = config_path("config.yaml")` line (`main.py:44`) needs no change — it already picks up the override transparently via U1.

**Known limitation (not fixed by this plan):** In the packaged `--windowed` build, `--help`/error text still lands only in `logs/debug.log`, not in an invoking terminal — Windows GUI-subsystem executables don't inherit console I/O by default, a pre-existing constraint `debug_console.py`'s own log-file/live-log-window design already works around for the app's normal output. Making `--help` visible in an invoking terminal for the frozen build would require Windows console attachment (`AttachConsole`), which is out of scope here; `python main.py --help` in dev mode (a real console throughout) is unaffected and works as expected.

**Patterns to follow:** `main.py:4-10`'s existing "run something before the heavy imports" precedent (the `debug_console.install()` call and its explanatory comment).

**Test scenarios:**
- `Test expectation: none -- this unit only wires together two already-tested modules (U1, U2) at the top of main.py; the wiring itself has no independent logic to unit test in isolation without importing main.py's full heavy dependency chain.`
- Manual/smoke verification instead: running `python main.py --help` prints usage and exits without a GUI window appearing; running with a valid `--conf <path>` loads that file's settings, verified by confirming a real pipeline run (not just GUI startup) reflects a value only present in the custom file — this is the concrete check for R6, since R6's whole point is that `pipeline.py`'s independent `config_path()` call (`pipeline.py:455`) also respects the override; running with a nonexistent `--conf <path>` prints an error and exits non-zero; running with no flags behaves exactly as before.

**Verification:** Manual smoke checks above. `pytest -q` stays green (no regressions in U1/U2's own suites).

---

## Verification Contract

| Command | Applicability | Gate |
|---|---|---|
| `pytest -q` | U1, U2 | Full existing suite stays green, plus new tests for `app_paths` override behavior and `cli_args` parsing. |
| Manual smoke run | U3 | `--help` exits without launching the GUI; valid `--conf` loads that file; missing `--conf` errors and exits; no flags behaves unchanged. |

---

## Definition of Done

- **Global:** `pytest -q` is green, including all new test files. No new third-party dependency added (`argparse` is standard library).
- **U1:** `config_path()` returns the override when set, bypassing bundled-seeding; unchanged default behavior when not set.
- **U2:** `cli_args.parse_args()` correctly parses `--conf`, ignores unrecognized (Qt) flags, and `--help` exits via `argparse`'s built-in behavior.
- **U3:** A real app launch with `--help` exits before the GUI appears; a valid `--conf` visibly changes loaded settings; a missing `--conf` path fails with a clear error and non-zero exit; omitting both flags behaves exactly as before this change.
- **Cleanup:** No leftover debug prints or scratch files from developing/testing this feature.
