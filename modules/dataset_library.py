"""Persistent dataset library: merge imported YOLO datasets into one growing
training set with a unified class list.

Dependency-light (stdlib only for all logic; yaml only in the data.yaml edge
function). The library lives at a durable location (datasets/library/ -- never
under temp/), holds merged train/valid/test splits, and a library.json
manifest recording schema version, imports with provenance, and the ordered
combined class list. The manifest is the source of truth; it is written
atomically after all file operations succeed, so an interrupted merge leaves
the previous state intact and re-running the import is a clean redo
(deterministic source-prefixed filenames make re-copies idempotent).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from modules.dataset_import import IMG_EXTS

SCHEMA_VERSION = 1
MANIFEST_NAME = "library.json"
SPLITS = ("train", "valid", "test")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class DatasetLibraryError(Exception):
    pass


def sanitize_source_name(name) -> str:
    """Allow-list a metadata-derived name for use in paths and manifest keys.

    Roboflow metadata travels inside the untrusted archive, so it gets the
    same containment discipline as ZIP entry names.
    """
    cleaned = _SAFE_NAME.sub("_", str(name or "")).strip("._")
    if not cleaned or set(cleaned) <= {".", "_", "-"}:
        raise DatasetLibraryError(f"unusable dataset name: {name!r}")
    return cleaned


def provenance_from_spec(spec: dict, fallback_name: str) -> dict:
    rf = spec.get("roboflow") or {}
    name = rf.get("project") or fallback_name
    return {
        "name": sanitize_source_name(name),
        "version": str(rf.get("version", "")) or None,
        "url": rf.get("url"),
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    # temp file + os.replace so a crash mid-write never corrupts the manifest
    # (mirrors modules/video_cache.py's atomic_write_json)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_manifest() -> dict:
    return {"schema_version": SCHEMA_VERSION, "classes": [], "imports": []}


def load_manifest(library_root) -> dict:
    """Load the library manifest; fresh manifest when the library is new."""
    path = Path(library_root) / MANIFEST_NAME
    if not path.exists():
        return new_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or "classes" not in data
                or "imports" not in data):
            raise ValueError("missing required manifest keys")
    except (OSError, ValueError) as e:
        raise DatasetLibraryError(
            f"library manifest unreadable ({path}): {e}. The library state is "
            f"unknown -- delete the library folder and rebuild by re-importing "
            f"your dataset ZIPs.") from e
    if data.get("schema_version", 0) > SCHEMA_VERSION:
        raise DatasetLibraryError(
            f"library manifest schema {data.get('schema_version')} is newer "
            f"than this app supports ({SCHEMA_VERSION})")
    return data


def find_import(manifest: dict, name: str, version) -> dict | None:
    for entry in manifest.get("imports", []):
        if entry.get("name") == name and entry.get("version") == version:
            return entry
    return None


@dataclass
class MergeStats:
    source: str
    added_classes: list[str]
    copied: dict[str, int]      # split -> images copied
    total_classes: int
    skipped: bool = False


def merge_dataset(library_root, dataset_root, spec: dict,
                  force: bool = False) -> MergeStats:
    """Merge a validated dataset into the library.

    Class lists are unioned by exact name (an existing name reuses its id);
    the source's label class-ids are rewritten through the resulting map;
    files are copied with a sanitized source prefix. The manifest is updated
    only after every copy and rewrite succeeded.
    """
    library_root = Path(library_root)
    dataset_root = Path(dataset_root)
    manifest = load_manifest(library_root)

    prov = provenance_from_spec(spec, fallback_name=dataset_root.name)
    source = prov["name"]
    existing = find_import(manifest, source, prov["version"])
    if existing and not force:
        return MergeStats(source=source, added_classes=[], copied={},
                          total_classes=len(manifest["classes"]), skipped=True)

    names = [str(n) for n in spec.get("names", [])]
    if not names:
        raise DatasetLibraryError("dataset spec has no class names")

    merged = list(manifest["classes"])
    added = [n for n in names if n not in merged]
    merged.extend(added)
    id_map = {old: merged.index(n) for old, n in enumerate(names)}

    library_root.mkdir(parents=True, exist_ok=True)
    lib_resolved = library_root.resolve()
    copied: dict[str, int] = {}
    for split in SPLITS:
        img_src = dataset_root / split / "images"
        lbl_src = dataset_root / split / "labels"
        if not img_src.is_dir() or not lbl_src.is_dir():
            continue
        img_dst = library_root / split / "images"
        lbl_dst = library_root / split / "labels"
        img_dst.mkdir(parents=True, exist_ok=True)
        lbl_dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for img in img_src.iterdir():
            if img.suffix.lower() not in IMG_EXTS:
                continue
            lbl = lbl_src / (img.stem + ".txt")
            if not lbl.exists():
                continue
            img_target = img_dst / f"{source}_{img.name}"
            lbl_target = lbl_dst / f"{source}_{img.stem}.txt"
            for target in (img_target, lbl_target):
                if lib_resolved not in target.resolve().parents:
                    raise DatasetLibraryError(
                        f"merge target escapes library: {target}")
            shutil.copy2(img, img_target)
            lines = []
            for line in lbl.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if not parts:
                    continue
                parts[0] = str(id_map[int(parts[0])])
                lines.append(" ".join(parts))
            lbl_target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            n += 1
        copied[split] = n

    manifest["classes"] = merged
    manifest["imports"] = [e for e in manifest["imports"]
                           if not (e.get("name") == source
                                   and e.get("version") == prov["version"])]
    manifest["imports"].append({
        **prov,
        "date": date.today().isoformat(),
        "images": copied,
    })
    _atomic_write_json(library_root / MANIFEST_NAME, manifest)
    return MergeStats(source=source, added_classes=added, copied=copied,
                      total_classes=len(merged))


def library_spec(library_root, manifest: dict) -> dict:
    """The library's data.yaml content (write with write_library_data_yaml)."""
    root = Path(library_root)
    spec = {
        "path": str(root.resolve()),
        "train": "train/images",
        "val": "valid/images",
        "nc": len(manifest["classes"]),
        "names": list(manifest["classes"]),
    }
    if (root / "test" / "images").is_dir():
        spec["test"] = "test/images"
    return spec


def summarize(library_root) -> dict:
    """Library contents without touching image files (manifest only)."""
    manifest = load_manifest(Path(library_root))
    totals: dict[str, int] = {}
    for entry in manifest["imports"]:
        for split, n in (entry.get("images") or {}).items():
            totals[split] = totals.get(split, 0) + int(n)
    return {
        "classes": list(manifest["classes"]),
        "imports": [
            {"name": e.get("name"), "version": e.get("version"),
             "date": e.get("date"), "images": e.get("images")}
            for e in manifest["imports"]
        ],
        "image_totals": totals,
    }


# --- model sidecars (KTD9) ---------------------------------------------------

CLASSES_SIDECAR = "classes.json"


def write_classes_sidecar(run_dir, names: list) -> Path:
    path = Path(run_dir) / CLASSES_SIDECAR
    _atomic_write_json(path, {"names": [str(n) for n in names]})
    return path


def read_classes_sidecar(model_path) -> list[str]:
    """Class names for a trained model, from the classes.json next to it.

    Empty list when absent/unreadable -- callers fall back to other sources.
    """
    path = Path(model_path).parent / CLASSES_SIDECAR
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("names")
        return [str(n) for n in names] if isinstance(names, list) else []
    except (OSError, ValueError):
        return []


# --- yaml edge function ------------------------------------------------------

def write_library_data_yaml(library_root, manifest: dict) -> Path:
    from modules.dataset_import import dump_yaml_file
    path = Path(library_root) / "data.yaml"
    dump_yaml_file(library_spec(library_root, manifest), path)
    return path
