"""User settings and the app's data folder."""

import dataclasses
import json
import os
import platform
from pathlib import Path
from typing import Optional

BACKENDS = ("mounted", "ssh")
# how this computer reaches a machine's files
REACHES = ("local", "mounted", "ssh")

DESTINATIONS = [
    ["/biohpc/data2", "BioHPC data2"],
    ["/biohpc/data3", "BioHPC data3"],
    ["/biohpc/data4", "BioHPC data4"],
]


def data_dir() -> Path:
    """``~/.imgui_lab4_interface``, or ``IMGUI_LAB4_HOME`` when set; created on first use."""
    base = os.environ.get("IMGUI_LAB4_HOME")
    path = Path(base) if base else Path.home() / ".imgui_lab4_interface"
    path.mkdir(parents=True, exist_ok=True)
    return path


def this_computer() -> dict:
    """A machine entry for this computer with nothing to catalog yet."""
    return {
        "name": platform.node() or "this computer",
        "reach": "local",
        "host": "",
        "mount_root": "",
        "mount_base": "/",
        "roots": [],
    }


@dataclasses.dataclass
class Settings:
    """
    How this machine reaches BioHPC, and how processing jobs run.

    Attributes:
        backend (str): "mounted" or "ssh"
        mount_root (str): Local folder the share is mounted at
        mount_base (str): BioHPC path of mount_root, e.g. /biohpc
        ssh_host (str): ssh alias or user@host of a machine with /biohpc mounted, keys already set up
        destinations (list): [path, description] pairs offered on Transfer
        command_mbo (str): mbo executable, on PATH or a full path
        command_masknmf (str): masknmf executable, on PATH or a full path
        processing_host (str): ssh alias or user@host of a BioHPC login node, where jobs are submitted
        partition (str): SLURM partition with GPUs
        gres (str): GPUs per job, e.g. gpu:1; empty for none
        email (str): SLURM mail address; empty for none
        dir_asap7 (str): BioHPC folder holding run_asap7.sh; ~/ is the BioHPC home
        cluster_root (str): Where mount_base is on BioHPC nodes
        machines (list): Computers whose results the Results page catalogs. Each is a dict: name; reach, one of
            REACHES; host, the ssh alias or user@host; mount_root, the local folder showing the machine's
            mount_base; roots, the machine's folders to catalog, in its own paths (~ is this computer's home
            for a local machine)
    """

    backend: str = "mounted"
    mount_root: str = ""
    mount_base: str = "/biohpc"
    # the lab server's alias, as in pml_utilities
    ssh_host: str = "lab-research"
    destinations: list = dataclasses.field(
        default_factory=lambda: [list(d) for d in DESTINATIONS]
    )
    command_mbo: str = "mbo"
    command_masknmf: str = "masknmf"
    processing_host: str = "biohpc"
    partition: str = ""
    gres: str = "gpu:1"
    email: str = ""
    dir_asap7: str = "~/repos/pml_utilities/hpc/asap7"
    cluster_root: str = "/project/OBI/OBI_faculty/Losonczy_lab/shared"
    machines: list = dataclasses.field(default_factory=lambda: [this_computer()])

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        """Settings from json, defaults for anything missing or unreadable."""
        path = path or data_dir() / "settings.json"
        if not path.is_file():
            return cls()
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in loaded.items() if k in names})

    def save(self, path: Optional[Path] = None) -> None:
        path = path or data_dir() / "settings.json"
        path.write_text(json.dumps(dataclasses.asdict(self), indent=2))
