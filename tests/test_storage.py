import json

import pytest

from imgui_lab4_interface import SIDECAR_SUFFIX, MountedStorage, Recording, Settings, Transfer, open_storage, SshStorage


def test_transfer_copies_files_and_folders_with_sidecars(tmp_path):
    local = tmp_path / "local"
    (local / "session" / "plane0").mkdir(parents=True)
    (local / "session" / "plane0" / "data.bin").write_bytes(b"x" * 3_000_000)
    (local / "movie.tif").write_bytes(b"tif")
    storage = MountedStorage(str(tmp_path / "mount"))
    metadata = Recording(subject="M231").sidecar(user="loson")

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


def test_recording_problems_and_label():
    rec = Recording(subject="", session_date="21-08-2026")
    assert len(rec.problems()) == 2
    rec = Recording(subject="M231", session_date="2026-08-21", recording_idx=3, extra=[["rig", "B"], ["", "dropped"]])
    assert rec.problems() == []
    assert rec.label() == "M231_2026-08-21_rec03"
    assert rec.sidecar(user="loson")["extra"] == {"rig": "B"}
