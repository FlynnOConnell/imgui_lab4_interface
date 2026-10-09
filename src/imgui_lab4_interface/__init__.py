"""Move PML data onto BioHPC storage and attach Lab4 metadata to it."""

__version__ = "0.1.0"

from imgui_lab4_interface.catalog import KINDS, Catalog
from imgui_lab4_interface.config import Settings, data_dir
from imgui_lab4_interface.lab4 import Recording
from imgui_lab4_interface.storage import (
    SIDECAR_SUFFIX,
    Entry,
    MountedStorage,
    Record,
    SshStorage,
    Storage,
    Transfer,
    open_storage,
)

__all__ = [
    "KINDS",
    "SIDECAR_SUFFIX",
    "Catalog",
    "Entry",
    "MountedStorage",
    "Record",
    "Recording",
    "Settings",
    "SshStorage",
    "Storage",
    "Transfer",
    "data_dir",
    "open_storage",
    "__version__",
]
