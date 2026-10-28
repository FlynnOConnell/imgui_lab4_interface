"""Images read from .mesc files and ephys runs from MIES .nwb files, into the slice tables."""

import json
from datetime import datetime

import h5py
import numpy as np

from imgui_lab4_interface.rawdata import mesc_images, nwb_runs
from imgui_lab4_interface.slices import ROOT, SliceDatabase
from imgui_lab4_interface.storage import MountedStorage

RATE = 10_000
START = "2026-09-19T03:12:16.446Z"


def write_mesc(path) -> None:
    """A camera snapshot, a 2P z-stack, MUnit_10 after them, and a unit without data."""
    with h5py.File(path, "w") as f:
        session = f.create_group("MSession_0")
        for name, kind, method, shape in (
            ("MUnit_0", "camera", "timeSeries", (1, 40, 60)),
            ("MUnit_1", "aO", "zStack", (5, 300, 300)),
            ("MUnit_10", "camera", "timeSeries", (1, 40, 60)),
        ):
            unit = session.create_group(name)
            unit.create_dataset(
                "Channel_0",
                data=np.arange(np.prod(shape), dtype=np.uint16).reshape(shape),
            )
            unit.attrs["MeasurementDatePosix"] = 1789769455
            unit.attrs["TechnologyTypeDebugString"] = kind
            unit.attrs["MethodTypeDebugString"] = method
            unit.attrs["ModalityJSON"] = json.dumps(
                {"modalities": [{"name": "Objective", "currentState": "Obj_16x"}]}
            )
            unit.attrs["Comment"] = "slice 2" if name == "MUnit_1" else ""
        session.create_group("MUnit_2")


def sweep(f, n: int, stimset: str, count: int, spiking: bool) -> None:
    """
    A current-clamp sweep at -65 mV: 20 ms at 0 pA, a -100 pA step to -75 mV for 100 ms (100 MΩ), 20 ms back at 0, then
    200 ms at +200 pA, with 3 spikes from -50 mV, or a slow climb through 0 mV that is not one.
    """
    i = np.concatenate(
        [np.zeros(200), np.full(1000, -100.0), np.zeros(200), np.full(2000, 200.0)]
    )
    v = np.full(i.size, -65.0)
    v[200:1200] = -75.0
    v[1400:] = -50.0
    if spiking:
        for at in (1600, 2000, 2400):
            v[at : at + 10] = 20.0
    else:
        v[1600:] = np.linspace(-50, 30, 1800)
    name = f"data_{n:05d}_AD0"
    series = f.create_group(f"acquisition/{name}")
    series.attrs.update(
        neurodata_type="CurrentClampSeries",
        stimulus_description=stimset,
        sweep_number=n,
    )
    series.attrs["comments"] = (
        f"HS#0:Set Sweep Count: {count:.2f}\nHigh precision sweep start: {START}"
    )
    series.create_dataset("data", data=v.astype(np.float32)).attrs["conversion"] = 1e-3
    series.create_dataset("starting_time", data=0.0).attrs["rate"] = float(RATE)
    stimulus = f.create_group(f"stimulus/presentation/{name.replace('_AD', '_DA')}")
    stimulus.create_dataset("data", data=i.astype(np.float32)).attrs["conversion"] = (
        1e-12
    )


def write_nwb(path) -> None:
    """Two sweeps of one IV run that spike, then a new run whose cell only depolarizes."""
    with h5py.File(path, "w") as f:
        sweep(f, 0, "IV_CC_DA_0", 0, spiking=True)
        sweep(f, 1, "IV_CC_DA_0", 1, spiking=True)
        sweep(f, 2, "IV_CC_DA_0", 0, spiking=False)
        f.create_group("acquisition/data_00003_TTL").attrs["neurodata_type"] = (
            "TimeSeries"
        )


def test_each_unit_with_data_is_an_image(tmp_path):
    write_mesc(tmp_path / "a.mesc")
    images = mesc_images(tmp_path / "a.mesc")
    assert [i["unit"] for i in images] == [
        "MSession_0/MUnit_0",
        "MSession_0/MUnit_1",
        "MSession_0/MUnit_10",
    ]
    camera, stack, _ = images
    assert (
        camera["kind"] == "camera"
        and stack["kind"] == "2P"
        and stack["method"] == "zStack"
        and stack["objective"] == "Obj_16x"
    )
    assert (stack["frames"], stack["height"], stack["width"], stack["comment"]) == (
        5,
        300,
        300,
        "slice 2",
    )
    assert camera["taken"] == datetime.fromtimestamp(1789769455).isoformat(
        " ", "seconds"
    )
    assert (
        camera["thumb"].shape == (40, 60)
        and stack["thumb"].shape == (150, 150)
        and stack["thumb"].dtype == np.uint8
    )


def test_runs_measure_vm_resistance_and_spikes(tmp_path):
    write_nwb(tmp_path / "a.nwb")
    first, second = nwb_runs(tmp_path / "a.nwb")
    assert (
        first["sweep"],
        first["sweeps"],
        first["stimset"],
        second["sweep"],
        second["sweeps"],
    ) == (0, 2, "IV_CC_DA_0", 2, 1)
    assert first["started"] == datetime.fromisoformat(START).astimezone().replace(
        tzinfo=None
    ).isoformat(" ", "seconds")
    assert round(first["vm_mv"], 3) == -65 and round(first["rin_mohm"], 3) == 100
    assert first["spikes"] == 3 and second["spikes"] == 0


def test_reading_fills_the_tables_once_from_the_mount_or_a_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    folder = tmp_path / "mount" / ROOT.lstrip("/")
    folder.mkdir(parents=True)
    write_mesc(folder / "a.mesc")
    write_nwb(folder / "a.nwb")
    files = [
        {"path": name, "size": (folder / name).stat().st_size}
        for name in ("a.mesc", "a.nwb")
    ]
    db = SliceDatabase(tmp_path / "s.sqlite")
    storage = MountedStorage(str(tmp_path / "mount"))
    progress = {"done": 0}
    assert db.read_data(storage, files, progress) == (3, 2) and progress["done"] == 2
    # read again off the mount: copied to the cache, and the rows replaced, not added
    monkeypatch.setattr(storage, "local_path", lambda path: None)
    assert db.read_data(storage, files, {"done": 0}) == (3, 2)
    assert (tmp_path / "home" / "cache" / ROOT.lstrip("/") / "a.nwb").is_file()
    images, runs = db.images(), db.runs()
    assert (
        len(images) == 3
        and images[1]["thumb"].shape == (150, 150)
        and images[0]["name"] == "a.mesc"
    )
    assert [(r["name"], r["sweep"], r["spikes"]) for r in runs] == [
        ("a.nwb", 0, 3),
        ("a.nwb", 2, 0),
    ]
