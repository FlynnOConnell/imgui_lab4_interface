import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from imgui_lab4_interface import SIDECAR_SUFFIX, MountedStorage, Recording, Settings, Transfer, open_storage, SshStorage
from imgui_lab4_interface.storage import SshReader, destination_state, download, options_shared, shows


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


def ssh_stand_in(tmp_path) -> tuple[SshStorage, Path]:
    """An SshStorage whose commands run here through sh, each logged to the returned file."""
    calls = tmp_path / "calls.log"
    storage = SshStorage("unused")
    storage.base = ["sh", "-c", f'printf "%s\\n" "$0" >> {calls}; eval "$0"']
    return storage, calls


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("tar") is None, reason="needs sh and tar to stand in for ssh")
def test_transfer_over_ssh_is_one_tar_and_keeps_folder_permissions(tmp_path):
    local = tmp_path / "local"
    (local / "session" / "plane0").mkdir(parents=True)
    (local / "session" / "plane0" / "data.bin").write_bytes(b"x" * 3_000_000)
    (local / "session" / "notes.txt").write_text("n")
    (local / "movie 5µm.tif").write_bytes(b"tif")
    dest = tmp_path / "remote" / "raw"
    (dest / "session").mkdir(parents=True)
    # a shared lab folder: group-writable, and new files take its group
    for folder in (dest, dest / "session"):
        folder.chmod(0o2775)
    storage, calls = ssh_stand_in(tmp_path)
    started = time.time()

    transfer = Transfer(storage, [local / "session", local / "movie 5µm.tif"], str(dest), {"recording": "r"}, overwrite=True)
    transfer.run()

    assert (dest / "session" / "plane0" / "data.bin").read_bytes() == b"x" * 3_000_000
    assert (dest / "session" / "notes.txt").read_text() == "n" and (dest / "movie 5µm.tif").read_bytes() == b"tif"
    assert json.loads((dest / f"session{SIDECAR_SUFFIX}").read_text())["source"] == str(local / "session")
    assert transfer.done == transfer.total == 3_000_004
    assert [oct(folder.stat().st_mode & 0o7777) for folder in (dest, dest / "session")] == [oct(0o2775)] * 2
    assert (dest / "movie 5µm.tif").stat().st_mtime >= started - 1
    commands = calls.read_text().splitlines()
    assert len(commands) == 2 and sum("tar -xmf" in c for c in commands) == 1


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("tar") is None, reason="needs sh and tar to stand in for ssh")
def test_transfer_over_ssh_checks_names_in_one_listing(tmp_path):
    sources = [tmp_path / f"{name}.tif" for name in "abc"]
    for source in sources:
        source.write_text("x")
    (tmp_path / "remote").mkdir()
    storage, calls = ssh_stand_in(tmp_path)
    Transfer(storage, sources, str(tmp_path / "remote"), {"recording": "r"}, overwrite=False).run()
    assert len(calls.read_text().splitlines()) == 4
    with pytest.raises(FileExistsError, match="b.tif already exists"):
        Transfer(storage, [sources[1]], str(tmp_path / "remote"), None, overwrite=False).run()


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("tar") is None, reason="needs sh and tar to stand in for ssh")
def test_transfer_over_ssh_reports_cancel_and_remote_errors(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (3 << 20))
    (tmp_path / "remote").mkdir()
    storage, _ = ssh_stand_in(tmp_path)
    transfer = Transfer(storage, [source], str(tmp_path / "remote"), None, overwrite=True)
    transfer.cancelled = True
    with pytest.raises(InterruptedError, match="cancelled"):
        transfer.run()
    # the remote side quits without reading, as tar does when it cannot write; its words beat the broken pipe
    failing = 'echo "** WARNING: connection is not using a post-quantum key exchange algorithm." >&2; echo "raw: Permission denied" >&2; exit 2'
    storage.base = ["sh", "-c", f'case "$0" in *tar*) {failing};; *) eval "$0";; esac']
    with pytest.raises(OSError, match="^raw: Permission denied$"):
        Transfer(storage, [source], str(tmp_path / "remote"), None, overwrite=True).run()


def test_ssh_shares_a_connection_only_where_the_socket_fits(monkeypatch):
    monkeypatch.setattr("imgui_lab4_interface.storage.os.path.isdir", lambda path: True)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/fo"))
    options = options_shared()
    assert "ControlPath=/home/fo/.ssh/lab4-%C" in options and options == SshStorage("biohpc").base[-1 - len(options) : -1]
    monkeypatch.setattr(Path, "home", lambda: Path("/home/" + "x" * 40))
    assert options_shared() == []
    monkeypatch.setattr(Path, "home", lambda: Path("/home/fo"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert options_shared() == []
