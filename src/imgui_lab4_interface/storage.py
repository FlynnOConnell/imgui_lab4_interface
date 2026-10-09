"""
Where data goes: a Storage protocol over BioHPC paths, the two ways this machine reaches them, and a Transfer
that copies local files into one.

Paths given to a Storage are always BioHPC's own posix paths (/archive/..., /work/...); each backend maps them
to whatever it talks to. The window only ever sees the protocol, so a remote backend (an http service, Globus)
slots in by implementing it.
"""

import json
import os
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple, Optional, Protocol

from imgui_lab4_interface.config import Settings

SIDECAR_SUFFIX = ".lab4.json"
SIZE_CHUNK = 1 << 20
TIMEOUT_SSH = 15
TIMEOUT_WALK = 1800
FLAGS_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Entry(NamedTuple):
    name: str
    is_dir: bool
    size: int


class Record(NamedTuple):
    """One file or folder a walk found; a hidden or .zarr folder is listed but not entered."""

    path: str
    is_dir: bool
    size: int
    mtime: float


class Storage(Protocol):
    label: str

    def check(self) -> None:
        """Raise OSError when the storage cannot be reached."""

    def exists(self, path: str) -> bool: ...

    def listdir(self, path: str) -> list[Entry]: ...

    def read_text(self, path: str) -> str: ...

    def open_write(self, path: str) -> BinaryIO:
        """A writable binary file at path, its parent folders made; closing it finishes the write."""

    def walk(self, root: str) -> Iterator[Record]:
        """Everything under root, root itself excluded."""

    def local_path(self, path: str) -> Optional[Path]:
        """Where path is on this machine, for a viewer to open; None when it is not reachable as a file here."""


class MountedStorage:
    """BioHPC paths resolved under a local folder the share is mounted at, e.g. a mapped lamella drive."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.label = str(root) or "no mount root"

    def local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def check(self) -> None:
        if not str(self.root) or str(self.root) == ".":
            raise OSError("no mount root set; choose one on the Storage page")
        if not self.root.is_dir():
            raise OSError(f"{self.root} is not reachable")

    def exists(self, path: str) -> bool:
        return self.local(path).exists()

    def listdir(self, path: str) -> list[Entry]:
        entries = [Entry(p.name, p.is_dir(), 0 if p.is_dir() else p.stat().st_size) for p in self.local(path).iterdir()]
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def read_text(self, path: str) -> str:
        return self.local(path).read_text()

    def open_write(self, path: str) -> BinaryIO:
        target = self.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.open("wb")

    def walk(self, root: str) -> Iterator[Record]:
        stack = [(self.local(root), root.rstrip("/"))]
        while stack:
            folder, remote = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat = entry.stat(follow_symlinks=False)
                path = f"{remote}/{entry.name}"
                yield Record(path, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime)
                if is_dir and not entry.name.startswith(".") and not entry.name.endswith(".zarr"):
                    stack.append((entry.path, path))

    def local_path(self, path: str) -> Optional[Path]:
        return self.local(path)


class SshWriter:
    """A remote file fed through `cat` over ssh; close raises when the remote side failed."""

    def __init__(self, base: list[str], path: str):
        command = f"mkdir -p {shlex.quote(posixpath.dirname(path))} && cat > {shlex.quote(path)}"
        self.path = path
        self.proc = subprocess.Popen([*base, command], stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=FLAGS_NO_WINDOW)

    def write(self, chunk: bytes) -> None:
        self.proc.stdin.write(chunk)

    def close(self) -> None:
        self.proc.stdin.close()
        code = self.proc.wait()
        if code != 0:
            raise OSError(self.proc.stderr.read().decode(errors="replace").strip() or f"writing {self.path} failed")

    def __enter__(self) -> "SshWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SshStorage:
    """BioHPC reached over ssh with keys already set up; it never prompts, so a missing key fails fast."""

    def __init__(self, host: str):
        self.host = host
        self.label = f"ssh {host}"
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={TIMEOUT_SSH}", "-o", "StrictHostKeyChecking=accept-new", host]

    def run(self, command: str, timeout: float = 4 * TIMEOUT_SSH) -> str:
        try:
            result = subprocess.run([*self.base, command], capture_output=True, text=True, timeout=timeout, creationflags=FLAGS_NO_WINDOW)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise OSError(f"ssh {self.host}: {e}") from e
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or f"ssh {self.host} exited {result.returncode}")
        return result.stdout

    def check(self) -> None:
        self.run("true")

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {shlex.quote(path)} && echo y || echo n").strip() == "y"

    def listdir(self, path: str) -> list[Entry]:
        out = self.run(f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 -printf '%y\\t%s\\t%f\\n'")
        entries = []
        for line in out.splitlines():
            kind, size, name = line.split("\t", 2)
            entries.append(Entry(name, kind == "d", 0 if kind == "d" else int(size)))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def read_text(self, path: str) -> str:
        return self.run(f"cat {shlex.quote(path)}")

    def open_write(self, path: str) -> SshWriter:
        return SshWriter(self.base, path)

    def walk(self, root: str) -> Iterator[Record]:
        fmt = r"'%y\t%s\t%T@\t%p\n'"
        prune = r"\( -type d \( -name '.*' -o -name '*.zarr' \)"
        out = self.run(f"find {shlex.quote(root)} -mindepth 1 {prune} -printf {fmt} -prune \\) -o -printf {fmt}", timeout=TIMEOUT_WALK)
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                kind, size, mtime, path = parts
                yield Record(path, kind == "d", 0 if kind == "d" else int(size), float(mtime))

    def local_path(self, path: str) -> Optional[Path]:
        return None


def open_storage(settings: Settings) -> Storage:
    if settings.backend == "ssh":
        return SshStorage(settings.ssh_host)
    return MountedStorage(settings.mount_root)


def write_text(storage: Storage, path: str, text: str) -> None:
    with storage.open_write(path) as f:
        f.write(text.encode())


class Transfer:
    """
    Copies local files and folders into a destination folder, each under its own name, with a sidecar of
    metadata beside each when given. Run it on a worker thread and read its counters from the window.

    Args:
        storage (Storage): Where to copy to
        sources (list[Path]): Files and folders on this machine
        dest (str): The BioHPC folder they land in
        metadata (dict | None): Written as <name>.lab4.json beside each source, with the source's local path added
        overwrite (bool): Whether a source already at the destination is replaced instead of refused
    """

    def __init__(self, storage: Storage, sources: list[Path], dest: str, metadata: Optional[dict], overwrite: bool):
        self.storage = storage
        self.sources = sources
        self.dest = dest.rstrip("/")
        self.metadata = metadata
        self.overwrite = overwrite
        self.total = 0
        self.done = 0
        self.current = ""
        self.cancelled = False

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0

    def run(self) -> None:
        plan = []
        for source in self.sources:
            target = f"{self.dest}/{source.name}"
            if not self.overwrite and self.storage.exists(target):
                raise FileExistsError(f"{target} already exists; tick overwrite to replace it")
            if source.is_dir():
                plan += [(p, f"{target}/{p.relative_to(source).as_posix()}") for p in sorted(source.rglob("*")) if p.is_file()]
            else:
                plan.append((source, target))
        self.total = sum(p.stat().st_size for p, _ in plan)
        for local, remote in plan:
            self.current = remote
            with local.open("rb") as src, self.storage.open_write(remote) as dst:
                while chunk := src.read(SIZE_CHUNK):
                    if self.cancelled:
                        raise InterruptedError(f"cancelled during {remote}; it is left partly written")
                    dst.write(chunk)
                    self.done += len(chunk)
        if self.metadata is not None:
            for source in self.sources:
                text = json.dumps({**self.metadata, "source": str(source)}, indent=2)
                write_text(self.storage, f"{self.dest}/{source.name}{SIDECAR_SUFFIX}", text)
        self.current = ""
