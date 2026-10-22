from concurrent.futures import wait

from imgui_lab4_interface import Settings
from imgui_lab4_interface.app import Lab4App


def settle(app: Lab4App) -> None:
    """Wait for the app's workers and land their outcomes, as frames would."""
    while app.futures:
        wait(list(app.futures.values()))
        app.poll()


def transfer_app(tmp_path, monkeypatch) -> Lab4App:
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    (tmp_path / "mount" / "data2" / "M230").mkdir(parents=True)
    (tmp_path / "mount" / "data2" / "notes.txt").write_text("x")
    (tmp_path / "rec.tif").write_bytes(b"tif")
    app = Lab4App(Settings(mount_root=str(tmp_path / "mount"), mount_base="/biohpc", destinations=[["/biohpc/data2", "data2"]]))
    app.work_offline()
    app.page = "transfer"
    app.sources = [str(tmp_path / "rec.tif")]
    app.attach = False
    settle(app)
    return app


def destination(app: Lab4App, path: str) -> list[str]:
    app.dest = path
    app.check_destination()
    settle(app)
    return app.problems()


def test_a_new_folder_is_made_on_send(tmp_path, monkeypatch):
    app = transfer_app(tmp_path, monkeypatch)
    assert destination(app, "/biohpc/data2/M231/") == []
    assert app.dest_state == "new"
    app.act()
    app.confirm["run"](*app.confirm["args"])
    settle(app)
    assert not app.failed, app.message
    assert (tmp_path / "mount" / "data2" / "M231" / "rec.tif").read_bytes() == b"tif"
    app.check_destination()
    settle(app)
    assert app.dest_state == "exists"


def test_only_one_new_folder_and_never_a_file(tmp_path, monkeypatch):
    app = transfer_app(tmp_path, monkeypatch)
    assert destination(app, "/biohpc/data2/M231/raw") == ["/biohpc/data2/M231 does not exist; a transfer makes at most one new folder"]
    assert destination(app, "/biohpc/data2/notes.txt") == ["/biohpc/data2/notes.txt is a file"]
    assert destination(app, "data2/M231") == ["the destination is a BioHPC path, e.g. /biohpc/data2/M231"]
    assert destination(app, "") == ["choose a destination folder"]
    assert not (tmp_path / "mount" / "data2" / "M231").exists()


def test_picker_lists_folders_only_and_starts_where_a_new_folder_would_go(tmp_path, monkeypatch):
    app = transfer_app(tmp_path, monkeypatch)
    destination(app, "/biohpc/data2/M231")
    app.open_destination_picker()
    settle(app)
    assert app.picker_path == "/biohpc/data2"
    assert [e.name for e in app.picker_entries] == ["M230"]
