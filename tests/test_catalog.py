import json
import sqlite3

import pytest

from imgui_lab4_interface import SIDECAR_SUFFIX, Catalog, MountedStorage, Settings
from imgui_lab4_interface.viewers import command

LAYOUT = {
    "lab/M231/2026-08-21/movie.tif": 100,
    "lab/M231/2026-08-21/movie.tif" + SIDECAR_SUFFIX: json.dumps({"recording": "M231_2026-08-21_rec01", "key": {"subject_id": "M231", "session_date": "2026-08-21", "experimenter": "loson"}}),
    "lab/M231/2026-08-21/run_20260822/config.json": "{}",
    "lab/M231/2026-08-21/run_20260822/results.hdf5": 400,
    "lab/M231/2026-08-21/suite2p/plane0/ops.npy": 10,
    "lab/M231/2026-08-21/suite2p/plane0/stat.npy": 10,
    "lab/M231/2026-08-21/suite2p/plane0/data.bin": 1000,
    "lab/M231/2026-08-21/suite2p/plane1/ops.npy": 10,
    "lab/M231/tiffs/a.tif": 5,
    "lab/M231/tiffs/b.tif": 5,
    "lab/M231/stack.zarr/zarr.json": 3,
    "lab/M231/stack.zarr/c/0/0": 50,
    "lab/asako/expt1/expt1.mesc": 70,
    "lab/asako/expt1/PF/denoised_trace_scans.pkl": 20,
    "lab/tracking/20261001T180415_roicat-tracking-manifest.json": "{}",
    "lab/.cache/ignored.tif": 1,
    "lab/notes.txt": 1,
}


@pytest.fixture
def scanned(tmp_path):
    root = tmp_path / "mount"
    for relative, content in LAYOUT.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(b"x" * content)
    storage = MountedStorage(str(root))
    catalog = Catalog(tmp_path / "catalog.sqlite")
    found = catalog.scan(storage, "/lab")
    return catalog, storage, found


def test_scan_finds_every_kind(scanned):
    catalog, storage, found = scanned
    columns, rows = catalog.sql("SELECT path, kind, detail FROM datasets ORDER BY path")
    kinds = {path: (kind, detail) for path, kind, detail in rows}
    assert kinds == {
        "/lab/M231/2026-08-21/movie.tif": ("tiff", ""),
        "/lab/M231/2026-08-21/run_20260822": ("masknmf-run", "results.hdf5"),
        "/lab/M231/2026-08-21/run_20260822/results.hdf5": ("masknmf-results", ""),
        "/lab/M231/2026-08-21/suite2p": ("suite2p-run", ""),
        "/lab/M231/2026-08-21/suite2p/plane0": ("suite2p-plane", "segmented"),
        "/lab/M231/2026-08-21/suite2p/plane1": ("suite2p-plane", "registered"),
        "/lab/M231/tiffs": ("tiff-series", "2 tiffs"),
        "/lab/M231/tiffs/a.tif": ("tiff", ""),
        "/lab/M231/tiffs/b.tif": ("tiff", ""),
        "/lab/M231/stack.zarr": ("zarr", ""),
        "/lab/asako/expt1/expt1.mesc": ("mesc", ""),
        "/lab/asako/expt1/PF": ("voltage-pf", ""),
        "/lab/tracking": ("roicat-tracking", ""),
    }
    assert found["datasets"] == len(kinds)


def test_describe_summarizes_folders_and_items(scanned):
    catalog, storage, _ = scanned
    described = catalog.describe(storage.label, "/lab/M231")
    assert described["scan"]["root"] == "/lab"
    assert described["folder"]["kinds"]["tiff"] == 3
    assert described["folder"]["n_files"] == 10
    items = described["items"]
    assert items["tiffs"]["kind"] == "tiff-series"
    assert items["2026-08-21"]["kinds"]["suite2p-plane"] == 2
    assert "kind" not in items["2026-08-21"]
    session = catalog.describe(storage.label, "/lab/M231/2026-08-21")["items"]
    assert session["movie.tif"]["has_metadata"] == 1
    assert session["suite2p"]["kinds"] == {"suite2p-plane": 2}


def test_search_by_words_kind_and_sidecar(scanned):
    catalog, storage, _ = scanned
    assert {r["path"] for r in catalog.search(storage.label, "kind:suite2p-plane")} == {
        "/lab/M231/2026-08-21/suite2p/plane0",
        "/lab/M231/2026-08-21/suite2p/plane1",
    }
    by_metadata = catalog.search(storage.label, "loson")
    assert [(r["path"], r["subject"]) for r in by_metadata] == [("/lab/M231/2026-08-21/movie.tif", "M231")]
    assert catalog.search(storage.label, "M231 plane1")[0]["kind"] == "suite2p-plane"
    assert catalog.search(storage.label, "100%_") == []
    assert catalog.search("another storage") == []


def test_rescan_replaces_only_its_subtree(scanned, tmp_path):
    catalog, storage, _ = scanned
    (tmp_path / "mount" / "lab" / "M231" / "tiffs" / "b.tif").unlink()
    catalog.scan(storage, "/lab/M231/tiffs")
    paths = {r["path"] for r in catalog.search(storage.label, "kind:tiff")}
    assert "/lab/M231/tiffs/b.tif" not in paths
    assert "/lab/M231/2026-08-21/movie.tif" in paths
    assert catalog.search(storage.label, "kind:tiff-series") == []


def test_sql_is_read_only(scanned):
    catalog, _, _ = scanned
    with pytest.raises(sqlite3.OperationalError):
        catalog.sql("DELETE FROM datasets")


def test_command_per_viewer(tmp_path):
    settings = Settings(command_mbo="mbo", command_masknmf="C:/envs/masknmf.exe")
    assert command(settings, "masknmf-run", tmp_path) == ["C:/envs/masknmf.exe", "view", str(tmp_path)]
    assert command(settings, "voltage-pf", tmp_path) == ["mbo", "curate", str(tmp_path)]
    assert command(settings, "mesc", tmp_path) == ["mbo", str(tmp_path)]
