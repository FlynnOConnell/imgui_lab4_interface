"""Overnight processing: list a .mesc's units and submit one SLURM job per unit running `mbo asap7` on BioHPC."""

import dataclasses
import json
import posixpath
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

import h5py

from imgui_lab4_interface.config import Settings, data_dir
from imgui_lab4_interface.storage import Storage

SUFFIX_MESC = ".mesc"
ROLE_MEASUREMENT = "measurement"
# MEScan MethodTypes that are movies: unset, time series, chessboard, ribbons
METHODS_MOVIE = (0, 1, 8, 9, 10)
NAME_RESULTS = "masknmf_asap7"
FIELDS_SACCT = "JobID,State,Elapsed,ExitCode"


class MescUnit(NamedTuple):
    """
    One unit of a .mesc file, from its headers only.

    Attributes:
        key (str): The full key the pipeline takes, e.g. MSession_0/MUnit_14
        role (str): "measurement" for a scan; "background" or "motionCorrection" for snapshots and RTMC streams
        frames (int): Frames in the first channel
        fs (float | None): Frame rate, None when unknown
        comment (str): The operator's comment
        method (int): MEScan's MethodType, e.g. 1 time series, 2 z-stack, 8 chessboard, 10 ribbon, 11 multicube
    """

    key: str
    role: str
    frames: int
    fs: Optional[float]
    comment: str
    method: int = 0

    @property
    def seconds(self) -> Optional[float]:
        return self.frames / self.fs if self.fs else None


@dataclasses.dataclass
class Asap7Params:
    """`mbo asap7` settings; defaults match the detrend notebook. channel is 1-based."""

    channel: int = 1
    first_frame: int = 200
    mad_correlation_threshold: float = 0.6
    detrend_window_s: float = 0.3

    def problems(self) -> list[str]:
        found = []
        if self.channel < 1:
            found.append("channel starts at 1")
        if self.first_frame < 0:
            found.append("first frame must be 0 or more")
        if not 0 < self.mad_correlation_threshold <= 1:
            found.append("MAD threshold must be between 0 and 1")
        if self.detrend_window_s <= 0:
            found.append("detrend window must be over 0 s")
        return found


class Job(NamedTuple):
    """A unit to process: the local .mesc, its BioHPC path, the unit, and the run folder."""

    local: str
    mesc: str
    unit: str
    dir_save: str


def read_attr(group: h5py.Group, name: str, default=None):
    """An HDF5 attribute with byte strings and character arrays decoded."""
    value = group.attrs.get(name, default)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "dtype") and value.dtype.kind in "SU":
        return "".join(v.decode() if isinstance(v, bytes) else str(v) for v in value)
    return value


def list_units(path: Path) -> list[MescUnit]:
    """Units with a Channel_0, in number order."""
    units = []
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
                step_ms = float(read_attr(unit, "TStepInMs") or 0)
                units.append(
                    MescUnit(
                        key=f"{name_session}/{name_unit}",
                        role=str(read_attr(unit, "ImageRoleDebugString", "") or ""),
                        frames=int(unit["Channel_0"].shape[0]),
                        fs=1000.0 / step_ms if step_ms > 0 else None,
                        comment=str(read_attr(unit, "Comment", "") or ""),
                        method=int(read_attr(unit, "MethodType", 0) or 0),
                    )
                )
    return units


def number(name: str) -> tuple[int, str]:
    """Sort MUnit_10 after MUnit_9."""
    tail = name.rsplit("_", 1)[-1]
    return (int(tail), name) if tail.isdigit() else (1 << 30, name)


def is_runnable(unit: MescUnit, params: Asap7Params) -> bool:
    """Whether unit is a movie with a frame rate and frames past first_frame."""
    return (
        unit.role in (ROLE_MEASUREMENT, "")
        and unit.method in METHODS_MOVIE
        and unit.fs is not None
        and unit.frames > params.first_frame
    )


def find_mesc(sources: list[Path]) -> list[tuple[Path, str]]:
    """Every .mesc in sources, folders searched, with its path relative to the destination."""
    found = []
    for source in sources:
        if source.is_file() and source.suffix.lower() == SUFFIX_MESC:
            found.append((source, source.name))
        elif source.is_dir():
            found += [
                (p, f"{source.name}/{p.relative_to(source).as_posix()}")
                for p in sorted(source.rglob(f"*{SUFFIX_MESC}", case_sensitive=False))
            ]
    return found


def dir_save(mesc: str, unit: str, when: datetime) -> str:
    """The run folder for unit, beside the .mesc: <folder>/masknmf_asap7/<unit>_detrend_<when>."""
    return f"{posixpath.dirname(mesc)}/{NAME_RESULTS}/{unit.replace('/', '_')}_detrend_{when:%Y%m%d_%H%M%S}"


def remote_path(path: str) -> str:
    """Quote path for the remote shell, keeping a leading ~/ as $HOME."""
    if path.startswith("~/"):
        return f'"$HOME"/{shlex.quote(path[2:])}'
    return shlex.quote(path)


def cluster_path(settings: Settings, path: str) -> str:
    """path as BioHPC's nodes see it: mount_base (/biohpc) replaced by cluster_root."""
    base = "/" + settings.mount_base.strip("/")
    if path != base and not path.startswith(f"{base}/"):
        raise ValueError(f"{path} is outside {base}")
    return settings.cluster_root.rstrip("/") + path[len(base) :]


def sbatch_command(settings: Settings, job: Job, params: Asap7Params) -> str:
    """Shell command that makes the run folder and submits job; prints the job id."""
    dir_asap7 = settings.dir_asap7.rstrip("/")
    mesc = cluster_path(settings, job.mesc)
    dir_run = cluster_path(settings, job.dir_save)
    options = [
        "--parsable",
        f"--job-name=asap7_{posixpath.basename(job.unit)}",
        f"--partition={settings.partition}",
        f"--output={dir_run}/logs/slurm_%j.out",
    ]
    if settings.gres.strip():
        options.append(f"--gres={settings.gres.strip()}")
    if settings.email.strip():
        options += ["--mail-type=END,FAIL", f"--mail-user={settings.email.strip()}"]
    arguments = [
        mesc,
        job.unit,
        str(params.channel),
        str(params.first_frame),
        str(params.mad_correlation_threshold),
        str(params.detrend_window_s),
        dir_run,
    ]
    return " ".join(
        [
            "mkdir -p",
            shlex.quote(f"{dir_run}/logs"),
            "&& sbatch",
            *map(shlex.quote, options),
            remote_path(f"{dir_asap7}/run_asap7.sh"),
            *map(shlex.quote, arguments),
        ]
    )


def parse_job_id(output: str) -> str:
    """The job id sbatch --parsable prints: "123" or "123;cluster"."""
    match = re.match(r"\s*(\d+)", output)
    if match is None:
        raise OSError(f"sbatch printed no job id: {output.strip()!r}")
    return match.group(1)


def check_landed(storage: Storage, records: list[dict]) -> None:
    """Raise OSError when a .mesc on BioHPC differs in size from the local one."""
    for mesc, size in {(r["mesc"], r["bytes"]) for r in records}:
        found = {e.name: e.size for e in storage.listdir(posixpath.dirname(mesc))}
        name = posixpath.basename(mesc)
        if found.get(name) != size:
            raise OSError(
                f"{mesc} is {found.get(name, 'missing')} bytes on BioHPC, {size} here; not processing it"
            )


def new_record(job: Job, params: Asap7Params) -> dict:
    """A job as jobs.json keeps it; job_id stays None until sbatch takes it."""
    return {
        **job._asdict(),
        "bytes": Path(job.local).stat().st_size,
        "params": dataclasses.asdict(params),
        "chosen_at": datetime.now().isoformat(timespec="seconds"),
        "job_id": None,
        "submitted_at": None,
    }


def submit(
    run, settings: Settings, jobs: list[Job], params: Asap7Params, storage: Storage
) -> list[dict]:
    """
    Save one waiting job per unit to jobs.json, then submit them; any that fail stay waiting.

    run runs a remote shell command and returns its output (SshStorage.run).
    """
    records = [new_record(job, params) for job in jobs]
    save_jobs(load_jobs() + records)
    return submit_waiting(run, settings, records, storage)


def submit_waiting(
    run, settings: Settings, records: list[dict], storage: Storage
) -> list[dict]:
    """Check the files landed, then submit each job, saving its id to jobs.json."""
    check_landed(storage, records)
    for record in records:
        job = Job(record["local"], record["mesc"], record["unit"], record["dir_save"])
        record["job_id"] = parse_job_id(
            run(sbatch_command(settings, job, Asap7Params(**record["params"])))
        )
        record["submitted_at"] = datetime.now().isoformat(timespec="seconds")
        save_jobs(
            [
                record if entry["dir_save"] == record["dir_save"] else entry
                for entry in load_jobs()
            ]
        )
    return records


def path_jobs() -> Path:
    return data_dir() / "jobs.json"


def load_jobs() -> list[dict]:
    """Every job chosen on this machine, oldest first, submitted or waiting."""
    path = path_jobs()
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def save_jobs(jobs: list[dict]) -> None:
    path = path_jobs()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(jobs, indent=2))
    tmp.replace(path)


def job_states(run, job_ids: list[str]) -> dict[str, str]:
    """Each job's SLURM state and elapsed time, e.g. {"123": "COMPLETED 01:02:03"}, from sacct."""
    job_ids = [job_id for job_id in job_ids if job_id]
    if not job_ids:
        return {}
    out = run(
        f"sacct -n -X -P --format={FIELDS_SACCT} -j {','.join(map(shlex.quote, job_ids))}"
    )
    states = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            states[parts[0]] = f"{parts[1]} {parts[2]}"
    return states
