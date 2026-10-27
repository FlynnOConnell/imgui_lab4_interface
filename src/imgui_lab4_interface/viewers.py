"""Launch dataset viewers as detached processes that log to a file."""

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from imgui_lab4_interface.catalog import KINDS
from imgui_lab4_interface.config import Settings, data_dir

FLAGS_DETACHED = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32"
    else 0
)


def command(settings: Settings, kind: str, local: Path) -> list[str]:
    """The command line that opens a dataset of kind at local, e.g. ["masknmf", "view", "<path>"]."""
    tool, args, _ = KINDS[kind]
    executable = settings.command_mbo if tool == "mbo" else settings.command_masknmf
    return [executable, *args, str(local)]


def launch(argv: list[str]) -> Path:
    """Start argv detached; returns its log file."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise FileNotFoundError(
            f"{argv[0]} not found; set its path in Settings > Viewers"
        )
    logs = data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / f"{datetime.now():%Y%m%dT%H%M%S}_{Path(argv[0]).stem}.log"
    with log.open("w") as out:
        subprocess.Popen(
            [executable, *argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=FLAGS_DETACHED,
            start_new_session=sys.platform != "win32",
        )
    return log
