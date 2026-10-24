"""The Human slices page's filters, picks, export and file opening, and the file index it reads."""

import csv
import threading

import pytest
from test_transfer_page import settle

import imgui_lab4_interface.app as app_module
from imgui_lab4_interface import Settings
from imgui_lab4_interface.app import Lab4App
from imgui_lab4_interface.slices import ROOT


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    (tmp_path / "mount" / ROOT.removeprefix("/biohpc/")).mkdir(parents=True)
    made = Lab4App(Settings(mount_root=str(tmp_path / "mount"), mount_base="/biohpc"))
    made.work_offline()
    made.page = "slices"
    settle(made)
    yield made
    made.executor.shutdown(wait=True)


class SavedTo:
    """A save dialog that has closed on path."""

    def __init__(self, path: str):
        self.path = path

    def ready(self) -> bool:
        return True

    def result(self) -> str:
        return self.path


class HeldIndex:
    """Stands in for index_files: notes each storage it was started for and waits for release."""

    def __init__(self):
        self.started = []
        self.release = threading.Event()

    def __call__(self, storage) -> tuple[list, None]:
        self.started.append(storage.label)
        self.release.wait(10)
        return [], None


def shown(app: Lab4App, table: str, text: str) -> list[str]:
    app.filter_slices[table] = text
    app.order_rows(table=table)
    return [r["slice_id"] for r in app.order_slices[table]]


def test_column_equals_matches_the_whole_value(app):
    assert shown(app, "slices", "slice_id:hs260727.c") == ["HS260727.C", "HS260727.cells"]
    assert shown(app, "slices", "slice_id=hs260727.c") == ["HS260727.C"]


def test_rows_the_filter_hides_are_unpicked(app):
    ids = {r["slice_id"]: r["id"] for r in app.rows_slices["slices"]}
    app.picked["slices"] |= {ids["HS260727.C"], ids["HS260727.cells"]}
    shown(app, "slices", "slice_id=hs260727.c")
    assert app.picked["slices"] == {ids["HS260727.C"]}


def test_export_writes_the_table_shown_when_it_was_asked_for(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.pfd, "save_file", lambda *args: SavedTo(str(tmp_path / "out.csv")))
    app.tab_slices = "slices"
    app.act()
    app.tab_slices = "patients"
    app.poll()
    with open(tmp_path / "out.csv", newline="", encoding="utf-8-sig") as f:
        assert "slice_id" in next(csv.reader(f))
    assert app.message.startswith(f"wrote {len(app.rows_slices['slices'])} slices")


def test_opening_a_file_off_the_mount_says_why(app, tmp_path):
    app.open_file(path="Images/missing.mesc", size=4)
    assert app.failed and "not found" in app.message and app.confirm is None
    app.storage.base = "/biohpc/data2"
    app.open_file(path="Images/a.mesc", size=4)
    assert app.failed and "outside /biohpc/data2" in app.message and app.confirm is None


def test_files_of_a_row_are_matched_once_per_change(app, monkeypatch):
    calls = []
    match = app_module.match_files
    monkeypatch.setattr(app_module, "match_files", lambda files, pattern: calls.append(pattern) or match(files, pattern))
    row = app.rows_slices["slices"][0]
    for _ in range(3):
        app.files_of("slices", row, row)
    assert len(calls) == 1
    app.reload_slices()
    app.files_of("slices", row, row)
    assert len(calls) == 2


def test_one_index_at_a_time_and_again_for_new_storage(app, tmp_path, monkeypatch):
    index = HeldIndex()
    monkeypatch.setattr(app.slices, "index_files", index)
    first = app.storage.label
    app.connect()
    app.connect()
    (tmp_path / "other").mkdir()
    app.settings.mount_root = str(tmp_path / "other")
    app.connect()
    index.release.set()
    settle(app)
    assert index.started == [first, app.storage.label]


def test_double_clicking_a_cell_edits_it_or_says_why_not(app):
    names = [name for name, _, _ in app_module.COLUMNS["slices"]]
    row = app.rows_slices["slices"][0]
    app.user_slices = "dana"
    app.start_edit("slices", row, names.index("region"))
    assert app.failed and app.message == "this row is shobhana's; only shobhana can edit it" and app.editing is None
    app.start_edit("slices", row, names.index("owner"))
    assert app.message == "owner cannot be changed"
    app.user_slices = "shobhana"
    app.start_edit("slices", row, names.index("region"))
    assert app.editing == ("slices", row["id"], "region") and app.open_choice
