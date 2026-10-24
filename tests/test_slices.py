import csv
import sqlite3
from contextlib import closing

import pytest

from imgui_lab4_interface.slices import ADMIN, COLUMNS, ROOT, SEED_SLICES, SliceDatabase, file_dates, match_files
from imgui_lab4_interface.storage import MountedStorage

# a sample of the names under ROOT on BioHPC
NAMES = [
    "Ephys/Shobhana_Data/SS_Ephys_HSlice_8-31-2026.pxp",
    "Ephys/mRNA_9-18-2026_Dev1-compressed.nwb",
    "Images/H_slice/HSLice_7-27-2026.mesc",
    "Images/H_slice/HSLice_7-27-2026_24hrs_chamber bubbling.mesc",
    "Images/H_slice/HSLice_7-29-2026_48hrs_chamber bubbling.mesc",
    "Images/H_slice/HSLice_B_7-27-2026.mesc",
    "Images/H_slice/HSLice_D_7-27-2026.mesc",
    "Images/H_slice/HSlice_260727_Alexa488.png",
    "Images/H_slice/HSlice_260727_Cell2A_Alexa488.png",
    "Images/H_slice/HSlice_260727_D_Mean_Alexa488.png",
    "Images/H_slice/HSlice_8-31-2026.mesc",
    "Images/H_slice/HSlice_8-31-2026_Day9_BPMedium.mesc",
    "Images/H_slice/Hslice mrna egfp8-31-26.png",
    "Images/H_slice/HSlice_9-14-2026-Day2_Storage_mrnaEGFP.mesc",
    "Images/Shobhana_Camera/H_slice/HSLice_B_7-27-2026.mesc",
    "Images/Shobhana_Camera/batch/11_May21st_2026/0_0.ome.tiff",
    "Images/mRNA_mouse_260811/Slice1_A.ome.tiff",
]
FILES = [{"path": name, "size": 1, "mtime": 0.0} for name in NAMES]


def names(pattern: str) -> list[str]:
    return [f["path"] for f in match_files(FILES, pattern)]


def test_dates_in_every_spelling():
    assert file_dates("HSLice_B_7-27-2026.mesc") == {"2026-07-27"}
    assert file_dates("HSlice_260727_Alexa488.png") == {"2026-07-27"}
    assert file_dates("Hslice mrna egfp8-31-26.png") == {"2026-08-31"}
    assert file_dates("batch/11_May21st_2026/0_0.ome.tiff") == {"2026-05-21"}
    assert file_dates("24hg20260812_132230.mp4") == {"2026-08-12"}
    assert file_dates("Screenshot 2026-05-22 102201.png") == {"2026-05-22"}
    assert file_dates("Ogbfill_7-14.mesc") == set()


def test_match_by_date_words_and_exclusions():
    assert names("2026-07-27 _b_") == ["Images/H_slice/HSLice_B_7-27-2026.mesc", "Images/Shobhana_Camera/H_slice/HSLice_B_7-27-2026.mesc"]
    assert names("2026-07-27 -_b_ -_d_ -24hrs -cell") == ["Images/H_slice/HSLice_7-27-2026.mesc", "Images/H_slice/HSlice_260727_Alexa488.png"]
    assert names("2026-08-31 -day9 -egfp") == ["Ephys/Shobhana_Data/SS_Ephys_HSlice_8-31-2026.pxp", "Images/H_slice/HSlice_8-31-2026.mesc"]
    assert names("") == []


def test_every_seed_slice_finds_its_files():
    for slice_id, *_, pattern, _ in SEED_SLICES:
        found = match_files(FILES, pattern)
        assert all("hslice" in f["path"].lower() or "ephys" in f["path"].lower() for f in found), slice_id


def test_seeds_once_and_the_same(tmp_path):
    db = SliceDatabase(tmp_path / "a.sqlite")
    patients, slices = db.rows("patients"), db.rows("slices")
    assert [p["patient_id"] for p in patients] == ["HS260727", "HS260831", "HS260914", "HS260918"]
    assert len(slices) == len(SEED_SLICES) and all(s["collection_date"] and s["file_match"] for s in slices)
    assert db.rows("assays") == []
    assert SliceDatabase(tmp_path / "a.sqlite").rows("slices") == slices


def test_an_older_file_is_kept_aside_not_dropped(tmp_path):
    with closing(sqlite3.connect(tmp_path / "s.sqlite")) as db, db:
        db.execute("CREATE TABLE donors (id INTEGER PRIMARY KEY, case_id TEXT)")
        db.execute("INSERT INTO donors VALUES (1, 'H26.001')")
        db.execute("PRAGMA user_version = 2")
    db = SliceDatabase(tmp_path / "s.sqlite")
    assert db.rows("patients") and "file_match" in db.rows("slices")[0]
    assert db.moved_aside == tmp_path / "s.v2.sqlite"
    with closing(sqlite3.connect(db.moved_aside)) as old:
        assert old.execute("SELECT case_id FROM donors").fetchall() == [("H26.001",)]
    assert SliceDatabase(tmp_path / "s.sqlite").moved_aside is None


def test_index_lists_without_changing_anything(tmp_path):
    folder = tmp_path / ROOT.lstrip("/") / "Images" / "H_slice"
    folder.mkdir(parents=True)
    (folder / "HSLice_B_7-27-2026.mesc").write_bytes(b"mesc")
    before = sorted((p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    db = SliceDatabase(tmp_path / "s.sqlite")
    storage = MountedStorage(str(tmp_path))
    files, indexed = db.index_files(storage)
    assert files == [{"path": "Images/H_slice/HSLice_B_7-27-2026.mesc", "size": 4, "mtime": files[0]["mtime"]}]
    assert indexed["storage"] == storage.label
    after = sorted((p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.name != "s.sqlite")
    assert after == [entry for entry in before if entry[0].name != "s.sqlite"]
    assert SliceDatabase(tmp_path / "s.sqlite").files()[0] == files


def test_update_parses_by_column(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    first = db.rows("slices")[0]
    db.update("slices", first["id"], "day", " Day 3 ", ADMIN)
    db.update("slices", first["id"], "ephys_notes", "", ADMIN)
    row = db.rows("slices")[0]
    assert row["day"] == 3 and row["ephys_notes"] == ""
    with pytest.raises(ValueError, match="whole number or Day N"):
        db.update("slices", first["id"], "day", "tomorrow", ADMIN)
    with pytest.raises(ValueError, match="one of"):
        db.update("slices", first["id"], "outcome", "lost", ADMIN)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        db.update("patients", 1, "collection_date", "May 3", ADMIN)
    with pytest.raises(ValueError, match="no patient"):
        db.update("slices", first["id"], "patient_id", "H99.999", ADMIN)
    with pytest.raises(ValueError, match="already taken"):
        db.update("slices", first["id"], "slice_id", db.rows("slices")[1]["slice_id"], ADMIN)
    with pytest.raises(ValueError, match="edit it on Patients"):
        db.update("slices", first["id"], "collection_date", "2026-01-01", ADMIN)


def test_only_the_owner_or_admin_changes_a_row(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    mine = db.rows("slices")[0]
    with pytest.raises(ValueError, match=f"{ADMIN}'s"):
        db.update("slices", mine["id"], "notes", "mine now", "dana")
    with pytest.raises(ValueError, match=f"{ADMIN}'s"):
        db.delete("slices", [mine["id"]], "adrian")
    id_ = db.add("assays", "dana", mine["slice_id"])
    db.update("assays", id_, "percent_live", "71", "dana")
    db.update("assays", id_, "notes", "checked", ADMIN)
    with pytest.raises(ValueError, match="dana's"):
        db.update("assays", id_, "notes", "not mine", "adrian")
    db.delete("assays", [id_], "dana")
    copies = db.duplicate("slices", [mine["id"]], "dana")
    db.update("slices", copies[0], "notes", "dana's copy", "dana")


def test_renaming_carries_down_and_parents_stay(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    slice_ = db.rows("slices")[0]
    db.add("assays", "dana", slice_["slice_id"])
    db.update("slices", slice_["id"], "slice_id", "HS260727.renamed", ADMIN)
    assert db.rows("assays")[0]["slice_id"] == "HS260727.renamed"
    patient = db.rows("patients")[0]
    with pytest.raises(ValueError, match="still have slices"):
        db.delete("patients", [patient["id"]], ADMIN)
    with pytest.raises(ValueError, match="still have assays"):
        db.delete("slices", [slice_["id"]], ADMIN)


def test_add_and_duplicate_take_fresh_ids(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    patient = db.rows("patients")[2]
    id_ = db.add("slices", "adrian", patient["patient_id"])
    added = next(s for s in db.rows("slices") if s["id"] == id_)
    assert added["patient_id"] == patient["patient_id"] and added["owner"] == "adrian"
    assert added["slice_id"] == f"{patient['patient_id']}.S04" and added["file_match"] == patient["collection_date"]
    copies = db.duplicate("slices", [id_, id_], "adrian")
    ids = [s["slice_id"] for s in db.rows("slices")]
    assert len(copies) == 2 and len(ids) == len(set(ids))
    db.add("patients", ADMIN)
    assert len({p["patient_id"] for p in db.rows("patients")}) == 5


def test_reset_and_export(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    seeded = db.rows("slices")
    db.delete("slices", [s["id"] for s in seeded[:5]], ADMIN)
    db.reset()
    assert [s["slice_id"] for s in db.rows("slices")] == [s["slice_id"] for s in seeded]
    db.update("slices", seeded[0]["id"], "notes", "ΔF/F in μm", ADMIN)
    db.export("slices", tmp_path / "slices.csv")
    with open(tmp_path / "slices.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(seeded) and list(rows[0]) == [name for name, _, _ in COLUMNS["slices"]]
    assert rows[0]["notes"] == "ΔF/F in μm"


def test_add_needs_a_parent(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    db.delete("slices", [s["id"] for s in db.rows("slices")], ADMIN)
    with pytest.raises(ValueError, match="add a slice first"):
        db.add("assays", ADMIN)
    db.delete("patients", [p["id"] for p in db.rows("patients")], ADMIN)
    with pytest.raises(ValueError, match="add a patient first"):
        db.add("slices", ADMIN)


def test_a_new_slice_id_skips_ids_moved_to_other_patients(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    moved = db.add("slices", ADMIN, "HS260914")
    db.update("slices", moved, "patient_id", "HS260918", ADMIN)
    added = db.add("slices", ADMIN, "HS260914")
    ids = {s["id"]: s["slice_id"] for s in db.rows("slices")}
    assert (ids[moved], ids[added]) == ("HS260914.S04", "HS260914.S05")


def test_a_choice_can_be_cleared(tmp_path):
    db = SliceDatabase(tmp_path / "s.sqlite")
    first = db.rows("slices")[0]
    db.update("slices", first["id"], "region", "hippocampus", ADMIN)
    db.update("slices", first["id"], "region", "", ADMIN)
    assert db.rows("slices")[0]["region"] is None


def test_an_unreachable_index_keeps_the_last_one(tmp_path):
    (tmp_path / ROOT.lstrip("/")).mkdir(parents=True)
    (tmp_path / ROOT.lstrip("/") / "a.mesc").write_bytes(b"mesc")
    db = SliceDatabase(tmp_path / "s.sqlite")
    kept = db.index_files(MountedStorage(str(tmp_path)))
    for storage in (MountedStorage(""), MountedStorage(str(tmp_path / "gone"))):
        with pytest.raises(OSError):
            db.index_files(storage)
    assert db.files() == kept
