"""Tests for modules/dataset_import.py -- pure logic, no yaml/ultralytics.

Datasets are built as plain files in tmp_path and specs passed as dicts, so
everything runs on the pytest+numpy dev install (yaml is shimmed in CI).
"""

import zipfile

import pytest

from modules import dataset_import as di


def make_dataset(root, names=("non threat", "threat-detection"),
                 splits=("train", "valid", "test"), images_per_split=3,
                 class_id=0):
    """Build a minimal valid YOLO dataset layout under root."""
    for split in splits:
        img_dir = root / split / "images"
        lbl_dir = root / split / "labels"
        img_dir.mkdir(parents=True)
        lbl_dir.mkdir(parents=True)
        for i in range(images_per_split):
            (img_dir / f"{split}_{i}.jpg").write_bytes(b"\xff\xd8fake")
            (lbl_dir / f"{split}_{i}.txt").write_text(
                f"{class_id} 0.5 0.5 0.2 0.2\n")
    return {"nc": len(names), "names": list(names),
            "train": "../train/images", "val": "../valid/images",
            "test": "../test/images"}


# --- validation --------------------------------------------------------------

def test_valid_dataset_passes(tmp_path):
    spec = make_dataset(tmp_path)
    result = di.validate_dataset(tmp_path, spec)
    assert result.ok
    assert result.class_names == ["non threat", "threat-detection"]
    assert result.counts == {"train": 3, "valid": 3, "test": 3}


def test_missing_valid_labels_fails_and_names_it(tmp_path):
    spec = make_dataset(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "valid" / "labels")
    result = di.validate_dataset(tmp_path, spec)
    assert not result.ok
    assert any("valid/labels" in f for f in result.failures)
    report = di.format_report(result)
    assert "valid/labels" in report
    assert "Expected dataset structure" in report


def test_missing_test_split_is_optional(tmp_path):
    spec = make_dataset(tmp_path, splits=("train", "valid"))
    result = di.validate_dataset(tmp_path, spec)
    assert result.ok
    assert any("test/ absent" in n for n in result.notes)


def test_nc_names_mismatch_fails(tmp_path):
    spec = make_dataset(tmp_path)
    spec["nc"] = 5
    result = di.validate_dataset(tmp_path, spec)
    assert not result.ok
    assert any("nc=5" in f for f in result.failures)


def test_out_of_range_class_id_fails(tmp_path):
    spec = make_dataset(tmp_path)
    (tmp_path / "train" / "labels" / "bad.txt").write_text("9 0.5 0.5 0.1 0.1\n")
    result = di.validate_dataset(tmp_path, spec)
    assert not result.ok
    assert any("bad class id" in f for f in result.failures)


def test_unreadable_label_file_fails(tmp_path):
    spec = make_dataset(tmp_path)
    # A directory with a .txt name is unreadable as a file on every platform.
    (tmp_path / "train" / "labels" / "trap.txt").mkdir()
    result = di.validate_dataset(tmp_path, spec)
    assert not result.ok
    assert any("unreadable" in f for f in result.failures)


def test_empty_train_images_fails(tmp_path):
    spec = make_dataset(tmp_path)
    for img in (tmp_path / "train" / "images").iterdir():
        img.unlink()
    result = di.validate_dataset(tmp_path, spec)
    assert not result.ok
    assert any("train/images is empty" in f for f in result.failures)


def test_non_ascii_class_names_pass_through(tmp_path):
    names = ("Comum", "Estrategista", "coração-mutante")
    spec = make_dataset(tmp_path, names=names, class_id=2)
    result = di.validate_dataset(tmp_path, spec)
    assert result.ok
    assert result.class_names == list(names)


# --- data.yaml discovery and rewrite -----------------------------------------

def test_find_data_yaml_at_root_and_nested(tmp_path):
    assert di.find_data_yaml(tmp_path) is None
    nested = tmp_path / "export"
    nested.mkdir()
    (nested / "data.yaml").write_text("nc: 1\n")
    assert di.find_data_yaml(tmp_path) == nested / "data.yaml"
    (tmp_path / "data.yaml").write_text("nc: 1\n")
    assert di.find_data_yaml(tmp_path) == tmp_path / "data.yaml"


def test_rewrite_spec_paths_pins_inside_root(tmp_path):
    spec = make_dataset(tmp_path)
    fixed = di.rewrite_spec_paths(spec, tmp_path)
    assert fixed["path"] == str(tmp_path.resolve())
    assert fixed["train"] == "train/images"
    assert fixed["val"] == "valid/images"
    assert fixed["test"] == "test/images"
    assert spec["train"] == "../train/images"  # original untouched


def test_rewrite_spec_paths_drops_absent_test_split(tmp_path):
    spec = make_dataset(tmp_path, splits=("train", "valid"))
    fixed = di.rewrite_spec_paths(spec, tmp_path)
    assert "test" not in fixed


# --- extraction guards --------------------------------------------------------

def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)


def test_extract_valid_zip_with_duplicate_entries(tmp_path):
    zp = tmp_path / "ds.zip"
    _write_zip(zp, [("data.yaml", "nc: 1\n"), ("data.yaml", "nc: 1\n"),
                    ("train/images/a.jpg", "x")])
    dest = di.extract_zip(zp, tmp_path / "out")
    assert (dest / "data.yaml").is_file()
    assert (dest / "train" / "images" / "a.jpg").is_file()


def test_extract_rejects_zip_slip_entry(tmp_path):
    zp = tmp_path / "evil.zip"
    _write_zip(zp, [("../evil.txt", "boom")])
    with pytest.raises(di.DatasetImportError, match="escapes"):
        di.extract_zip(zp, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_extract_rejects_non_zip(tmp_path):
    fake = tmp_path / "not.zip"
    fake.write_text("plain text")
    with pytest.raises(di.DatasetImportError, match="not a valid ZIP"):
        di.extract_zip(fake, tmp_path / "out")


def test_extract_missing_file_says_not_found(tmp_path):
    with pytest.raises(di.DatasetImportError, match="not found"):
        di.extract_zip(tmp_path / "gone.zip", tmp_path / "out")


def test_extract_rejects_oversized_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "MAX_ENTRY_UNCOMPRESSED", 4)
    zp = tmp_path / "big.zip"
    _write_zip(zp, [("huge.bin", "x" * 100)])
    with pytest.raises(di.DatasetImportError, match="too large"):
        di.extract_zip(zp, tmp_path / "out")


def test_extract_rejects_entry_count_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "MAX_ENTRY_COUNT", 2)
    zp = tmp_path / "many.zip"
    _write_zip(zp, [(f"f{i}.txt", "x") for i in range(3)])
    with pytest.raises(di.DatasetImportError, match="entries"):
        di.extract_zip(zp, tmp_path / "out")


def test_extract_rejects_total_size_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "MAX_TOTAL_UNCOMPRESSED", 50)
    zp = tmp_path / "total.zip"
    _write_zip(zp, [("a.bin", "x" * 40), ("b.bin", "y" * 40)])
    with pytest.raises(di.DatasetImportError, match="expands to"):
        di.extract_zip(zp, tmp_path / "out")


# --- input classification and default resolution ------------------------------

def test_resolve_input_kind(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    assert di.resolve_input_kind(d) == "dir"
    assert di.resolve_input_kind(tmp_path / "data.yaml") == "yaml"
    assert di.resolve_input_kind(tmp_path / "ds.zip") == "zip"


def test_base_model_follows_config_size():
    cfg = {"advanced": {"yolo_model_size": "m"}}
    assert di.resolve_base_model(cfg, None) == "yolo11m.pt"


def test_base_model_defaults_when_config_missing_or_invalid():
    assert di.resolve_base_model({}, None) == "yolo11m.pt"
    assert di.resolve_base_model(None, None) == "yolo11m.pt"
    assert di.resolve_base_model({"advanced": {"yolo_model_size": "xxl"}},
                                 None) == "yolo11m.pt"


def test_model_flag_overrides_config():
    cfg = {"advanced": {"yolo_model_size": "n"}}
    assert di.resolve_base_model(cfg, "yolo11x.pt") == "yolo11x.pt"


def test_weights_allowed_only_in_standalone():
    assert di.resolve_start_checkpoint({}, None, "prev/best.pt",
                                       standalone=True) == "prev/best.pt"
    with pytest.raises(ValueError, match="standalone"):
        di.resolve_start_checkpoint({}, None, "prev/best.pt", standalone=False)


def test_start_checkpoint_falls_back_to_base_model():
    cfg = {"advanced": {"yolo_model_size": "s"}}
    assert di.resolve_start_checkpoint(cfg, None, None,
                                       standalone=False) == "yolo11s.pt"
