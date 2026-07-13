---
title: YOLO Dataset Import and Training - Plan
type: feat
date: 2026-07-10
topic: yolo-dataset-import-training
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# YOLO Dataset Import and Training - Plan

## Goal Capsule

- **Objective:** Import Roboflow-style YOLO dataset ZIPs into a persistent, growing dataset library; train one custom detector over the merged library (or a single dataset standalone) from the app's configured base model, with end-of-run metrics; and wire the app so the trained model is selectable and correctly cache-invalidated.
- **Authority:** This plan's Product Contract (user-confirmed through brainstorm and planning dialogue) governs behavior; the Planning Contract governs technical approach; repo conventions override where the plan is silent.
- **Stop conditions:** Surface rather than guess if implementation contradicts the Product Contract (e.g., the pipeline's custom branch behaves differently than documented) or requires new heavy dependencies in the test path.
- **Execution profile:** Code changes with unit tests for all pure logic; GUI and training runs verified by manual smoke (no Qt test harness exists; training requires GPU time).
- **Product Contract preservation:** Changed relative to the brainstorm version — added dataset enrichment (R17-R21), metrics and class sidecars (R22-R24), and amended R2/R13; all user-directed during planning dialogue. R1-R16 otherwise preserved.

---

## Product Contract

### Summary

A standalone CLI training script imports YOLO dataset ZIPs (Roboflow/CVAT exports), validates them with clear pass/fail reporting, and by default merges each import into a persistent dataset library — training one model from the app's configured base checkpoint over everything imported so far, and reporting mAP/precision/recall metrics at the end. A small app-side change makes the trained model selectable in the GUI and folds the model choice into the analysis-cache signature.

### Problem Frame

Users source object-detection datasets from annotation platforms (Roboflow, CVAT) as ZIP archives in the standard YOLO layout. VideoHighlighter cannot consume them: users must extract manually, fix the dataset paths, and train outside the app. The user has three such exports in `temp/` and wants one detector whose class set grows as datasets are added — not a shelf of single-purpose models.

Separately, even a successfully trained model is unusable today: the pipeline has a custom-detector branch, but the GUI's Detector-type dropdown offers only "standard" and hardcodes the custom model path to `None`, so no config or GUI action can reach it.

### Key Decisions

- **Standalone script, not a `main.py` subcommand.** `main.py` is a windowed PySide6 app with heavy module-level imports and no console in the packaged build. Training follows the existing standalone-script pattern (`training/train_yolo.py`).
- **Enrichment is the default; standalone is the flag.** Importing a ZIP merges it into a persistent dataset library and trains over the full library, matching the user's goal of one model with growing variety. A flag trains a single dataset without touching the library.
- **Enrichment retrains from the base model, never from the previous custom checkpoint.** Fine-tuning an existing custom model on only new data would forget earlier classes (catastrophic forgetting). Cost: training time grows with the library.
- **The trained model is a separate artifact, never an overwrite of `yolo11m.pt`.** Fine-tuning replaces the 80-class COCO head; overwriting the base would break standard detection and desynchronize the existing OpenVINO export. The app consumes trained models through its custom-detector slot, one active at a time.
- **Defaults come from the app config, selected by `--config`.** Base model derives from the config's `advanced.yolo_model_size` (currently `m` → `yolo11m.pt`); fallback `yolo11m.pt` when the config is missing or unreadable; `--model` overrides both.
- **Training runs are additive and versioned.** Each run gets its own output directory; retraining never clobbers a previous `best.pt`. The user repoints the active model deliberately.
- **App-side wiring is in scope.** Without it the feature trains models nothing can use.
- **In this repo, not a separate project.** Half the feature is app changes; the trainer reuses this repo's heavy environment; `training/` scripts are already dev-only and excluded from the packaged exe. The dependency-light module boundary keeps future extraction cheap.
- **Coexistence with the labeler workflow.** The in-app annotation → `training/train_yolo.py` path is untouched.

### Requirements

**Dataset import and validation**

- R1. The script accepts `--dataset <path>` where the path is a ZIP archive, an already-extracted dataset directory, or a direct `data.yaml` path.
- R2. ZIP input is extracted to a temporary working directory that is removed after the run unless the user asks to preserve it; the persistent dataset library is never under `temp/` and routine temp cleanup never touches it.
- R3. The script locates `data.yaml` (at archive root or one level down) and validates before training: archive integrity, `data.yaml` present, class definitions consistent (`nc` matches `names`), image and label directories present for `train` and `valid` splits, and label files readable with class ids in range. A `test` split is optional.
- R4. Roboflow-style relative paths in `data.yaml` (`../train/images`) are rewritten to resolve inside the extracted dataset before training.
- R5. Duplicate entries inside the ZIP (observed in real Roboflow exports) do not fail extraction; archive entries that would escape the extraction directory (zip-slip) are rejected via resolved-path containment (covering `../`, absolute, and drive-letter entries); extraction enforces caps on total uncompressed size, per-entry size, compression ratio, and entry count so a decompression bomb fails validation instead of exhausting disk.
- R6. Class names pass through untouched, including spaces, hyphens, and non-ASCII text.
- R7. Validation output reports each check, the class list with ids, and per-split image counts; on failure it names what is missing alongside the expected structure, and training does not start.

**Dataset library (enrichment)**

- R17. A persistent dataset library lives at a durable location; each import merges the new dataset in: class lists unioned by name, label class-ids remapped to the combined list, source-prefixed filenames to prevent collisions, split structure preserved per source.
- R18. Importing enriches the library and trains on the full merged set by default; a standalone flag trains only the given dataset without touching the library.
- R19. The library records per-import provenance (dataset name/version from the Roboflow metadata block); re-importing an already-imported dataset version is detected and skipped unless forced.
- R20. A summary mode reports library contents — datasets imported, combined class list, per-split image counts — without training.
- R21. Once merged, the source ZIP is no longer needed; the library is self-contained for all future runs.

**Training**

- R8. Training fine-tunes from the config-derived base model by default; `--weights <checkpoint>` fine-tunes from an existing checkpoint instead (standalone mode only — enrichment always starts from the base model per Key Decisions).
- R9. The script exposes `--epochs`, `--batch`, `--img-size`, `--device`, `--workers`, and `--project` (output directory), with sensible defaults when omitted.
- R10. Device selection follows the repo's training policy: CUDA when available, otherwise CPU; Intel GPU is never used for training.
- R11. Training proceeds in offline or download-blocked environments (observed live: ultralytics' AMP-check download failed on this machine's SSL setup and training continued).
- R12. Training progress, dataset statistics, selected model, device, and the final model location are logged to the console.

**Metrics**

- R22. Every training run ends with a console metrics summary: mAP50, mAP50-95, precision, recall, per-class AP, dataset stats, and total training time; when the dataset has a test split, the model is evaluated on it and those held-out numbers are reported.
- R23. The same numbers are written to a `metrics.json` next to `best.pt`, alongside ultralytics' native artifacts (results curves, confusion matrix), so models in the library can be compared across enrichment runs.

**Output and app integration**

- R13. Training artifacts (`best.pt`, `last.pt`, plots, `metrics.json`) land in a predictable per-run directory; re-running with the same name creates a new versioned run directory rather than overwriting the previous one.
- R14. On completion the script prints the exact config settings needed to use the trained model in VideoHighlighter.
- R24. A `classes.json` sidecar is written next to `best.pt` with the model's class list; the GUI label selector reads it for custom detect models so classes are listed by name without loading torch (mirroring how `yolo_objects_labels.json` serves COCO names today).
- R15. The config keys `advanced.yolo_type: custom` and `advanced.yolo_custom_model_path` reach the pipeline's existing custom-detector branch. Today the GUI overwrites both from widget state (`yolo_type` always "standard", custom path always `None`), making that branch unreachable.
- R16. The GUI's Detector-type dropdown regains a "Custom model" option with a file-picker row for the model path, and the selection survives round-trips through GUI config saves.

### Key Flows

- F1. **Import and train (enrichment default).**
  - **Trigger:** User runs the script with `--dataset ThreatDataset.zip`.
  - **Steps:** Extract to temp; validate per R3-R7; rewrite paths per R4; merge into the library per R17 (skipped when already imported, per R19); train from the config-derived base model over the full library; write artifacts, `metrics.json`, and `classes.json`; print metrics and the config snippet; clean up temp.
  - **Outcome:** One `best.pt` that detects every class imported so far.
  - **Covers R1-R14, R17-R24.**
- F2. **Validation failure.**
  - **Trigger:** Dataset is missing a required piece (e.g., `valid/labels`).
  - **Steps:** Validation reports what passed, names what is missing, shows the expected structure, exits nonzero. The library is not modified.
  - **Outcome:** No training time wasted on a broken dataset.
  - **Covers R3, R7.**
- F3. **Use the trained model.**
  - **Trigger:** User picks "Custom model" in the GUI and browses to `best.pt` (or sets the printed config values).
  - **Steps:** GUI carries `yolo_type: custom` + the model path into the pipeline run; the user selects the model's classes in the label selector (which lists them from `classes.json`) — `highlight_objects` must be non-empty, since the pipeline skips object detection entirely otherwise; the custom-detector branch loads the model and detects the selected classes; the analysis cache treats the model switch as a new signature.
  - **Outcome:** Highlights driven by the custom classes; no stale cached detections.
  - **Covers R15, R16, R24.**
- F4. **Enrich with a second dataset.**
  - **Trigger:** User runs the script with a second ZIP after a previous import.
  - **Steps:** Validate; merge (class union by name, id remap, filename prefixing); retrain from the base model over the combined library; report merged metrics.
  - **Outcome:** One model detecting both datasets' classes; the earlier run's artifacts remain untouched.
  - **Covers R17-R19, R13.**

### Acceptance Examples

- AE1. **Covers R4.** **Given** a real Roboflow export whose `data.yaml` declares `train: ../train/images`, **when** the user runs the script with that ZIP, **then** training starts successfully without the user editing any file.
- AE2. **Covers R3, R7.** **Given** a ZIP missing `valid/labels`, **when** the user runs the script, **then** output names `valid/labels` as missing, shows the expected structure, and no training run or library change occurs.
- AE3. **Covers R8.** **Given** `config/config.yaml` with `yolo_model_size: m` and no `--model` flag, **when** training starts, **then** the base model is `yolo11m.pt`.
- AE4. **Covers R15, R16.** **Given** a completed training run and the model selected in the GUI, **when** the user runs a highlight analysis, **then** the pipeline loads the trained `best.pt` and reports detections from the dataset's classes.
- AE5. **Covers R17-R19.** **Given** theat (2 classes) already imported, **when** the user imports zombie-class-decection (5 classes), **then** the library holds one 7-class dataset and the trained model detects objects from both.
- AE6. **Covers R22-R23.** **Given** any completed training run, **then** the console shows mAP50, mAP50-95, precision, recall, and per-class AP, and `metrics.json` exists next to `best.pt` with the same values.
- AE7. **Covers R19.** **Given** theat version 2 already in the library, **when** the user imports the same ZIP again, **then** the script reports it as already imported and does not duplicate images or retrain unless forced.

### Scope Boundaries

- **Deferred for later:** the GUI "Object Detection Training" tab (dataset browser, progress charts); running COCO classes and custom classes in the same detection pass; removing a dataset from the library (add-only for v1 — rebuilding the library from kept ZIPs is the workaround).
- **Out of scope:** archive formats other than ZIP; converting non-YOLO annotation formats (COCO JSON, Pascal VOC); classification datasets and models — the pipeline has no image-classifier stage to consume a YOLO-cls model, and the action classifiers it does use are video models trained from labeled clips via the existing `model_training/` path, not from annotation-platform exports; any change to the labeler → `training/train_yolo.py` workflow; retraining or replacing the stock `yolo11m.pt`; packaging the training script into the exe (training scripts are dev-only, consistent with the build workflow).

### Dependencies / Assumptions

- Ultralytics (8.4.90 in the venv) performs training and evaluation; detection `.pt` files embed class names, which the pipeline's custom branch reads. `pyyaml` and `ultralytics` are already declared in `requirements.txt`.
- The pipeline's custom-detector branch works as written and only lacks reachability; verified by code reading, proven live by AE4's manual run.
- Verified during the brainstorm's live dry-run on `temp/theat.v2i.yolov11.zip`: extraction, validation (2,360/323/161 images), path rewriting, and fine-tuning from `yolo11m.pt` on CUDA (RTX 5080) work end-to-end.

### Sources / Research

- `pipeline.py:1164-1169` — base model resolved from `yolo_model_size` (code default `n`; the user's `config/config.yaml` sets `m`); `pipeline.py:1264-1290` — the custom-detector branch and its embedded-class-names fallback.
- `main.py:1413` — `advanced_cfg` load; `main.py:1449-1457` — Detector-type dropdown offers only "standard"; `main.py:1453` — `_custom_pose_model = None`, never reassigned (dead state); `main.py:2372-2448` (`build_pipeline_config`) and `main.py:3800-3817` (inline dict in `run_pipeline`) — the two `gui_config` builders, both already threading `yolo_type`/`yolo_custom_model_path`, both stripping `None` values (`main.py:2448`, `main.py:3833`); `main.py:2987-2997` — `save_config`'s `advanced:` block, which must persist the new key; `main.py:3154-3175` — label selector's custom branch.
- `modules/video_cache.py:87-114` — the "yolo identity" block of `build_analysis_cache_params`; `tests/test_video_cache.py:242-509` — the established style for signature-coverage tests.
- `modules/app_paths.py:59-82` — `latest_custom_pose_model()`'s task-blind `**/weights/best.pt` glob; `modules/app_paths.py:97-116` — `custom_keypoint_names()` callers at `main.py:3159-3160` and `pipeline.py:1288-1289`.
- `training/train_yolo.py` — conventions this feature follows: device policy (`:553-562`), patience 20, warm-start weights, `keypoint_names.json` sidecar (`:656-663`).
- Test infrastructure: `tests/conftest.py:37-66` shims heavy deps (cv2, torch, ultralytics, openvino, **yaml**) as `MagicMock` when not importable — the dev install is pytest+numpy only, so new module code must keep yaml parsing at its edge; `.github/workflows/tests.yml:16-21` runs `pytest -q` on every push; `tests/test_local_import_completeness.py` fails CI if a new module or script is not git-tracked.
- Packaging: no `.spec` file; PyInstaller args inline in `.github/workflows/build-release.yaml` — `modules/` is bundled via `--add-data "modules;modules"`, `training/` is never bundled.
- Real dataset exports for testing: `temp/theat.v2i.yolov11.zip` (2 classes, duplicate `data.yaml` entries), `temp/Dead By Daylight.v2i.yolov11.zip` (21 classes), `temp/zombie-class-decection.v6i.yolov11.zip` (5 classes, non-English names). All three carry the `../`-style path quirk (R4).

---

## Planning Contract

### Key Technical Decisions

- **KTD1. Two dependency-light modules, yaml at the edge.** `modules/dataset_import.py` (extraction, validation, `data.yaml` rewrite) and `modules/dataset_library.py` (merge, remap, provenance, summary, sidecar helpers). Pure functions take parsed dicts and paths; the only `yaml.safe_load`/`yaml.dump` calls live in thin loader/writer functions. Reason: CI's dev install shims `yaml` as `MagicMock` (`tests/conftest.py`), so anything testable must not depend on real yaml parsing inside the logic under test.
- **KTD2. Thin CLI orchestrator.** `training/train_object.py` holds argparse and the ultralytics calls; argument/default resolution (config-derived base model, flag precedence, `--conf` accepted as an alias of `--config`) lives in the modules so it is unit-testable without ultralytics.
- **KTD3. Config duality handled explicitly.** The script reads the config given by `--config` (default `config/config.yaml`, the user's live file). The printed config snippet targets that same file, with a one-line reminder that the app reads the repo-root `config.yaml` unless launched with `--conf` — this repo's known accessor-duality footgun, stated rather than hidden.
- **KTD4. Library location: `datasets/library/`.** Durable, outside `temp/` (R2), distinct from the existing `dataset/` directory used by the keypoint labeler. Layout: merged `train/valid/test` image/label dirs, a rewritten `data.yaml`, and a `library.json` manifest (imports with provenance, class list). Added to `.gitignore` — training data is not committed.
- **KTD5. Training outputs under `models/`, ultralytics-native layout.** `--project models` with a run name derived from the dataset (standalone) or `library` (enrichment); ultralytics' native run-increment behavior provides versioned directories (R13) — no `exist_ok=True`.
- **KTD6. Cache signature extension.** `yolo_type` and `yolo_custom_model_path` join the "yolo identity" block of `build_analysis_cache_params()`. Match-affecting only — no training-script settings enter the signature (repo cache-signature discipline).
- **KTD7. Pose-model lookup scoped by sidecar.** `latest_custom_pose_model()` only considers `best.pt` candidates with a `keypoint_names.json` sidecar (which `training/train_yolo.py` already writes) or the explicit `custom_keypoints.pt` drop-in. Detect runs write `classes.json` instead, so they can never hijack pose resolution.
- **KTD8. GUI wiring replaces the dead state.** The restored "Custom model" combo item and a Browse row (single-file picker, `*.pt` filter — first use of the singular `QFileDialog.getOpenFileName` in this codebase) back `yolo_custom_model_path`; the unused `_custom_pose_model` attribute is retired rather than left alongside. Empty picker text coerces to `None` so both `gui_config` builders' `None`-stripping filters behave; `save_config`'s `advanced:` block persists the key.
- **KTD9. Class names for the GUI come from `classes.json`, not the model.** A small reader helper in `modules/dataset_library.py` resolves a custom detect model's class list from its sidecar; the label selector uses it in custom mode. Keeps torch out of the GUI path, mirroring `yolo_objects_labels.json`'s role for COCO.

### High-Level Technical Design

```mermaid
flowchart TB
  Z[dataset ZIP / folder / data.yaml] --> I[dataset_import: extract, validate, rewrite paths]
  I -->|standalone flag| T
  I -->|default| L[dataset_library: merge classes by name, remap label ids, record provenance]
  L --> T[train_object.py: fine-tune base model via ultralytics]
  C[config/config.yaml: yolo_model_size] --> T
  T --> O[models/run-N/: best.pt, metrics.json, classes.json, plots]
  O --> G[GUI: Custom model + Browse picker]
  G --> P[pipeline custom-detector branch]
  O -.classes.json.-> S[label selector class names]
  G --> V[video_cache signature: yolo_type + custom path]
```

Class-id remap on merge (directional example): theat contributes `0 non threat`, `1 threat-detection`; zombie then contributes `Comum`..`Tanque`, which land as combined ids `2..6`; every zombie label file's class column is rewritten accordingly. Merging is by exact class-name match — a name already in the library reuses its existing id.

---

## Implementation Units

### U1. Dataset import module: extract, validate, rewrite

- **Goal:** Pure, testable import logic — ZIP/folder/data.yaml intake, structural validation, Roboflow path rewrite.
- **Requirements:** R1-R7.
- **Dependencies:** None.
- **Files:** `modules/dataset_import.py` (new), `tests/test_dataset_import.py` (new).
- **Approach:** `zipfile`-based extraction with per-entry resolved-path containment check (zip-slip guard covering `../`, absolute, and drive-letter entries; tolerant of duplicate entries — last wins) and decompression-bomb caps (total uncompressed size, per-entry size, compression ratio ~100:1, entry count) surfaced as validation failures; locate `data.yaml` at root or one level down; validation returns a structured result (passed checks, failures, class list, per-split counts) that the CLI renders; path rewrite sets `path:` to the extracted root and split keys to `train/images` etc. Loader/writer functions isolate yaml per KTD1. Also hosts the argument/default-resolution helpers (config-derived base model, flag precedence, `--conf` alias handling) per KTD2, so U3's CLI stays a thin shell.
- **Patterns to follow:** `modules/cli_args.py` and `modules/keyword_scoring_editor.py` (dependency-light module discipline); `tests/test_video_cache.py` helpers for tmp_path fixture style.
- **Test scenarios:** Build tiny synthetic datasets in `tmp_path` (zipfile + generated label files):
  - Happy path: valid Roboflow-shaped ZIP → validation passes, counts and class list correct, rewritten `data.yaml` resolves inside the extract dir (covers AE1 mechanics).
  - Direct folder input and direct `data.yaml` input validate identically (R1).
  - Missing `valid/labels` → failure names it; result blocks training (covers AE2).
  - `data.yaml` nested one level down is found; missing entirely fails.
  - `nc` ≠ `len(names)` fails; label line with out-of-range class id fails; unreadable label file fails.
  - Duplicate `data.yaml` entries in the ZIP extract cleanly (R5).
  - Zip-slip entry (`../../evil.txt`) is rejected (R5).
  - Class names with spaces/hyphens/accents survive untouched (R6).
  - Missing `test/` split passes with a note (R3).
- **Verification:** `pytest -q tests/test_dataset_import.py` green on the pytest+numpy dev install (yaml shimmed).

### U2. Dataset library: merge, provenance, summary, sidecar helpers

- **Goal:** Persistent enrichment — merge validated datasets into `datasets/library/`, with class-name-keyed id remapping and provenance tracking.
- **Requirements:** R17-R21, R24 (reader helper).
- **Dependencies:** U1.
- **Files:** `modules/dataset_library.py` (new), `tests/test_dataset_library.py` (new), `.gitignore` (add `datasets/`).
- **Approach:** `library.json` manifest holds a schema-version field, imports (name, version, source, date, image counts), and the ordered combined class list. Metadata-derived names/versions are sanitized against a strict allow-list (`[A-Za-z0-9_.-]+`, rejecting empty/`.`/`..`) before use in filename prefixes or manifest keys, and constructed target paths are verified to resolve inside the library root — the same containment discipline U1 applies to ZIP entries. Merge copies images/labels with a source-prefix on filenames, rewrites each label file's class column through an old-id→new-id map built by name matching, and regenerates the library `data.yaml`; the manifest is written atomically (temp file + `os.replace`, mirroring `modules/video_cache.py`'s atomic_write_json) only after all copies and rewrites succeed, so an interrupted merge leaves the previous manifest intact and re-import is a clean redo; an unreadable/invalid manifest aborts with a clear rebuild-from-ZIPs message. Re-import detection keys on the Roboflow `project` + `version` metadata (R19) with a force flag. Summary reads the manifest only. Includes `write_classes_sidecar(run_dir, names)` and `read_classes_sidecar(model_path)` for `classes.json` (KTD9).
- **Test scenarios:**
  - Covers AE5: merge 2-class then 5-class dataset → 7 combined classes, second dataset's label ids remapped, per-source split structure preserved.
  - Same class name in both datasets → single shared id, no duplicate.
  - Filename collision across sources → both files present, source-prefixed.
  - Covers AE7: re-import of same project+version detected and skipped; force flag proceeds.
  - Summary reports datasets, classes, counts without touching images.
  - Manifest survives round-trip (write, reload, merge again).
  - `read_classes_sidecar` resolves names next to a model path; missing sidecar returns empty.
- **Verification:** `pytest -q tests/test_dataset_library.py` green on the dev install.

### U3. Training CLI: `train-object` script with metrics

- **Goal:** The user-facing command — orchestrates import→(merge)→train→metrics→handoff printout.
- **Requirements:** R8-R14, R18, R22-R24.
- **Dependencies:** U1, U2.
- **Files:** `training/train_object.py` (new), plus default-resolution functions and their tests in `modules/dataset_import.py` / `tests/test_dataset_import.py`.
- **Approach:** argparse flags: `--dataset` (required), `--config`/`--conf` (default `config/config.yaml`), `--model`, `--weights` (standalone only), `--epochs 100`, `--batch 16`, `--img-size 640`, `--device`, `--workers 4`, `--project models`, `--standalone`, `--keep-temp`, `--summary`, `--force`. Default base model = `yolo11{yolo_model_size}.pt` from the config, fallback `yolo11m.pt` (covers AE3 via unit test on the resolution function). Device policy and offline tolerance mirror `training/train_yolo.py:553-562` and the observed AMP-check behavior (R10, R11). After training: `model.val()` on the test split when present; write `metrics.json` and `classes.json` next to `best.pt`; print the metrics table and the config snippet with the KTD3 duality reminder. Patience 20, no `exist_ok` (versioned runs, R13).
- **Execution note:** Smoke-first — after wiring, run a 1-epoch standalone import against `temp/theat.v2i.yolov11.zip` end-to-end before relying on unit coverage for the orchestration glue.
- **Test scenarios:** (resolution functions only; the script body is smoke-verified)
  - Covers AE3: config with `yolo_model_size: m` → `yolo11m.pt`; missing/unreadable config → `yolo11m.pt`; `--model` beats both; `--weights` beats `--model` in standalone; `--weights` rejected in enrichment mode (R19's retrain-from-base rule).
  - `--conf` and `--config` are interchangeable.
  - Test expectation for the ultralytics call path: none — heavy training glue, covered by the smoke run and AE6's manual check.
- **Verification:** 1-epoch smoke run completes: run dir under `models/`, `best.pt` + `metrics.json` + `classes.json` present, console shows metrics table and config snippet, temp extraction cleaned (kept with `--keep-temp`).

### U4. Analysis-cache signature covers the detector choice

- **Goal:** Switching detector type or custom model path invalidates cached analysis instead of silently reusing stale detections.
- **Requirements:** R15 (correctness half).
- **Dependencies:** None.
- **Files:** `modules/video_cache.py`, `tests/test_video_cache.py`.
- **Approach:** Add `yolo_type` and `yolo_custom_model_path` to the "yolo identity" block of `build_analysis_cache_params()` **only when `yolo_type` is not "standard"** — the signature is a hash of the whole params dict, so unconditional inclusion would invalidate every existing cache file. Standard-mode params serialize byte-identically to today; custom mode gets a distinct signature. Nothing else enters the signature (KTD6).
- **Test scenarios:** In the file's established two-config style:
  - Same config twice → equal params (baseline).
  - `yolo_type` standard vs custom → different params.
  - Two different `yolo_custom_model_path` values → different params.
  - A standard-mode config's params dict contains neither new key (pinning the pre-change shape, so existing caches genuinely stay valid).
- **Verification:** `pytest -q tests/test_video_cache.py` green; existing signature tests unaffected.

### U5. Scope the pose-model lookup by sidecar

- **Goal:** A freshly trained object detector's `best.pt` can never hijack pose-model resolution.
- **Requirements:** Supports R13/R24 safety (adjacent fix confirmed in planning dialogue).
- **Dependencies:** None.
- **Files:** `modules/app_paths.py`, `tests/test_app_paths_pose_lookup.py` (new).
- **Approach:** `latest_custom_pose_model()` filters glob candidates to those with a `keypoint_names.json` sidecar in the same directory, keeping the explicit `custom_keypoints.pt` drop-in unconditionally. `training/train_yolo.py` already writes that sidecar, so existing pose models keep working.
- **Test scenarios:**
  - `best.pt` with `keypoint_names.json` sibling → returned.
  - Newer `best.pt` with only `classes.json` sibling → ignored; older pose model still wins.
  - `custom_keypoints.pt` drop-in returned without any sidecar.
  - No candidates → `None`.
- **Verification:** `pytest -q tests/test_app_paths_pose_lookup.py` green; existing `tests/test_app_paths_*` suites unaffected.

### U6. GUI wiring: custom detector selectable end-to-end

- **Goal:** The trained model is selectable in the GUI, persists through config saves, reaches the pipeline, and lists its classes in the label selector.
- **Requirements:** R15, R16, R24 (GUI half).
- **Dependencies:** U2 (sidecar reader), U4 (signature must land first or in the same change so switching is cache-correct).
- **Files:** `main.py`.
- **Approach:** Add `("Custom model (trained detector)", "custom")` to the Detector-type combo; seed from `advanced_cfg` so a config-file value round-trips. The existing `on_yolo_type_changed` handler's custom-mode placeholder hardcodes the size-combo data to `"n"` (`main.py:1471`) — it must carry the previously selected size instead, so `save_config` and the standard-mode restore round-trip the configured `yolo_model_size` unchanged. Add a model-path row (QLineEdit + Browse via single-file `QFileDialog.getOpenFileName`, `*.pt` filter, shown/enabled in custom mode) following the Browse-row pattern at `main.py:1025-1034` (singular-picker precedent: `llm/llm_chat_widget.py:1449`). Retire `_custom_pose_model`; both `gui_config` builders (`build_pipeline_config` and `run_pipeline`'s inline dict) read the picker, coercing empty text to `None` (KTD8 — both builders, or the second call path stays broken). Persist `yolo_custom_model_path` in `save_config`'s `advanced:` block. In the label selector's custom branch, list classes from `read_classes_sidecar` with the existing keypoint-names path as fallback.
- **Test scenarios:** Test expectation: none — Qt GUI code with no test harness in this repo; behavior is covered by the manual smoke below and AE4.
- **Verification:** Manual smoke (launch the app with `--conf config/config.yaml` so saves target that file): select Custom model, browse to the trained run's `best.pt`, save config (`config/config.yaml` gains the key; confirm `yolo_model_size` is unchanged), restart app (selection restored), open label selector and **select the model's classes** (the pipeline skips object detection when `highlight_objects` is empty), run an analysis on a short clip (log shows the custom model loading; a second run hits cache; switching back to standard recomputes).

---

## Verification Contract

| Gate | Command / procedure | Proves |
|---|---|---|
| Unit tests | `pytest -q` (pytest+numpy dev install, as CI runs it) | U1, U2, U4, U5 logic; import-completeness gate passes for the new tracked files |
| Standalone smoke | 1-epoch run against `temp/theat.v2i.yolov11.zip` with the standalone flag | U3 orchestration, AE1, AE2 (rerun with a broken copy), AE3, AE6 |
| Enrichment smoke | Import theat then zombie ZIPs, 1 epoch | AE5, AE7, F4; `datasets/library/` layout and manifest |
| App smoke | U6's manual procedure on a short clip | AE4, F3, cache recompute on model switch |

New files must be `git add`ed before running the suite — `tests/test_local_import_completeness.py` fails on untracked modules by design.

## Definition of Done

- All six units landed with their per-unit verification satisfied.
- `pytest -q` green locally and in CI (`.github/workflows/tests.yml`).
- AE1-AE7 demonstrated (AE1-AE3, AE5-AE7 via unit tests + smokes; AE4 via the app smoke).
- The printed config snippet applied to a fresh config actually activates the model (KTD3's duality note verified once against the repo-root config path).
- No leftover experimental code, temp datasets, or abandoned run directories in the diff; `datasets/` gitignored.
