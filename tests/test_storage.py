import json
import shutil
from datetime import datetime

import pytest

from imgui_lab4_interface import SIDECAR_SUFFIX, MountedStorage, Recording, Settings, Transfer, open_storage, SshStorage
from imgui_lab4_interface.storage import SshReader, destination_state, download, shows


def test_transfer_copies_files_and_folders_with_sidecars(tmp_path):
    local = tmp_path / "local"
    (local / "session" / "plane0").mkdir(parents=True)
    (local / "session" / "plane0" / "data.bin").write_bytes(b"x" * 3_000_000)
    (local / "movie.tif").write_bytes(b"tif")
    (tmp_path / "mount" / "archive" / "pml").mkdir(parents=True)
    storage = MountedStorage(str(tmp_path / "mount"))
    metadata = Recording(subject_id="M231").sidecar(user="loson")

    transfer = Transfer(storage, [local / "session", local / "movie.tif"], "/archive/pml/raw/", metadata, overwrite=False)
    transfer.run()

    dest = tmp_path / "mount" / "archive" / "pml" / "raw"
    assert (dest / "session" / "plane0" / "data.bin").stat().st_size == 3_000_000
    assert (dest / "movie.tif").read_bytes() == b"tif"
    assert transfer.done == transfer.total == 3_000_003
    sidecar = json.loads((dest / f"movie.tif{SIDECAR_SUFFIX}").read_text())
    assert sidecar["key"]["subject_id"] == "M231"
    assert sidecar["source"] == str(local / "movie.tif")
    assert (dest / f"session{SIDECAR_SUFFIX}").is_file()


def test_transfer_refuses_existing_unless_overwrite(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("new")
    storage = MountedStorage(str(tmp_path / "mount"))
    (tmp_path / "mount" / "dest").mkdir(parents=True)
    (tmp_path / "mount" / "dest" / "a.txt").write_text("old")

    with pytest.raises(FileExistsError):
        Transfer(storage, [source], "/dest", None, overwrite=False).run()
    Transfer(storage, [source], "/dest", None, overwrite=True).run()
    assert (tmp_path / "mount" / "dest" / "a.txt").read_text() == "new"
    assert not (tmp_path / "mount" / "dest" / f"a.txt{SIDECAR_SUFFIX}").exists()


def test_cancel_stops_mid_transfer(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 10)
    (tmp_path / "mount").mkdir()
    transfer = Transfer(MountedStorage(str(tmp_path / "mount")), [source], "/dest", None, overwrite=False)
    transfer.cancelled = True
    with pytest.raises(InterruptedError):
        transfer.run()


def test_mounted_listdir_and_check(tmp_path):
    root = tmp_path / "mount"
    (root / "work" / "sub").mkdir(parents=True)
    (root / "work" / "b.txt").write_text("hi")
    storage = MountedStorage(str(root))
    storage.check()
    assert [(e.name, e.is_dir, e.size) for e in storage.listdir("/work")] == [("sub", True, 0), ("b.txt", False, 2)]
    with pytest.raises(OSError):
        MountedStorage("").check()
    with pytest.raises(OSError):
        MountedStorage(str(tmp_path / "missing")).check()


def test_open_storage_follows_settings(tmp_path):
    assert isinstance(open_storage(Settings(backend="mounted", mount_root=str(tmp_path))), MountedStorage)
    assert open_storage(Settings(backend="ssh", ssh_host="biohpc")).label == SshStorage("biohpc").label


def test_settings_round_trip_ignores_unknown(tmp_path):
    path = tmp_path / "settings.json"
    Settings(backend="ssh", ssh_host="me@host").save(path)
    loaded = json.loads(path.read_text())
    path.write_text(json.dumps({**loaded, "stale": 1}))
    settings = Settings.load(path)
    assert (settings.backend, settings.ssh_host) == ("ssh", "me@host")


def test_mounted_base_maps_biohpc_paths_under_the_root(tmp_path):
    (tmp_path / "data2" / "M231").mkdir(parents=True)
    storage = MountedStorage(str(tmp_path), "/biohpc")
    assert storage.local_path("/biohpc/data2/M231") == tmp_path / "data2" / "M231"
    assert storage.local_path("/biohpc") == tmp_path
    assert [e.name for e in storage.listdir("/biohpc/data2")] == ["M231"]
    with pytest.raises(OSError):
        storage.local_path("/archive/x")
    with pytest.raises(OSError):
        storage.local_path("/biohpc/../etc")


def test_recording_problems_label_and_key():
    rec = Recording(session_datetime="21-08-2026", weight_g="heavy")
    assert len(rec.problems()) == 6
    rec = Recording(
        experimenter="Eunji Kong",
        collection_name="place cells",
        subject_id=" M231 ",
        session_datetime="2026-08-21 14:30",
        device_name="Bruker 1 Imaging",
        weight_g="24.5",
        extra=[["rig", "B"], ["", "dropped"]],
    )
    assert rec.problems() == []
    assert rec.label() == "M231_2026-08-21_1430_Bruker 1 Imaging"
    assert rec.key()["subject_id"] == "M231"
    assert rec.key()["session_datetime"] == datetime(2026, 8, 21, 14, 30)
    sidecar = rec.sidecar(user="loson")
    assert sidecar["extra"] == {"rig": "B"}
    assert sidecar["key"]["session_datetime"] == "2026-08-21 14:30:00"
    json.dumps(sidecar)


def test_shows_matches_the_share_not_the_letter():
    assert shows(r"\\sshfs.kr\flynn@server.memorylongevity.org!2216\biohpc", "/biohpc")
    assert shows("flynn@server:/biohpc/", "/biohpc")
    assert shows(r"\\nas\lab\BioHPC", "/biohpc")
    assert not shows(r"\\nas\biohpc_old", "/biohpc")
    assert not shows("flynn@server:/home2", "/biohpc")


def test_transfer_makes_one_new_folder_but_not_its_parents(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("a")
    (tmp_path / "mount" / "data2").mkdir(parents=True)
    storage = MountedStorage(str(tmp_path / "mount"), "/biohpc")

    Transfer(storage, [source], "/biohpc/data2/M231", None, overwrite=False).run()
    assert (tmp_path / "mount" / "data2" / "M231" / "a.txt").read_text() == "a"
    Transfer(storage, [source], "/biohpc/data2/M231", None, overwrite=True).run()

    with pytest.raises(FileNotFoundError):
        Transfer(storage, [source], "/biohpc/data2/M232/raw", None, overwrite=False).run()
    assert not (tmp_path / "mount" / "data2" / "M232").exists()


def test_destination_state(tmp_path):
    (tmp_path / "data2" / "M231").mkdir(parents=True)
    (tmp_path / "data2" / "notes.txt").write_text("x")
    storage = MountedStorage(str(tmp_path), "/biohpc")
    assert destination_state(storage, "/biohpc/data2/M231/") == ("/biohpc/data2/M231/", "exists")
    assert destination_state(storage, "/biohpc/data2/M232") == ("/biohpc/data2/M232", "new")
    with pytest.raises(OSError, match="at most one new folder"):
        destination_state(storage, "/biohpc/data2/M232/raw")
    with pytest.raises(OSError, match="is a file"):
        destination_state(storage, "/biohpc/data2/notes.txt")


def test_walk_raises_when_the_root_is_unreachable(tmp_path):
    with pytest.raises(OSError, match="no mount root"):
        list(MountedStorage("").walk("/x"))
    with pytest.raises(OSError, match="not found"):
        list(MountedStorage(str(tmp_path)).walk("/missing"))


def test_a_cancelled_download_leaves_nothing(tmp_path):
    (tmp_path / "mount").mkdir()
    (tmp_path / "mount" / "big.mesc").write_bytes(b"x" * 10)
    local = tmp_path / "cache" / "big.mesc"
    with pytest.raises(InterruptedError, match="cancelled"):
        download(MountedStorage(str(tmp_path / "mount")), "/big.mesc", local, {"done": 0, "cancelled": True})
    assert not local.exists() and not local.with_name("big.mesc.part").exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh to stand in for ssh")
def test_an_error_while_reading_over_ssh_is_not_replaced(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * (4 << 20))
    with pytest.raises(InterruptedError, match="stop"):
        with SshReader(["sh", "-c"], str(tmp_path / "big")) as src:
            src.read(10)
            raise InterruptedError("stop")


def test_an_unset_mount_root_reaches_nothing():
    storage = MountedStorage("", "/biohpc")
    for call in (storage.check, lambda: storage.listdir("/biohpc"), lambda: destination_state(storage, "/biohpc/data2")):
        with pytest.raises(OSError, match="no mount root set"):
            call()
