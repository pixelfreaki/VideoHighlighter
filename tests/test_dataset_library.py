"""Tests for modules/dataset_library.py -- merge, provenance, sidecars."""

import json

import pytest

from modules import dataset_library as dl
from tests.test_dataset_import import make_dataset


_seq = iter(range(10_000))


def _merge(tmp_path, name, names, version="1", class_id=0, force=False,
           images_per_split=2):
    src = tmp_path / f"src_{name}_{next(_seq)}"
    src.mkdir()
    spec = make_dataset(src, names=names, images_per_split=images_per_split,
                        class_id=class_id)
    spec["roboflow"] = {"project": name, "version": version,
                        "url": f"https://universe.roboflow.com/x/{name}"}
    return dl.merge_dataset(tmp_path / "library", src, spec, force=force)


def test_merge_two_datasets_unions_classes_and_remaps(tmp_path):
    s1 = _merge(tmp_path, "theat", ("non threat", "threat-detection"))
    assert s1.added_classes == ["non threat", "threat-detection"]
    s2 = _merge(tmp_path, "zombie",
                ("Comum", "Corredor", "Estrategista", "Mutante", "Tanque"),
                class_id=1)
    assert s2.total_classes == 7
    assert s2.added_classes[0] == "Comum"
    # zombie's class id 1 (Corredor) must have been remapped to merged id 3
    lbl = (tmp_path / "library" / "train" / "labels").glob("zombie_*.txt")
    line = next(iter(lbl)).read_text().splitlines()[0]
    assert line.startswith("3 ")


def test_shared_class_name_reuses_id(tmp_path):
    _merge(tmp_path, "a", ("threat", "weapon"))
    s2 = _merge(tmp_path, "b", ("weapon", "explosion"))
    assert s2.added_classes == ["explosion"]
    assert s2.total_classes == 3
    # b's class 0 ("weapon") remaps to existing merged id 1
    line = next(iter((tmp_path / "library" / "train" / "labels")
                     .glob("b_*.txt"))).read_text().splitlines()[0]
    assert line.startswith("1 ")


def test_filename_collision_is_prefixed(tmp_path):
    _merge(tmp_path, "a", ("x",))
    _merge(tmp_path, "b", ("y",))
    imgs = sorted(p.name for p in
                  (tmp_path / "library" / "train" / "images").iterdir())
    assert any(n.startswith("a_") for n in imgs)
    assert any(n.startswith("b_") for n in imgs)
    assert len(imgs) == 4  # both sources' train images present


def test_reimport_same_version_skipped_unless_forced(tmp_path):
    _merge(tmp_path, "theat", ("a", "b"))
    again = _merge(tmp_path, "theat", ("a", "b"))
    assert again.skipped
    forced = _merge(tmp_path, "theat", ("a", "b"), force=True)
    assert not forced.skipped
    manifest = dl.load_manifest(tmp_path / "library")
    assert len(manifest["imports"]) == 1  # forced re-import replaces the entry


def test_summary_reads_manifest_only(tmp_path):
    _merge(tmp_path, "theat", ("a", "b"))
    _merge(tmp_path, "zombie", ("c",))
    summary = dl.summarize(tmp_path / "library")
    assert summary["classes"] == ["a", "b", "c"]
    assert [i["name"] for i in summary["imports"]] == ["theat", "zombie"]
    assert summary["image_totals"]["train"] == 4


def test_manifest_round_trip_and_merge_again(tmp_path):
    _merge(tmp_path, "a", ("x",))
    manifest = dl.load_manifest(tmp_path / "library")
    assert manifest["schema_version"] == dl.SCHEMA_VERSION
    _merge(tmp_path, "b", ("y",))
    manifest2 = dl.load_manifest(tmp_path / "library")
    assert manifest2["classes"] == ["x", "y"]


def test_corrupt_manifest_aborts_with_rebuild_message(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / dl.MANIFEST_NAME).write_text("{not json")
    with pytest.raises(dl.DatasetLibraryError, match="rebuild"):
        dl.load_manifest(lib)


def test_newer_schema_rejected(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / dl.MANIFEST_NAME).write_text(json.dumps(
        {"schema_version": 99, "classes": [], "imports": []}))
    with pytest.raises(dl.DatasetLibraryError, match="newer"):
        dl.load_manifest(lib)


def test_failed_merge_leaves_prior_manifest_untouched(tmp_path, monkeypatch):
    _merge(tmp_path, "a", ("x",))
    before = (tmp_path / "library" / dl.MANIFEST_NAME).read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(dl.shutil, "copy2", boom)
    with pytest.raises(OSError):
        _merge(tmp_path, "b", ("y",))
    assert (tmp_path / "library" / dl.MANIFEST_NAME).read_text() == before


def test_malicious_metadata_name_sanitized_or_rejected(tmp_path):
    assert dl.sanitize_source_name("../../evil") == "evil"
    assert dl.sanitize_source_name("my project v2!") == "my_project_v2"
    with pytest.raises(dl.DatasetLibraryError):
        dl.sanitize_source_name("../..")
    with pytest.raises(dl.DatasetLibraryError):
        dl.sanitize_source_name("")


def test_library_spec_shape(tmp_path):
    _merge(tmp_path, "a", ("x", "y"))
    manifest = dl.load_manifest(tmp_path / "library")
    spec = dl.library_spec(tmp_path / "library", manifest)
    assert spec["nc"] == 2
    assert spec["names"] == ["x", "y"]
    assert spec["train"] == "train/images"
    assert spec["test"] == "test/images"  # make_dataset builds a test split


def test_classes_sidecar_round_trip(tmp_path):
    run = tmp_path / "weights"
    run.mkdir()
    dl.write_classes_sidecar(run, ["a", "b"])
    assert dl.read_classes_sidecar(run / "best.pt") == ["a", "b"]
    assert dl.read_classes_sidecar(tmp_path / "nowhere" / "best.pt") == []
