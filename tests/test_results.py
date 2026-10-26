import json
import shutil
from contextlib import closing
from pathlib import Path

import pytest

from imgui_lab4_interface import Catalog, MountedStorage, Settings, SshStorage, processing
from imgui_lab4_interface.catalog import match_unit, scan_machines
from imgui_lab4_interface.storage import machine_roots, open_machine
from test_processing import write_mesc

REC = "TZ_SCE014_asap7_20260822_am"
UNITS = {
    "MSession_0/MUnit_1": (40, {"ImageRoleDebugString": b"measurement", "TStepInMs": 1.0, "MethodType": 10}),
    "MSession_0/MUnit_2": (1, {"ImageRoleDebugString": b"background", "TStepInMs": 1.0, "MethodType": 2}),
    "MSession_0/MUnit_14": (30, {"ImageRoleDebugString": b"measurement", "TStepInMs": 2.0, "MethodType": 8}),
}


def manifest(status: str, data: str, started: str = "20261023T005441") -> str:
    run = {"command": "masknmf run --frame-rate 500", "started": started, "status": status}
    return json.dumps({"masknmf_version": "0.4", "pipeline": "one-photon-culture", "run": run, "inputs": {"data": {"path": data}}})


def write(root, layout: dict) -> None:
    for relative, content in layout.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content) if isinstance(content, str) else path.write_bytes(b"x" * content)


@pytest.fixture
def machines(tmp_path):
    """Two machines: home holds the .mesc, a unit tiff and runs; lab ran a pipeline on home's tiff, copied over."""
    home, lab = tmp_path / "home", tmp_path / "lab"
    (home / "data" / REC).mkdir(parents=True)
    write_mesc(home / "data" / REC / f"{REC}.mesc", UNITS)
    write(
        home,
        {
            f"data/tiffs/{REC}__MSession_0_MUnit_14.tiff": 10,
            "data/runs/20261023T005441_one-photon-culture/config.json": manifest("done", f"/data/tiffs/{REC}__MSession_0_MUnit_14.tiff"),
            "data/runs/20261023T005441_one-photon-culture/results.hdf5": 10,
            "data/runs/20261023T005441_one-photon-culture/summary.png": 5,
            "data/runs/20261023T010000_one-photon-culture/config.json": manifest("failed", f"/data/{REC}/{REC}.mesc", "20261023T010000"),
            "data/runs/20261023T010000_one-photon-culture/20261023T010000_one-photon-culture.log": "step 1\nValueError: no frames\n",
            "data/runs/notes/config.json": json.dumps({"other": "tool"}),
            f"data/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr/zarr.json": "{}",
            f"data/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr/traces.png": 5,
            f"data/{REC}/stack.zarr/inside.png": 5,
            f"figures/{REC}_MUnit_1_trace.png": 5,
            "figures/unrelated.png": 5,
        },
    )
    write(lab, {"work/20261024T090000_two-photon/config.json": manifest("running", f"C:\\copies\\{REC}__MSession_0_MUnit_1.tif", "20261024T090000")})
    return [
        {"name": "home", "reach": "mounted", "host": "", "mount_root": str(home), "mount_base": "/", "roots": ["/data", "/figures", ""]},
        {"name": "lab", "reach": "mounted", "host": "", "mount_root": str(lab), "mount_base": "/", "roots": ["/work"]},
    ]


def rows(catalog, query: str) -> list[tuple]:
    with closing(catalog.connect()) as db:
        return [tuple(row) for row in db.execute(query)]


def test_scan_reads_runs_units_and_results(machines, tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    found, failed = scan_machines(catalog, machines)
    assert failed == [] and [scan["root"] for scan in found] == ["/data", "/figures", "/work"]
    assert rows(catalog, "SELECT storage, path, kind, status, started, fs, error, recording, unit FROM runs ORDER BY started") == [
        ("home", "/data/runs/20261023T005441_one-photon-culture", "masknmf-run", "done", "2026-10-23 00:54", 500.0, "", REC, "MSession_0/MUnit_14"),
        ("home", "/data/runs/20261023T010000_one-photon-culture", "masknmf-run", "failed", "2026-10-23 01:00", 500.0, "ValueError: no frames", REC, ""),
        ("home", f"/data/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr", "vnoiser", "done", "2026-10-24 01:04", None, "", REC, "MSession_0/MUnit_1"),
        ("lab", "/work/20261024T090000_two-photon", "masknmf-run", "running", "2026-10-24 09:00", 500.0, "", REC, "MSession_0/MUnit_1"),
    ]
    assert rows(catalog, "SELECT path, unit, role, frames, fs FROM units ORDER BY path, unit") == [
        (f"/data/{REC}/{REC}.mesc", "MSession_0/MUnit_1", "measurement", 40, 1000.0),
        (f"/data/{REC}/{REC}.mesc", "MSession_0/MUnit_14", "measurement", 30, 500.0),
        (f"/data/{REC}/{REC}.mesc", "MSession_0/MUnit_2", "background", 1, 1000.0),
        (f"/data/tiffs/{REC}__MSession_0_MUnit_14.tiff", "MSession_0/MUnit_14", "export", None, None),
    ]
    assert rows(catalog, "SELECT path, run, unit FROM results ORDER BY path") == [
        (f"/data/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr/traces.png", f"/data/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr", "MSession_0/MUnit_1"),
        ("/data/runs/20261023T005441_one-photon-culture/summary.png", "/data/runs/20261023T005441_one-photon-culture", "MSession_0/MUnit_14"),
        (f"/figures/{REC}_MUnit_1_trace.png", "", "MSession_0/MUnit_1"),
        ("/figures/unrelated.png", "", ""),
    ]


def test_overview_has_a_row_per_unit_used_or_filmed(machines, tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    scan_machines(catalog, machines)
    overview = catalog.overview()
    by_key = {(row["recording"], row["unit"]): row for row in overview["rows"]}
    # the background snapshot is left out, and the failed run that named no unit keeps the recording's row
    assert list(by_key) == [(REC, ""), (REC, "MSession_0/MUnit_1"), (REC, "MSession_0/MUnit_14")]
    unit_1, unit_14 = by_key[(REC, "MSession_0/MUnit_1")], by_key[(REC, "MSession_0/MUnit_14")]
    assert (unit_1["fs"], unit_1["frames"], unit_1["indicator"], unit_1["storages"]) == (1000.0, 40, "ASAP7", ["home", "lab"])
    assert [run["status"] for run in unit_1["runs"]] == ["running", "done"]
    assert len(unit_1["results"]) == 2
    assert {data["path"] for data in unit_14["data"]} == {f"/data/{REC}/{REC}.mesc", f"/data/tiffs/{REC}__MSession_0_MUnit_14.tiff"}
    assert [data["path"] for data in by_key[(REC, "")]["data"]] == [f"/data/{REC}/{REC}.mesc"]
    assert {scan["storage"] for scan in overview["scans"]} == {"home", "lab"}


def test_rescan_reuses_mesc_headers_until_the_file_changes(machines, tmp_path, monkeypatch):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    calls = []
    list_units = processing.list_units
    monkeypatch.setattr(processing, "list_units", lambda path: calls.append(path) or list_units(path))
    home = open_machine(machines[0])
    catalog.scan(home, "/data")
    catalog.scan(home, "/data")
    assert len(calls) == 1
    write_mesc(tmp_path / "home" / "data" / REC / f"{REC}.mesc", {"MSession_0/MUnit_1": UNITS["MSession_0/MUnit_1"]})
    catalog.scan(home, "/data")
    assert len(calls) == 2
    assert rows(catalog, "SELECT unit FROM units WHERE role != 'export'") == [("MSession_0/MUnit_1",)]


def test_an_unreachable_machine_keeps_its_last_scan(machines, tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    scan_machines(catalog, machines)
    shutil.rmtree(tmp_path / "lab")
    found, failed = scan_machines(catalog, machines)
    assert len(found) == 2 and len(failed) == 1 and failed[0].startswith("lab /work: ")
    assert rows(catalog, "SELECT status FROM runs WHERE storage = 'lab'") == [("running",)]


def test_match_unit_rules():
    recordings, by_folder, known = [REC, "TZ_SCE014"], {"/data/x": REC}, {REC: {"MSession_1/MUnit_3"}}
    assert match_unit([f"/a/{REC}__MSession_0_MUnit_5.tif"], recordings, by_folder, known) == (REC, "MSession_0/MUnit_5")
    assert match_unit(["/data/x/out/MUnit_3/fig.png"], recordings, by_folder, known) == (REC, "MSession_1/MUnit_3")
    assert match_unit(["/f/TZ_SCE014_asap7_MUnit_9.png"], [REC], {}, {}) == (REC, "MSession_0/MUnit_9")
    assert match_unit(["/f/other.png"], recordings, by_folder, known) == ("", "")


def test_machines_settings_round_trip_and_storage(tmp_path):
    settings = Settings()
    assert [m["reach"] for m in settings.machines] == ["local"]
    settings.machines.append({"name": "pml-w1", "reach": "ssh", "host": "fo@pml-w1", "mount_root": "", "mount_base": "/", "roots": ["/home/fo/data"]})
    settings.save(tmp_path / "settings.json")
    loaded = Settings.load(tmp_path / "settings.json")
    assert loaded.machines == settings.machines
    assert open_machine(loaded.machines[1]).label == "pml-w1" and isinstance(open_machine(loaded.machines[1]), SshStorage)
    local = {**loaded.machines[0], "roots": ["~/data", " "]}
    assert machine_roots(local) == [(Path.home() / "data").as_posix()]
    assert open_machine(local).local_path(machine_roots(local)[0]) == Path.home() / "data"
    assert MountedStorage(str(tmp_path), "/x", label="mine").label == "mine"


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("find") is None, reason="needs sh and find to stand in for ssh")
def test_ssh_reads_many_files_and_walks_into_vnoiser_runs(machines, tmp_path):
    storage = SshStorage("unused", label="far")
    storage.base = ["sh", "-c"]
    root = tmp_path / "home" / "data"
    paths = [str(root / "runs" / "20261023T005441_one-photon-culture" / "config.json"), str(root / "missing.json"), str(root / "runs" / "notes" / "config.json")]
    texts = storage.read_texts(paths)
    assert sorted(texts) == sorted([paths[0], paths[2]]) and json.loads(texts[paths[2]]) == {"other": "tool"}
    walked = {record.path[len(str(root)) :] for record in storage.walk(str(root))}
    assert f"/{REC}/vnoiser/MUnit_1/{REC}.2026-10-24-01-04-36.vnoiser.zarr/traces.png" in walked
    assert f"/{REC}/stack.zarr" in walked and f"/{REC}/stack.zarr/inside.png" not in walked


class ResultsDriver:
    """Opens Results with a unit picked, and draws the Machines settings beside it."""

    def __init__(self, app, key: tuple[str, str]):
        self.app, self.key, self.frame = app, key, 0

    def __call__(self) -> None:
        from imgui_bundle import hello_imgui, imgui

        self.frame += 1
        if not self.app.user:
            self.app.work_offline()
        self.app.page = "results"
        self.app.picked_result = self.key
        self.app.dialog.render()
        imgui.begin("machines")
        self.app.draw_machines()
        imgui.end()
        if self.frame > 40:
            hello_imgui.get_runner_params().app_shall_exit = True


def test_results_page_renders_and_opens_in_place(machines, tmp_path, monkeypatch):
    import imgui_data_loader as idl
    from imgui_bundle import hello_imgui, immapp

    from imgui_lab4_interface import viewers
    from imgui_lab4_interface.app import Lab4App
    from test_headless_render import enable_textures
    from test_transfer_page import settle

    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "app"))
    scan_machines(Catalog(), machines)
    idl.ensure_assets()
    app = Lab4App(Settings(machines=machines))
    app.watch_catalog()
    settle(app)
    assert len(app.overview["rows"]) == 3
    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.app_window_params.window_geometry.size = (1040, 720)
    params.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    params.ini_filename = "imgui_lab4_interface_results_test.ini"
    params.callbacks.post_init = enable_textures
    params.callbacks.show_gui = ResultsDriver(app, (REC, "MSession_0/MUnit_1"))
    try:
        immapp.run(params)
    except Exception as e:
        pytest.skip(f"null backend unavailable: {e}")
    app.executor.shutdown(wait=True)
    app.executor_long.shutdown(wait=True)
    assert app.picked_result == (REC, "MSession_0/MUnit_1") and app.page == "results"

    launched = []
    monkeypatch.setattr(viewers, "launch", lambda argv: launched.append(argv) or tmp_path / "log")
    run = "/data/runs/20261023T005441_one-photon-culture"
    app.open_on("home", run, "masknmf-run")
    assert launched == [[app.settings.command_masknmf, "view", str(tmp_path / "home" / run.lstrip("/"))]] and not app.failed
    app.open_on("lab", "/work/missing", None)
    assert app.failed and "not found" in app.message
    app.open_on("gone", "/x", None)
    assert app.failed and "Settings > Machines" in app.message
