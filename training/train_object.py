"""Train a custom YOLO object detector from a dataset ZIP (Roboflow/CVAT
export), an extracted dataset folder, or a data.yaml path.

By default each import ENRICHES the persistent dataset library
(datasets/library/) -- classes are unioned by name, label ids remapped -- and
training runs over everything imported so far, from the base model, producing
one detector that knows every imported class. --standalone trains just the
given dataset instead, without touching the library.

Examples:
    python training/train_object.py --dataset ThreatDataset.zip
    python training/train_object.py --dataset ThreatDataset.zip --standalone --weights models/prev/weights/best.pt
    python training/train_object.py --summary

Dev-only script (never bundled into the packaged exe), like the other
training/ scripts. Heavy imports (ultralytics/torch) happen after argument
parsing so --help and --summary stay fast.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import dataset_import as di          # noqa: E402
from modules import dataset_library as dl         # noqa: E402

LIBRARY_ROOT = ROOT / "datasets" / "library"
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Import a YOLO dataset and train a custom object detector.")
    ap.add_argument("--dataset", help="Dataset ZIP, extracted folder, or data.yaml")
    ap.add_argument("--config", "--conf", dest="config", default=str(DEFAULT_CONFIG),
                    help=f"App config for defaults (default: {DEFAULT_CONFIG})")
    ap.add_argument("--model", help="Base model override (e.g. yolo11l.pt)")
    ap.add_argument("--weights", help="Fine-tune from this checkpoint (--standalone only)")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--img-size", type=int, default=640, dest="img_size")
    ap.add_argument("--device", default=None, help="cuda index or 'cpu' (default: auto)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--project", default=str(ROOT / "models"),
                    help="Output directory for training runs (default: models/)")
    ap.add_argument("--standalone", action="store_true",
                    help="Train only this dataset; do not touch the library")
    ap.add_argument("--keep-temp", action="store_true",
                    help="Keep the temporary ZIP extraction directory")
    ap.add_argument("--summary", action="store_true",
                    help="Show library contents and exit (no training)")
    ap.add_argument("--retrain", action="store_true",
                    help="Train on the current library without importing "
                         "anything (source ZIPs not needed)")
    ap.add_argument("--force", action="store_true",
                    help="Re-import a dataset version the library already has")
    return ap.parse_args(argv)


def resolve_device(flag):
    if flag is not None:
        return flag
    # Repo training policy: CUDA when present, otherwise CPU. Intel GPU is
    # never used for training (ultralytics has no stable XPU path) -- it is
    # reserved for OpenVINO inference. Mirrors training/train_yolo.py.
    try:
        import torch
        if torch.cuda.is_available():
            return 0
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            print("[i] Intel GPU detected -- training on CPU "
                  "(Intel GPU is for inference, not training).")
    except Exception:
        pass
    return "cpu"


def print_summary():
    try:
        summary = dl.summarize(LIBRARY_ROOT)
    except dl.DatasetLibraryError as e:
        sys.exit(f"[x] {e}")
    if not summary["imports"]:
        print("Library is empty -- import a dataset ZIP to start it.")
        return
    print(f"Dataset library: {LIBRARY_ROOT}")
    print(f"\nClasses ({len(summary['classes'])})")
    for i, name in enumerate(summary["classes"]):
        print(f"  {i} - {name}")
    print("\nImports")
    for imp in summary["imports"]:
        images = ", ".join(f"{k}: {v}" for k, v in (imp["images"] or {}).items())
        print(f"  {imp['name']} v{imp['version']} ({imp['date']})  {images}")
    totals = ", ".join(f"{k}: {v:,}" for k, v in summary["image_totals"].items())
    print(f"\nTotals  {totals}")


def import_dataset(args):
    """Returns (data_yaml_path, class_names, temp_dir_or_None)."""
    kind = di.resolve_input_kind(args.dataset)
    temp_dir = None
    if kind == "zip":
        temp_dir = Path(tempfile.mkdtemp(prefix="yolo_dataset_"))
        print("Importing dataset...\n")
        try:
            di.extract_zip(args.dataset, temp_dir)
        except di.DatasetImportError as e:
            sys.exit(f"[x] {e}")
        print("[ok] ZIP extracted")
        root = temp_dir
    elif kind == "yaml":
        root = Path(args.dataset).parent
    else:
        root = Path(args.dataset)

    data_yaml = di.find_data_yaml(root)
    if data_yaml is None:
        sys.exit(f"[x] data.yaml not found under {root}\n\n"
                 f"Expected dataset structure:\n{di.EXPECTED_STRUCTURE}")
    print(f"[ok] data.yaml found")
    dataset_root = data_yaml.parent

    spec = di.load_yaml_file(data_yaml)
    result = di.validate_dataset(dataset_root, spec)
    print(di.format_report(result))
    if not result.ok:
        sys.exit(1)

    if args.standalone:
        fixed = di.rewrite_spec_paths(spec, dataset_root)
        di.dump_yaml_file(fixed, data_yaml)
        run_name = dl.sanitize_source_name(
            dl.provenance_from_spec(spec, dataset_root.name)["name"])
        return data_yaml, result.class_names, temp_dir, run_name

    # Enrichment: merge into the persistent library, train on everything.
    try:
        stats = dl.merge_dataset(LIBRARY_ROOT, dataset_root, spec,
                                 force=args.force)
    except dl.DatasetLibraryError as e:
        sys.exit(f"[x] {e}")
    if stats.skipped:
        print(f"\n[i] '{stats.source}' is already in the library "
              f"(use --force to re-import). Training on the current library.")
    else:
        added = ", ".join(stats.added_classes) or "none (all classes known)"
        print(f"\n[ok] merged '{stats.source}' into the library "
              f"(new classes: {added})")
    manifest = dl.load_manifest(LIBRARY_ROOT)
    library_yaml = dl.write_library_data_yaml(LIBRARY_ROOT, manifest)
    print(f"[ok] library now has {len(manifest['classes'])} classes")
    return library_yaml, list(manifest["classes"]), temp_dir, "library"


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.summary:
        print_summary()
        return
    if not args.dataset and not args.retrain:
        sys.exit("[x] --dataset is required (or use --summary / --retrain)")

    app_config = di.load_app_config(args.config)
    try:
        start = di.resolve_start_checkpoint(app_config, args.model,
                                            args.weights, args.standalone)
    except ValueError as e:
        sys.exit(f"[x] {e}")

    if args.retrain:
        # Train on the library as it stands -- the library is self-contained,
        # so this works after the source ZIPs are long gone.
        try:
            manifest = dl.load_manifest(LIBRARY_ROOT)
        except dl.DatasetLibraryError as e:
            sys.exit(f"[x] {e}")
        if not manifest["imports"]:
            sys.exit("[x] library is empty -- import a dataset first")
        data_yaml = dl.write_library_data_yaml(LIBRARY_ROOT, manifest)
        print(f"[ok] retraining on the library "
              f"({len(manifest['classes'])} classes)")
        temp_dir, run_name = None, "library"
    else:
        data_yaml, class_names, temp_dir, run_name = import_dataset(args)
    device = resolve_device(args.device)
    print(f"\nBase model: {start}")
    print(f"Device: {device}")

    from ultralytics import YOLO  # heavy import deferred until training

    t0 = time.time()
    model = YOLO(start)
    model.train(
        data=str(data_yaml),
        epochs=args.epochs,
        batch=args.batch,
        imgsz=args.img_size,
        device=device,
        patience=20,
        # resolve() so a relative --project lands where the user said, not
        # under ultralytics' runs/detect default
        project=str(Path(args.project).resolve()),
        name=run_name,
        workers=args.workers,
        verbose=True,
    )
    best = Path(model.trainer.best)
    elapsed = time.time() - t0

    # Held-out metrics: prefer the test split when the dataset has one. The
    # library may have been enriched by another import while this run trained
    # (training is pinned by ultralytics' label cache, but a fresh val re-scans
    # the live folders) -- so a failed evaluation falls back to the
    # training-time validation metrics instead of losing the run's sidecars.
    final = YOLO(str(best))
    names = list(final.names.values())
    spec = di.load_yaml_file(data_yaml)
    split = "test" if spec.get("test") else "val"
    per_class = {}
    try:
        if len(spec.get("names") or []) != len(names):
            raise RuntimeError(
                f"dataset now has {len(spec.get('names') or [])} classes but "
                f"the model was trained on {len(names)} -- the library "
                f"changed during training")
        m = final.val(data=str(data_yaml), split=split, device=device)
        for i, v in zip(m.box.ap_class_index.tolist(),
                        m.box.maps[m.box.ap_class_index].tolist()):
            per_class[names[i]] = round(float(v), 4)
        headline = {
            "mAP50": round(float(m.box.map50), 4),
            "mAP50_95": round(float(m.box.map), 4),
            "precision": round(float(m.box.mp), 4),
            "recall": round(float(m.box.mr), 4),
        }
    except Exception as e:
        print(f"\n[!] held-out evaluation unavailable ({e}); "
              f"reporting training-time validation metrics instead.")
        split = "val (training-time)"
        rm = getattr(model.trainer, "metrics", {}) or {}
        headline = {
            "mAP50": round(float(rm.get("metrics/mAP50(B)", 0.0)), 4),
            "mAP50_95": round(float(rm.get("metrics/mAP50-95(B)", 0.0)), 4),
            "precision": round(float(rm.get("metrics/precision(B)", 0.0)), 4),
            "recall": round(float(rm.get("metrics/recall(B)", 0.0)), 4),
        }
    metrics = {
        "split": split,
        **headline,
        "per_class_AP50_95": per_class,
        "epochs": args.epochs,
        "training_seconds": round(elapsed),
    }
    (best.parent / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    dl.write_classes_sidecar(best.parent, names)

    print(f"\nMetrics ({split} split)")
    print(f"  mAP50:     {metrics['mAP50']}")
    print(f"  mAP50-95:  {metrics['mAP50_95']}")
    print(f"  precision: {metrics['precision']}")
    print(f"  recall:    {metrics['recall']}")
    for name, ap in per_class.items():
        print(f"  AP {name}: {ap}")
    print(f"  training time: {elapsed/60:.1f} min")
    print(f"\nModel: {best}")

    print("\nTo use this model in VideoHighlighter, set in the 'advanced:' "
          f"section of {args.config}:")
    print("  yolo_type: custom")
    print(f"  yolo_custom_model_path: {best}")
    print("(Note: launched without --conf, the app reads the repo-root "
          "config.yaml -- apply the settings to whichever config the app "
          "actually loads, or pick the model in the GUI's Detector type.)")

    if temp_dir is not None:
        if args.keep_temp:
            print(f"[i] kept temp extraction: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
