"""Per-user settings, and the folder everything this app writes lives under."""

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

BACKENDS = ("mounted", "ssh")

# placeholders carried over from the pml_utilities BioHPC widget; edit them on the Storage page
DESTINATIONS = [
    ["/archive/mbo/raw", "Raw acquisitions, long-term archive"],
    ["/archive/mbo/assembled", "Assembled volumes (zarr / tiff)"],
    ["/scratch/mbo/suite2p", "Suite2p outputs, fast scratch"],
    ["/work/mbo/shared", "Shared lab working set"],
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
        mount_root (str): The local folder BioHPC paths resolve under when mounted
        ssh_host (str): An ssh alias or user@host whose keys are already set up
        destinations (list): [path, description] pairs offered on the Transfer page
        command_mbo (str): The mbo executable, a name on PATH or a full path
        command_masknmf (str): The masknmf executable, a name on PATH or a full path
    """

    backend: str = "mounted"
    mount_root: str = ""
    ssh_host: str = "biohpc"
    destinations: list = dataclasses.field(default_factory=lambda: [list(d) for d in DESTINATIONS])
    command_mbo: str = "mbo"
    command_masknmf: str = "masknmf"

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
