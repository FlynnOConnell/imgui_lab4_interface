import json
import shlex
from datetime import datetime

import h5py
import numpy as np
import pytest
from imgui_bundle import hello_imgui, imgui, immapp

from imgui_lab4_interface import MountedStorage, Settings
from imgui_lab4_interface.processing import (
    Asap7Params,
    Job,
    dir_save,
    find_mesc,
    is_runnable,
    job_states,
    list_units,
    load_jobs,
    parse_job_id,
    sbatch_command,
    submit,
    submit_waiting,
)
from test_headless_render import enable_textures
from test_transfer_page import destination, settle, transfer_app


def write_mesc(path, units: dict) -> None:
    """A .mesc with only what list_units reads: per unit a Channel_0 of (frames, 2, 2) and its header attributes."""
    with h5py.File(path, "w") as f:
        for key, (frames, attrs) in units.items():
            group = f.create_group(key)
            group.create_dataset("Channel_0", data=np.zeros((frames, 2, 2), dtype=np.uint16))
            for name, value in attrs.items():
                group.attrs[name] = value


UNITS = {
    "MSession_0/MUnit_14": (500, {"ImageRoleDebugString": b"measurement", "TStepInMs": 1.29, "Comment": b"0.35,770Hz"}),
    "MSession_0/MUnit_9": (300, {"ImageRoleDebugString": b"measurement", "TStepInMs": 0.87}),
    "MSession_0/MUnit_13": (1, {"ImageRoleDebugString": b"measurement", "TStepInMs": 1966.0}),
    "MSession_0/MUnit_18": (10, {"ImageRoleDebugString": b"measurement"}),
    "MSession_1/MUnit_23": (500, {"ImageRoleDebugString": b"motionCorrection", "TStepInMs": 1.29}),
}


def test_list_units_reads_headers_and_keeps_only_runnable_scans(tmp_path):
    write_mesc(tmp_path / "scan.mesc", UNITS)
    units = list_units(tmp_path / "scan.mesc")
    assert [u.key for u in units] == ["MSession_0/MUnit_9", "MSession_0/MUnit_13", "MSession_0/MUnit_14", "MSession_0/MUnit_18", "MSession_1/MUnit_23"]
    unit = units[2]
    assert (unit.role, unit.frames, unit.comment) == ("measurement", 500, "0.35,770Hz")
    assert unit.fs == pytest.approx(1000 / 1.29) and unit.seconds == pytest.approx(0.645)
    assert [u.key for u in units if is_runnable(u, Asap7Params())] == ["MSession_0/MUnit_9", "MSession_0/MUnit_14"]
    assert [u.key for u in units if is_runnable(u, Asap7Params(exclude_initial_frames=400))] == ["MSession_0/MUnit_14"]


def test_find_mesc_in_files_and_folders(tmp_path):
    (tmp_path / "day" / "sub").mkdir(parents=True)
    for path in (tmp_path / "a.mesc", tmp_path / "day" / "sub" / "b.MESC", tmp_path / "day" / "notes.txt"):
        path.write_bytes(b"")
    found = find_mesc([tmp_path / "a.mesc", tmp_path / "day", tmp_path / "day" / "notes.txt"])
    assert [relative for _, relative in found] == ["a.mesc", "day/sub/b.MESC"]


def test_sbatch_command_quotes_paths_and_keeps_home_remote():
    settings = Settings(partition="GPUA100", gres="gpu:1", email="me@lab.org")
    job = Job("C:/scan.mesc", "/biohpc/data2/M231/my scan.mesc", "MSession_0/MUnit_14", dir_save("/biohpc/data2/M231/my scan.mesc", "MSession_0/MUnit_14", datetime(2026, 10, 13, 22, 5)))
    assert job.dir_save == "/biohpc/data2/M231/masknmf_asap7/MSession_0_MUnit_14_detrend_20261013_220500"
    command = sbatch_command(settings, job, Asap7Params(mad_correlation_threshold=0.5))
    dir_run = "/project/OBI/OBI_faculty/Losonczy_lab/shared/data2/M231/masknmf_asap7/MSession_0_MUnit_14_detrend_20261013_220500"
    assert command.startswith(f"mkdir -p {dir_run}/logs && sbatch --parsable --job-name=asap7_MUnit_14 --partition=GPUA100 ")
    assert f"--output={dir_run}/logs/slurm_%j.out" in command
    assert "--gres=gpu:1 --mail-type=END,FAIL --mail-user=me@lab.org" in command
    assert '"$HOME"/repos/pml_utilities/hpc/asap7/run_asap7.sh "$HOME"/repos/pml_utilities/hpc/asap7/asap7_spine.py' in command
    words = shlex.split(command.split("/asap7_spine.py ", 1)[1])
    assert words == ["/project/OBI/OBI_faculty/Losonczy_lab/shared/data2/M231/my scan.mesc", "MSession_0/MUnit_14", "0", "200", "0.5", "0.3", dir_run]

    bare = sbatch_command(Settings(partition="p", gres="", dir_asap7="/opt/asap7/"), job, Asap7Params())
    assert "--gres" not in bare and "--mail" not in bare and "/opt/asap7/run_asap7.sh /opt/asap7/asap7_spine.py" in bare
    with pytest.raises(ValueError, match="outside /biohpc"):
        sbatch_command(settings, job._replace(mesc="/archive/scan.mesc"), Asap7Params())


def test_parse_job_id():
    assert parse_job_id("4242\n") == "4242"
    assert parse_job_id("4242;biohpc") == "4242"
    with pytest.raises(OSError):
        parse_job_id("sbatch: error: invalid partition")


def refuse_ssh(command: str) -> str:
    raise OSError("Permission denied (publickey,password)")


def landed(tmp_path, size_remote: int):
    (tmp_path / "scan.mesc").write_bytes(b"m" * 10)
    (tmp_path / "mount" / "d").mkdir(parents=True)
    (tmp_path / "mount" / "d" / "scan.mesc").write_bytes(b"m" * size_remote)
    storage = MountedStorage(str(tmp_path / "mount"), "/biohpc")
    jobs = [Job(str(tmp_path / "scan.mesc"), "/biohpc/d/scan.mesc", unit, f"/biohpc/d/out_{i}") for i, unit in enumerate(("MSession_0/MUnit_1", "MSession_0/MUnit_2"))]
    return storage, jobs


def test_submit_one_job_per_unit_and_records_them(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    storage, jobs = landed(tmp_path, size_remote=10)
    commands = []
    run = lambda command: commands.append(command) or f"{100 + len(commands)}\n"
    submitted = submit(run, Settings(partition="p"), jobs, Asap7Params(), storage)
    assert [job["job_id"] for job in submitted] == ["101", "102"]
    assert ["MSession_0/MUnit_1" in commands[0], "MSession_0/MUnit_2" in commands[1]] == [True, True]
    assert [job["job_id"] for job in load_jobs()] == ["101", "102"]
    assert load_jobs()[0]["params"]["detrend_window_s"] == 0.3


def test_submit_refuses_a_partial_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    storage, jobs = landed(tmp_path, size_remote=4)
    commands = []
    with pytest.raises(OSError, match="4 bytes on BioHPC, 10 here"):
        submit(commands.append, Settings(partition="p"), jobs, Asap7Params(), storage)
    assert commands == [] and [job["job_id"] for job in load_jobs()] == [None, None]


def test_jobs_wait_when_biohpc_refuses_and_submit_later(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    storage, jobs = landed(tmp_path, size_remote=10)

    with pytest.raises(OSError, match="Permission denied"):
        submit(refuse_ssh, Settings(partition="p"), jobs, Asap7Params(channel=1), storage)
    waiting = load_jobs()
    assert [job["job_id"] for job in waiting] == [None, None] and waiting[0]["params"]["channel"] == 1

    (tmp_path / "scan.mesc").unlink()
    commands = []
    submit_waiting(lambda command: commands.append(command) or "55", Settings(partition="p"), waiting, storage)
    assert [job["job_id"] for job in load_jobs()] == ["55", "55"]
    assert all(" MSession_0/MUnit_1 1 " in c or " MSession_0/MUnit_2 1 " in c for c in commands)


def test_job_states_from_sacct():
    out = "101|COMPLETED|01:02:03|0:0\n102|FAILED|00:00:09|1:0\n"
    asked = []
    states = job_states(lambda command: asked.append(command) or out, ["101", "102"])
    assert states == {"101": "COMPLETED 01:02:03", "102": "FAILED 00:00:09"}
    assert "-j 101,102" in asked[0]
    assert job_states(lambda command: pytest.fail("no jobs, no sacct"), [None]) == {}


def processing_app(tmp_path, monkeypatch):
    app = transfer_app(tmp_path, monkeypatch)
    write_mesc(tmp_path / "scan.mesc", UNITS)
    app.sources = [str(tmp_path / "scan.mesc")]
    app.process = True
    app.settings.partition = "GPUA100"
    destination(app, "/biohpc/data2/M231")
    app.read_mesc()
    settle(app)
    return app


def test_transfer_page_submits_the_units_chosen_once_sent(tmp_path, monkeypatch):
    app = processing_app(tmp_path, monkeypatch)
    assert app.problems() == ["choose the units to process"]
    app.chosen = {(str(tmp_path / "scan.mesc"), "MSession_0/MUnit_14")}
    assert app.problems() == []
    assert app.label_action().endswith("Send 1 item, process 1 unit")
    commands = []
    monkeypatch.setattr(app, "run_remote", lambda command: commands.append(command) or "777")
    app.act()
    settle(app)
    assert not app.failed, app.message
    assert app.message == "submitted job(s) 777 on GPUA100"
    assert (tmp_path / "mount" / "data2" / "M231" / "scan.mesc").is_file()
    assert len(commands) == 1 and "/project/OBI/OBI_faculty/Losonczy_lab/shared/data2/M231/scan.mesc MSession_0/MUnit_14 " in commands[0]
    assert [job["job_id"] for job in app.jobs] == ["777"]
    assert app.jobs[0]["dir_save"].startswith("/biohpc/data2/M231/masknmf_asap7/MSession_0_MUnit_14_detrend_")
    assert json.loads((tmp_path / "home" / "jobs.json").read_text())[0]["unit"] == "MSession_0/MUnit_14"


def test_the_window_keeps_jobs_biohpc_refused_and_submits_them_later(tmp_path, monkeypatch):
    app = processing_app(tmp_path, monkeypatch)
    app.chosen = {(str(tmp_path / "scan.mesc"), "MSession_0/MUnit_14")}

    monkeypatch.setattr(app, "run_remote", refuse_ssh)
    app.act()
    settle(app)
    assert app.failed and app.message.startswith("1 job(s) wait to be submitted")
    assert (tmp_path / "mount" / "data2" / "M231" / "scan.mesc").is_file()
    assert [job["job_id"] for job in app.jobs] == [None]

    monkeypatch.setattr(app, "run_remote", lambda command: "901")
    app.submit_waiting()
    settle(app)
    assert not app.failed and [job["job_id"] for job in app.jobs] == ["901"]


def test_a_failed_transfer_submits_nothing(tmp_path, monkeypatch):
    app = processing_app(tmp_path, monkeypatch)
    app.chosen = {(str(tmp_path / "scan.mesc"), "MSession_0/MUnit_14")}
    (tmp_path / "mount" / "data2" / "M231").mkdir()
    (tmp_path / "mount" / "data2" / "M231" / "scan.mesc").write_bytes(b"old")
    monkeypatch.setattr(app, "run_remote", lambda command: pytest.fail("submitted after a failed transfer"))
    app.act()
    settle(app)
    assert app.failed and "already exists" in app.message
    assert app.jobs == [] and app.pending == []


def test_processing_needs_a_partition_and_a_mesc(tmp_path, monkeypatch):
    app = processing_app(tmp_path, monkeypatch)
    app.settings.partition = ""
    app.chosen = {(str(tmp_path / "scan.mesc"), "MSession_0/MUnit_14")}
    assert app.problems() == ["set the GPU partition processing runs on, in Settings > Processing"]
    app.settings.partition = "p"
    app.sources = [str(tmp_path / "rec.tif")]
    assert app.problems() == ["reading the .mesc files' units"]
    app.read_mesc()
    settle(app)
    assert app.problems() == ["processing runs on .mesc files; none of the sources is or holds one"]
    assert app.chosen == set()


class ProcessingDriver:
    """Draws step 4, the submitted jobs and the Processing settings tab for three frames."""

    def __init__(self, app):
        self.app = app
        self.frames = 0

    def __call__(self) -> None:
        self.frames += 1
        imgui.begin("processing")
        self.app.draw_processing()
        self.app.draw_jobs()
        self.app.draw_processing_settings()
        imgui.end()
        if self.frames >= 3:
            hello_imgui.get_runner_params().app_shall_exit = True


def test_processing_widgets_render(tmp_path, monkeypatch):
    app = processing_app(tmp_path, monkeypatch)
    app.jobs = [
        {"job_id": "1", "unit": "MSession_0/MUnit_14", "mesc": "/m.mesc", "dir_save": "/out", "chosen_at": "then", "submitted_at": "now"},
        {"job_id": None, "unit": "MSession_0/MUnit_15", "mesc": "/m.mesc", "dir_save": "/out2", "chosen_at": "then", "submitted_at": None},
    ]
    app.job_states = {"1": "FAILED 00:00:09"}
    draw = ProcessingDriver(app)

    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    params.ini_filename = "imgui_lab4_interface_processing_test.ini"
    params.callbacks.post_init = enable_textures
    params.callbacks.show_gui = draw
    try:
        immapp.run(params)
    except Exception as e:
        pytest.skip(f"null backend unavailable: {e}")
    assert draw.frames == 3
