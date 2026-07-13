"""YOLO dataset import: extraction, validation, and data.yaml path rewriting.

Dependency-light module (stdlib + yaml at the edges) so it's testable without
the training stack. All validation logic operates on parsed dicts and paths;
the only yaml calls live in load_yaml_file()/dump_yaml_file()/load_app_config()
so tests can exercise everything else with plain dicts (CI shims yaml as a
MagicMock -- see tests/conftest.py).
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Decompression-bomb caps. Image datasets are large but bounded; a crafted
# archive that exceeds these fails validation instead of exhausting disk.
MAX_TOTAL_UNCOMPRESSED = 20 * 1024**3   # 20 GiB across the whole archive
MAX_ENTRY_UNCOMPRESSED = 2 * 1024**3    # 2 GiB for any single entry
MAX_ENTRY_COUNT = 200_000
MAX_COMPRESSION_RATIO = 100             # only enforced on entries > 1 MiB
_RATIO_FLOOR = 1024**2

REQUIRED_SPLITS = ("train", "valid")
OPTIONAL_SPLITS = ("test",)

EXPECTED_STRUCTURE = """data.yaml
train/
    images/
    labels/
valid/
    images/
    labels/
test/            (optional)
    images/
    labels/"""


class DatasetImportError(Exception):
    """Raised when an archive cannot be safely extracted."""


@dataclass
class ValidationResult:
    passed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    class_names: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # split -> image count

    @property
    def ok(self) -> bool:
        return not self.failures


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def extract_zip(zip_path, dest_dir) -> Path:
    """Extract a dataset ZIP with zip-slip and decompression-bomb guards.

    Duplicate entries are tolerated (last one wins, matching zipfile's own
    extractall behavior). Containment uses resolved paths, so '../', absolute,
    and drive-letter entries are all rejected by the same check.
    """
    zip_path, dest = Path(zip_path), Path(dest_dir)
    if not zip_path.exists():
        raise DatasetImportError(f"dataset not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise DatasetImportError(f"not a valid ZIP archive: {zip_path}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        if len(entries) > MAX_ENTRY_COUNT:
            raise DatasetImportError(
                f"archive has {len(entries):,} entries (max {MAX_ENTRY_COUNT:,})")
        total = 0
        for entry in entries:
            total += entry.file_size
            if entry.file_size > MAX_ENTRY_UNCOMPRESSED:
                raise DatasetImportError(
                    f"entry too large: {entry.filename} "
                    f"({entry.file_size:,} bytes)")
            if (entry.file_size > _RATIO_FLOOR and entry.compress_size > 0
                    and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO):
                raise DatasetImportError(
                    f"suspicious compression ratio on {entry.filename}")
            if not _is_within(dest, dest / entry.filename):
                raise DatasetImportError(
                    f"entry escapes extraction directory: {entry.filename}")
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise DatasetImportError(
                f"archive expands to {total:,} bytes (max {MAX_TOTAL_UNCOMPRESSED:,})")
        zf.extractall(dest)
    return dest


def find_data_yaml(root) -> Path | None:
    """Locate data.yaml at the dataset root or one directory level down."""
    root = Path(root)
    direct = root / "data.yaml"
    if direct.is_file():
        return direct
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        nested = child / "data.yaml"
        if nested.is_file():
            return nested
    return None


def count_images(split_dir: Path) -> int:
    images = split_dir / "images"
    if not images.is_dir():
        return 0
    return sum(1 for p in images.iterdir() if p.suffix.lower() in IMG_EXTS)


def validate_dataset(root, spec: dict) -> ValidationResult:
    """Validate a YOLO dataset layout against its parsed data.yaml spec.

    `root` is the directory containing the split folders (data.yaml's parent).
    Pure logic over paths and a dict -- no yaml access.
    """
    root = Path(root)
    result = ValidationResult()

    names = spec.get("names")
    nc = spec.get("nc")
    if not names or not isinstance(names, list):
        result.failures.append("data.yaml has no class names")
        return result
    if nc is not None and nc != len(names):
        result.failures.append(
            f"class definitions inconsistent: nc={nc} but {len(names)} names")
        return result
    result.class_names = [str(n) for n in names]
    result.passed.append(f"class definitions ({len(names)} classes)")

    for split in REQUIRED_SPLITS + OPTIONAL_SPLITS:
        split_dir = root / split
        required = split in REQUIRED_SPLITS
        if not split_dir.is_dir():
            if required:
                result.failures.append(f"{split}/")
            else:
                result.notes.append(f"{split}/ absent (optional)")
            continue
        missing = [f"{split}/{sub}" for sub in ("images", "labels")
                   if not (split_dir / sub).is_dir()]
        if missing:
            if required:
                result.failures.extend(missing)
            else:
                result.notes.extend(f"{m} absent (optional)" for m in missing)
            continue
        n_images = count_images(split_dir)
        if required and n_images == 0:
            result.failures.append(f"{split}/images is empty")
            continue
        label_error = _check_labels(split_dir / "labels", len(names))
        if label_error:
            result.failures.append(f"{split}/labels: {label_error}")
            continue
        result.counts[split] = n_images
        result.passed.append(f"{split}: {n_images:,} images")
    return result


def _check_labels(labels_dir: Path, num_classes: int) -> str | None:
    for lbl in labels_dir.glob("*.txt"):
        try:
            text = lbl.read_text(encoding="utf-8")
        except OSError as e:
            return f"unreadable label file {lbl.name}: {e}"
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if not parts[0].isdigit() or int(parts[0]) >= num_classes:
                return f"bad class id in {lbl.name}: {line!r}"
    return None


def rewrite_spec_paths(spec: dict, root) -> dict:
    """Return a copy of the data.yaml spec with paths anchored inside `root`.

    Roboflow exports declare '../train/images'-style paths that escape the
    extracted folder; this pins `path:` to the dataset root and the split keys
    to their conventional relative locations (dropping declared-but-absent
    optional splits).
    """
    root = Path(root)
    fixed = dict(spec)
    fixed["path"] = str(root.resolve())
    fixed["train"] = "train/images"
    fixed["val"] = "valid/images"
    if (root / "test" / "images").is_dir():
        fixed["test"] = "test/images"
    else:
        fixed.pop("test", None)
    return fixed


def format_report(result: ValidationResult) -> str:
    """Render a ValidationResult as the console report the CLI prints."""
    lines = []
    for item in result.passed:
        lines.append(f"[ok] {item}")
    for note in result.notes:
        lines.append(f"[--] {note}")
    if result.ok:
        lines.append("")
        lines.append("Classes")
        for i, name in enumerate(result.class_names):
            lines.append(f"  {i} - {name}")
        lines.append("")
        lines.append("Dataset validation passed.")
    else:
        lines.append("")
        lines.append("Dataset validation FAILED")
        lines.append("Missing or invalid:")
        for item in result.failures:
            lines.append(f"  {item}")
        lines.append("")
        lines.append("Expected dataset structure:")
        lines.append(EXPECTED_STRUCTURE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI argument/default resolution (KTD2): pure functions the training script
# calls, unit-testable without ultralytics.
# ---------------------------------------------------------------------------

DEFAULT_BASE_MODEL = "yolo11m.pt"


def resolve_input_kind(path) -> str:
    """Classify --dataset input: 'zip', 'yaml', or 'dir'."""
    p = Path(path)
    if p.is_dir():
        return "dir"
    if p.suffix.lower() in (".yaml", ".yml"):
        return "yaml"
    return "zip"


def resolve_base_model(app_config: dict | None, model_flag: str | None) -> str:
    """Base checkpoint: --model flag > config's yolo_model_size > yolo11m.pt."""
    if model_flag:
        return model_flag
    size = ((app_config or {}).get("advanced") or {}).get("yolo_model_size")
    if isinstance(size, str) and size.strip().lower() in ("n", "s", "m", "l", "x"):
        return f"yolo11{size.strip().lower()}.pt"
    return DEFAULT_BASE_MODEL


def resolve_start_checkpoint(app_config: dict | None, model_flag: str | None,
                             weights_flag: str | None, standalone: bool) -> str:
    """Checkpoint training starts from.

    --weights wins in standalone mode; enrichment always retrains from the
    base model (fine-tuning the previous custom checkpoint on a grown library
    would forget earlier classes), so --weights there is an error.
    """
    if weights_flag:
        if not standalone:
            raise ValueError(
                "--weights is only valid with --standalone: enrichment always "
                "retrains from the base model so earlier classes are kept")
        return weights_flag
    return resolve_base_model(app_config, model_flag)


# --- yaml edge functions (the only yaml access in this module) --------------

def load_yaml_file(path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def dump_yaml_file(data: dict, path) -> None:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True)


def load_app_config(path) -> dict:
    """Read the app config for defaults; {} when missing or unreadable."""
    try:
        return load_yaml_file(path)
    except Exception:
        return {}
