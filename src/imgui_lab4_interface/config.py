"""Per-user settings, and the folder everything this app writes lives under."""

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

BACKENDS = ("mounted", "ssh")

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


@dataclasses.dataclass
class Settings:
    """
    How this machine reaches BioHPC and where data may be sent.

    Attributes:
        backend (str): "mounted" for a share mounted on this machine, "ssh" for a host reached over ssh
        mount_root (str): The local folder the share is mounted at
        mount_base (str): The BioHPC path mount_root shows, e.g. /biohpc for a drive mapped to /biohpc
        ssh_host (str): An ssh alias or user@host whose keys are already set up
        destinations (list): [path, description] pairs offered on the Transfer page
        command_mbo (str): The mbo executable, a name on PATH or a full path
        command_masknmf (str): The masknmf executable, a name on PATH or a full path
        partition (str): The SLURM partition processing jobs run on, one with GPUs
        gres (str): The GPUs a processing job asks for, e.g. gpu:1; empty asks for none
        email (str): Where SLURM writes when a processing job ends or fails; empty for no mail
        dir_asap7 (str): The folder on BioHPC holding run_asap7.sh and asap7_spine.py; ~/ is the BioHPC home
        cluster_root (str): Where mount_base is on BioHPC's own nodes, e.g. the lab's /project folder the /biohpc share mounts
    """

    backend: str = "mounted"
    mount_root: str = ""
    mount_base: str = "/biohpc"
    ssh_host: str = "biohpc"
    destinations: list = dataclasses.field(default_factory=lambda: [list(d) for d in DESTINATIONS])
    command_mbo: str = "mbo"
    command_masknmf: str = "masknmf"
    partition: str = ""
    gres: str = "gpu:1"
    email: str = ""
    dir_asap7: str = "~/repos/pml_utilities/hpc/asap7"
    cluster_root: str = "/project/OBI/OBI_faculty/Losonczy_lab/shared"

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
