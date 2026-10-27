"""Every action that writes a data file, or deletes rows, only asks; nothing changes until the dialog's button runs it."""

from pathlib import Path

import pytest
from test_transfer_page import settle

from imgui_lab4_interface import Settings
from imgui_lab4_interface.app import Lab4App
from imgui_lab4_interface.slices import ROOT
from imgui_lab4_interface.storage import MountedStorage, Transfer, download


def snapshot(root: Path) -> list:
    return sorted(
        (p, p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    (tmp_path / "mount" / "archive" / "x").mkdir(parents=True)
    (tmp_path / "mount" / "archive" / "x" / "data.tif").write_bytes(b"old")
    (tmp_path / "mount" / "archive" / "x" / "data.tif.lab4.json").write_text("{}")
    (tmp_path / "data.tif").write_bytes(b"new")
    made = Lab4App(
        Settings(
            mount_root=str(tmp_path / "mount"),
            mount_base="/",
            destinations=[["/archive/x", "test"]],
        )
    )
    made.user = "loson"
    made.recording.subject_id = "M231"
    yield made
    made.executor.shutdown(wait=True)
    made.executor_long.shutdown(wait=True)


def test_transfer_asks_first(app, tmp_path):
    before = snapshot(tmp_path / "mount")
    app.page, app.sources, app.overwrite = (
        "transfer",
        [str(tmp_path / "data.tif")],
        True,
    )
    app.act()
    assert app.confirm is None and "sizes" in app.futures
    settle(app)
    assert app.confirm["button"] == "Send" and "replaced" in " ".join(
        app.confirm["lines"]
    )
    assert app.transfer is None and "transfer" not in app.futures
    assert snapshot(tmp_path / "mount") == before


def test_attach_asks_first_and_says_it_replaces(app, tmp_path):
    before = snapshot(tmp_path / "mount")
    app.page, app.path_browse, app.selected = "browse", "/archive/x", "data.tif"
    app.entries = MountedStorage(str(tmp_path / "mount")).listdir("/archive/x")
    app.act()
    assert app.confirm["button"] == "Write" and "replaced" in " ".join(
        app.confirm["lines"]
    )
    assert "attach" not in app.futures
    assert snapshot(tmp_path / "mount") == before


def test_deleting_and_resetting_rows_ask_first(app):
    rows = app.slices.rows("slices")
    app.page = "slices"
    app.confirm = {
        "title": "x",
        "lines": [],
        "button": "Delete",
        "run": app.delete_rows,
        "args": ("slices", [rows[0]["id"]]),
    }
    assert app.slices.rows("slices") == rows
    app.confirm["run"](*app.confirm["args"])
    assert len(app.slices.rows("slices")) == len(rows) - 1


def test_opening_a_remote_file_asks_before_copying(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app.storage, "local_path", lambda path: None)
    app.open_file(path="Images/H_slice/HSLice_B_7-27-2026.mesc", size=10)
    assert app.confirm[
        "button"
    ] == "Copy and open" and "Nothing on BioHPC changes." in " ".join(
        app.confirm["lines"]
    )
    assert "download" not in app.futures and not (tmp_path / "home" / "cache").exists()


def test_download_only_reads_storage(tmp_path):
    source = tmp_path / "mount" / ROOT.lstrip("/") / "a.mesc"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 3000)
    before = snapshot(tmp_path / "mount")
    progress = {"done": 0}
    local = download(
        MountedStorage(str(tmp_path / "mount")),
        f"{ROOT}/a.mesc",
        tmp_path / "cache" / "a.mesc",
        progress,
    )
    assert local.read_bytes() == b"x" * 3000 and progress["done"] == 3000
    assert not (tmp_path / "cache" / "a.mesc.part").exists()
    assert snapshot(tmp_path / "mount") == before


def test_transfer_without_overwrite_leaves_an_existing_sidecar(tmp_path):
    root = tmp_path / "mount"
    (root / "archive" / "x").mkdir(parents=True)
    (root / "archive" / "x" / "data.tif.lab4.json").write_text("{}")
    (tmp_path / "data.tif").write_bytes(b"new")
    transfer = Transfer(
        MountedStorage(str(root)),
        [tmp_path / "data.tif"],
        "/archive/x",
        {"recording": "r"},
        overwrite=False,
    )
    with pytest.raises(FileExistsError, match="lab4.json"):
        transfer.run()
    assert not (root / "archive" / "x" / "data.tif").exists()
    assert (root / "archive" / "x" / "data.tif.lab4.json").read_text() == "{}"
