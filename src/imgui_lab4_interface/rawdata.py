"""What the slice table reads from raw data: the images in a .mesc, and the current-clamp runs in a MIES .nwb."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from imgui_lab4_interface.processing import number, read_attr

# longest side of a thumbnail, in pixels
SIZE_THUMB = 192
# input resistance: a negative step at least 20 ms long, its ΔV averaged over the 10 ms before 50 ms in, or before its end
SECONDS_STEP_MIN, SECONDS_RIN, SECONDS_RIN_WINDOW = 0.020, 0.050, 0.010
# a spike crosses 0 mV upward and falls back below -20 mV within 5 ms, which a slow depolarization through 0 mV does
# not; a step's first 1 ms is its transient, not a spike
VOLTS_SPIKE, VOLTS_RESET, SECONDS_SPIKE, SECONDS_EDGE = 0.0, -0.020, 0.005, 0.001


def thumbnail(frame: np.ndarray) -> np.ndarray:
    """frame shrunk by block means to at most SIZE_THUMB a side, stretched to 8 bits between its 1st and 99.7th percentiles."""
    step = max(1, -(-max(frame.shape) // SIZE_THUMB))
    h, w = (n // step * step for n in frame.shape)
    small = (
        frame[:h, :w]
        .astype(np.float32)
        .reshape(h // step, step, w // step, step)
        .mean(axis=(1, 3))
    )
    lo, hi = np.percentile(small, (1, 99.7))
    return (np.clip((small - lo) / max(hi - lo, 1e-9), 0, 1) * 255).astype(np.uint8)


def mesc_images(path: Path) -> list[dict]:
    """Each unit of a .mesc: when it was taken (local time), camera or 2P, method, objective, size, and its middle frame."""
    images = []
    with h5py.File(path, "r") as f:
        for name_session in sorted(
            (k for k in f if k.startswith("MSession")), key=number
        ):
            session = f[name_session]
            for name_unit in sorted(
                (k for k in session if k.startswith("MUnit")), key=number
            ):
                unit = session[name_unit]
                if "Channel_0" not in unit:
                    continue
                data = unit["Channel_0"]
                modalities = json.loads(read_attr(unit, "ModalityJSON") or "{}").get(
                    "modalities", []
                )
                images.append(
                    {
                        "unit": f"{name_session}/{name_unit}",
                        "taken": datetime.fromtimestamp(
                            int(read_attr(unit, "MeasurementDatePosix", 0))
                        ).isoformat(" ", "seconds"),
                        "kind": "camera"
                        if read_attr(unit, "TechnologyTypeDebugString") == "camera"
                        else "2P",
                        "method": read_attr(unit, "MethodTypeDebugString", ""),
                        "objective": next(
                            (
                                m.get("currentState")
                                for m in modalities
                                if m.get("name") == "Objective"
                            ),
                            None,
                        ),
                        "frames": data.shape[0],
                        "height": data.shape[1],
                        "width": data.shape[2],
                        "comment": read_attr(unit, "Comment", ""),
                        "thumb": thumbnail(data[data.shape[0] // 2]),
                    }
                )
    return images


def count_spikes(v: np.ndarray, width: int) -> int:
    """Upward crossings of VOLTS_SPIKE that fall below VOLTS_RESET within width samples, counting each spike once."""
    n, last = 0, -1
    for at in np.flatnonzero((v[:-1] < VOLTS_SPIKE) & (v[1:] >= VOLTS_SPIKE)):
        if (v[last + 1 : at + 1] < VOLTS_RESET).any() and (
            v[at + 1 : at + 1 + width] < VOLTS_RESET
        ).any():
            n, last = n + 1, at
    return n


def measure(
    series: h5py.Group, stimulus: Optional[h5py.Group]
) -> tuple[float, Optional[float], int]:
    """A sweep's resting Vm (V, before the first current change), input resistance (Ω) and spikes during positive steps."""
    v = series["data"][:] * float(series["data"].attrs["conversion"])
    if stimulus is None:
        return float(np.median(v)), None, 0
    rate = float(series["starting_time"].attrs["rate"])
    i = stimulus["data"][:] * float(stimulus["data"].attrs["conversion"])
    edges = [0, *(np.flatnonzero(np.diff(i)) + 1), len(i)]
    vm = float(np.median(v[: edges[1]]))
    rin, spikes = None, 0
    for a, b in zip(edges[:-1], edges[1:]):
        if i[a] < 0 and rin is None and b - a >= SECONDS_STEP_MIN * rate:
            end = min(a + int(SECONDS_RIN * rate), b)
            rin = (
                float(v[end - int(SECONDS_RIN_WINDOW * rate) : end].mean()) - vm
            ) / float(i[a])
        elif i[a] > 0:
            spikes += count_spikes(
                v[a + int(SECONDS_EDGE * rate) : b], int(SECONDS_SPIKE * rate)
            )
    return vm, rin, spikes


def nwb_runs(path: Path) -> list[dict]:
    """
    The current-clamp runs of a MIES .nwb, one per stimulus set from its first sweep: when it started (local time), its
    sweeps, median resting Vm and input resistance, and the most spikes in one sweep.
    """
    runs = []
    with h5py.File(path, "r") as f:
        presented = f.get("stimulus/presentation", {})
        for name in sorted(f["acquisition"]):
            series = f["acquisition"][name]
            if read_attr(series, "neurodata_type") != "CurrentClampSeries":
                continue
            stimset = read_attr(series, "stimulus_description", "")
            # MIES notes each sweep's start and its place in the set
            comments = read_attr(series, "comments", "")
            count = re.search(r"Set Sweep Count: ([\d.]+)", comments)
            start = re.search(r"sweep start: (\S+)", comments)
            if (
                not runs
                or stimset != runs[-1]["stimset"]
                or (count is not None and float(count[1]) == 0)
            ):
                started = (
                    datetime.fromisoformat(start[1])
                    .astimezone()
                    .replace(tzinfo=None)
                    .isoformat(" ", "seconds")
                    if start
                    else None
                )
                runs.append(
                    {
                        "sweep": int(read_attr(series, "sweep_number", 0)),
                        "started": started,
                        "stimset": stimset,
                        "measured": [],
                    }
                )
            runs[-1]["measured"].append(
                measure(series, presented.get(name.replace("_AD", "_DA")))
            )
    for run in runs:
        vm, rin, spikes = zip(*run.pop("measured"))
        rin = [r for r in rin if r is not None]
        run.update(
            sweeps=len(vm),
            vm_mv=float(np.median(vm)) * 1e3,
            rin_mohm=float(np.median(rin)) / 1e6 if rin else None,
            spikes=max(spikes),
        )
    return runs
