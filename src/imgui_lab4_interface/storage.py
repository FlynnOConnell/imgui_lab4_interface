"""
BioHPC storage, or a machine in Settings.machines, over a mounted share or ssh, and Transfer, which copies local
files to BioHPC.

Paths are always the storage's own: BioHPC paths (/biohpc/data2/...), the ones Lab4 stores, or the machine's.
Nothing here deletes, moves or renames; open_write, open_tree and mkdir are the only writes.
"""

import io
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple, Optional, Protocol

from imgui_lab4_interface.config import Settings

SIDECAR_SUFFIX = ".lab4.json"
# a vnoiser run is a zarr store holding its figures and tables, so walk enters it
SUFFIX_VNOISER = ".vnoiser.zarr"
SIZE_CHUNK = 1 << 20
TIMEOUT_SSH = 15
TIMEOUT_WALK = 1800
FILES_PER_CAT = 200
# how long a shared ssh connection outlives its last command
SECONDS_SHARED = 60
# the longest unix socket path macOS takes (Linux takes 107)
LENGTH_SOCKET = 103
LINES_ERROR = 3
FLAGS_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def is_pruned(name: str) -> bool:
    """Whether walk lists a folder without entering it."""
    return name.startswith(".") or (
        name.endswith(".zarr") and not name.endswith(SUFFIX_VNOISER)
    )


def options_shared() -> list[str]:
    """
    ssh options that share one connection per host, so a command after the first costs no new login. None on Windows,
    whose ssh cannot, or when the socket cannot be made: ~/.ssh is missing, or the path is too long, which ssh treats
    as fatal.
    """
    folder = str(Path.home() / ".ssh")
    path = f"{folder}/lab4-%C"
    # %C expands to 40 characters, and ssh binds the socket as <path>.<16 random characters> first
    too_long = len(path) - len("%C") + 40 + 17 > LENGTH_SOCKET
    if (
        sys.platform == "win32"
        or not os.path.isdir(folder)
        or too_long
        or any(c in folder for c in " %")
    ):
        return []
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={path}",
        "-o",
        f"ControlPersist={SECONDS_SHARED}",
    ]


def ssh_error(text: str) -> str:
    """The first lines of ssh's stderr, without OpenSSH's ** notices, e.g. that the server lacks post-quantum keys."""
    lines = [
        line
        for line in text.strip().splitlines()
        if line.strip() and not line.startswith("**")
    ]
    more = len(lines) - LINES_ERROR
    return "\n".join(
        lines[:LINES_ERROR] + ([f"... and {more} more"] if more > 0 else [])
    )


class Entry(NamedTuple):
    name: str
    is_dir: bool
    size: int


class Record(NamedTuple):
    """A file or folder found by walk; hidden and .zarr folders other than vnoiser runs are listed but not entered."""

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

    def read_texts(self, paths: list[str]) -> dict[str, str]:
        """The text of each path that can be read; one round trip over ssh."""

    def mkdir(self, path: str) -> None:
        """Make the folder if missing; its parent must exist."""

    def open_read(self, path: str) -> BinaryIO:
        """Open path for reading."""

    def open_write(self, path: str) -> BinaryIO:
        """Open path for writing, making its parent folders."""

    def open_tree(self, dest: str) -> "FileByFile | SshTar":
        """Write files under the folder dest, in order, with add(relative path, size, source)."""

    def walk(self, root: str) -> Iterator[Record]:
        """Everything under root, root itself excluded."""

    def local_path(self, path: str) -> Optional[Path]:
        """path on this machine, or None when it is not a local file."""


class MountedStorage:
    """
    BioHPC through a local mount, e.g. X: mapped to /biohpc.

    Args:
        root (str): The local mount folder
        base (str): The BioHPC path root maps to
        label (str): The catalog's name for this storage; root and base when not given
    """

    def __init__(self, root: str, base: str = "/", label: Optional[str] = None):
        self.root = Path(root)
        self.base = "/" + base.strip("/")
        self.label = label or (
            f"{root} ({self.base})" if str(root) else "no mount root"
        )

    def local(self, path: str) -> Path:
        if str(self.root) == ".":
            raise OSError("no mount root set; set one in Settings > Connection")
        relative = posixpath.relpath(posixpath.normpath(path), self.base)
        if relative == ".." or relative.startswith("../"):
            raise OSError(f"{path} is outside {self.base}")
        return self.root if relative == "." else self.root / relative

    def check(self) -> None:
        self.local(self.base)
        if not self.root.is_dir():
            raise OSError(f"{self.root} is not reachable")

    def exists(self, path: str) -> bool:
        return self.local(path).exists()

    def listdir(self, path: str) -> list[Entry]:
        entries = [
            Entry(p.name, p.is_dir(), 0 if p.is_dir() else p.stat().st_size)
            for p in self.local(path).iterdir()
        ]
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def read_text(self, path: str) -> str:
        return self.local(path).read_text()

    def read_texts(self, paths: list[str]) -> dict[str, str]:
        texts = {}
        for path in paths:
            try:
                texts[path] = self.local(path).read_text(errors="replace")
            except OSError:
                pass
        return texts

    def mkdir(self, path: str) -> None:
        self.local(path).mkdir(exist_ok=True)

    def open_read(self, path: str) -> BinaryIO:
        return self.local(path).open("rb")

    def open_write(self, path: str) -> BinaryIO:
        target = self.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.open("wb")

    def open_tree(self, dest: str) -> "FileByFile":
        return FileByFile(self, dest)

    def walk(self, root: str) -> Iterator[Record]:
        self.check()
        top = self.local(root)
        if not top.is_dir():
            raise OSError(f"{root} not found under {self.root}")
        stack = [(top, root.rstrip("/"))]
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
                if is_dir and not is_pruned(entry.name):
                    stack.append((entry.path, path))

    def local_path(self, path: str) -> Optional[Path]:
        return self.local(path)


class SshReader:
    """Reads a remote file through ssh `cat`; close raises if it failed."""

    def __init__(self, base: list[str], path: str):
        self.path = path
        self.proc = subprocess.Popen(
            [*base, f"cat {shlex.quote(path)}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=FLAGS_NO_WINDOW,
        )

    def read(self, n: int) -> bytes:
        return self.proc.stdout.read(n)

    def close(self) -> None:
        self.proc.stdout.close()
        code = self.proc.wait()
        if code != 0:
            raise OSError(
                ssh_error(self.proc.stderr.read().decode(errors="replace"))
                or f"reading {self.path} failed"
            )

    def __enter__(self) -> "SshReader":
        return self

    def __exit__(self, kind, *exc) -> None:
        # on an error, stop ssh instead of raising its broken-pipe failure over the error
        if kind is None:
            self.close()
            return
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


class SshWriter:
    """Streams into a remote command, `cat > path` by default; close raises if it failed."""

    def __init__(self, base: list[str], path: str, command: Optional[str] = None):
        command = (
            command
            or f"mkdir -p {shlex.quote(posixpath.dirname(path))} && cat > {shlex.quote(path)}"
        )
        self.path = path
        # a file, not a pipe: tar reports an error per file, and a full pipe would stall ssh mid-write
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [*base, command],
            stdin=subprocess.PIPE,
            stderr=self.errors,
            creationflags=FLAGS_NO_WINDOW,
        )

    def write(self, chunk: bytes) -> None:
        self.proc.stdin.write(chunk)

    def wait(self) -> Optional[str]:
        """Close the stream and wait for ssh; why it failed, or None."""
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            # the remote side stopped reading; its exit code says whether that was a failure
            pass
        code = self.proc.wait()
        self.errors.seek(0)
        text = ssh_error(self.errors.read().decode(errors="replace"))
        self.errors.close()
        return None if code == 0 else text or f"writing {self.path} failed"

    def close(self) -> None:
        failed = self.wait()
        if failed is not None:
            raise OSError(failed)

    def __enter__(self) -> "SshWriter":
        return self

    def __exit__(self, kind, error, trace) -> None:
        if kind is None:
            self.close()
            return
        # the error that stopped the write stands, unless it is the broken pipe of the remote side failing
        failed = self.wait()
        if isinstance(error, BrokenPipeError) and failed is not None:
            raise OSError(failed) from error


class FileByFile:
    """Writes files under a storage folder with one open_write each."""

    def __init__(self, storage: Storage, dest: str):
        self.storage = storage
        self.dest = dest

    def add(self, relative: str, size: int, src: BinaryIO) -> None:
        with self.storage.open_write(f"{self.dest}/{relative}") as dst:
            while chunk := src.read(SIZE_CHUNK):
                dst.write(chunk)

    def __enter__(self) -> "FileByFile":
        return self

    def __exit__(self, *exc) -> None:
        pass


class SshTar:
    """
    Writes files under a remote folder as one tar through one ssh, unpacked as it arrives, instead of a login per
    file. Only files are sent, so tar leaves existing folders' permissions alone; -m gives each file the time it lands,
    as cat would.
    """

    def __init__(self, base: list[str], dest: str):
        self.writer = SshWriter(
            base, dest, command=f"cd {shlex.quote(dest)} && tar -xmf -"
        )
        self.tar = tarfile.open(fileobj=self.writer, mode="w|")

    def add(self, relative: str, size: int, src: BinaryIO) -> None:
        info = tarfile.TarInfo(relative)
        # the remote umask applies, as it does to cat
        info.size, info.mode = size, 0o666
        self.tar.addfile(info, src)

    def __enter__(self) -> "SshTar":
        return self

    def __exit__(self, kind, error, trace) -> None:
        # tarfile writes the end of the archive only when nothing failed, so a cut file is never padded out as whole
        try:
            self.tar.__exit__(kind, error, trace)
        finally:
            self.writer.__exit__(kind, error, trace)


class SshStorage:
    """
    BioHPC over ssh. BatchMode never prompts, so a missing key fails fast. Commands share one connection per host
    where ssh can (options_shared), and keepalives end a dead one within a minute instead of hanging.
    """

    def __init__(self, host: str, label: Optional[str] = None):
        self.host = host
        self.label = label or f"ssh {host}"
        self.base = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={TIMEOUT_SSH}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ServerAliveInterval={TIMEOUT_SSH}",
            "-o",
            "ServerAliveCountMax=3",
            *options_shared(),
            host,
        ]

    def run(self, command: str, timeout: float = 4 * TIMEOUT_SSH) -> str:
        try:
            # remote names are utf-8; the Windows locale would misread them
            result = subprocess.run(
                [*self.base, command],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=FLAGS_NO_WINDOW,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise OSError(f"ssh {self.host}: {e}") from e
        if result.returncode != 0:
            raise OSError(
                ssh_error(result.stderr)
                or f"ssh {self.host} exited {result.returncode}"
            )
        return result.stdout

    def check(self) -> None:
        self.run("true")

    def exists(self, path: str) -> bool:
        return (
            self.run(f"test -e {shlex.quote(path)} && echo y || echo n").strip() == "y"
        )

    def listdir(self, path: str) -> list[Entry]:
        out = self.run(
            f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 -printf '%y\\t%s\\t%f\\n'"
        )
        entries = []
        for line in out.splitlines():
            kind, size, name = line.split("\t", 2)
            entries.append(Entry(name, kind == "d", 0 if kind == "d" else int(size)))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def read_text(self, path: str) -> str:
        return self.run(f"cat {shlex.quote(path)}")

    def read_texts(self, paths: list[str]) -> dict[str, str]:
        texts = {}
        for start in range(0, len(paths), FILES_PER_CAT):
            quoted = " ".join(
                shlex.quote(path) for path in paths[start : start + FILES_PER_CAT]
            )
            out = self.run(
                f'for f in {quoted}; do test -r "$f" && printf \'\\036%s\\037\' "$f" && cat -- "$f"; done; true',
                timeout=TIMEOUT_WALK,
            )
            for chunk in out.split("\x1e")[1:]:
                path, _, text = chunk.partition("\x1f")
                texts[path] = text
        return texts

    def mkdir(self, path: str) -> None:
        self.run(f"test -d {shlex.quote(path)} || mkdir {shlex.quote(path)}")

    def open_read(self, path: str) -> SshReader:
        return SshReader(self.base, path)

    def open_write(self, path: str) -> SshWriter:
        return SshWriter(self.base, path)

    def open_tree(self, dest: str) -> SshTar:
        return SshTar(self.base, dest)

    def walk(self, root: str) -> Iterator[Record]:
        fmt = r"'%y\t%s\t%T@\t%p\n'"
        prune = rf"\( -type d \( -name '.*' -o \( -name '*.zarr' ! -name '*{SUFFIX_VNOISER}' \) \)"
        out = self.run(
            f"find {shlex.quote(root)} -mindepth 1 {prune} -printf {fmt} -prune \\) -o -printf {fmt}",
            timeout=TIMEOUT_WALK,
        )
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                kind, size, mtime, path = parts
                yield Record(
                    path, kind == "d", 0 if kind == "d" else int(size), float(mtime)
                )

    def local_path(self, path: str) -> Optional[Path]:
        return None


class Mount(NamedTuple):
    """A local mount folder and its remote share."""

    root: str
    remote: str


def shows(remote: str, base: str) -> bool:
    """Whether a remote share names base, e.g. \\\\sshfs.kr\\me@host!2216\\biohpc or me@host:/biohpc for /biohpc."""
    return (
        remote.replace("\\", "/")
        .rstrip("/")
        .lower()
        .endswith("/" + base.strip("/").lower())
    )


def mapped_drives() -> list[Mount]:
    """Mapped drive letters and their shares for this Windows logon."""
    import ctypes
    import string

    mpr = ctypes.WinDLL("mpr")
    letters = ctypes.windll.kernel32.GetLogicalDrives()
    found = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not letters >> i & 1:
            continue
        buffer, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        # 1201: mapped but disconnected; the share is still named
        if mpr.WNetGetConnectionW(f"{letter}:", buffer, ctypes.byref(size)) in (
            0,
            1201,
        ):
            found.append(Mount(f"{letter}:\\", buffer.value))
    return found


def mounted_shares() -> list[Mount]:
    """Mounts listed by `mount` (Linux, macOS): `source on target type ...` or `source on target (...)`."""
    try:
        out = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=TIMEOUT_SSH
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        source, _, rest = line.partition(" on ")
        target = rest.split(" type ")[0].split(" (")[0]
        if source and target:
            found.append(Mount(target, source))
    return found


def find_mounts(base: str = "/biohpc") -> list[Mount]:
    """Local folders whose remote share is base."""
    mounts = mapped_drives() if sys.platform == "win32" else mounted_shares()
    return [m for m in mounts if shows(m.remote, base)]


def open_storage(settings: Settings) -> Storage:
    if settings.backend == "ssh":
        return SshStorage(settings.ssh_host)
    return MountedStorage(settings.mount_root, settings.mount_base)


def open_machine(machine: dict) -> Storage:
    """Storage for one of Settings.machines, labelled with its name. A local machine sees this computer's home drive."""
    if machine["reach"] == "ssh":
        return SshStorage(machine["host"], label=machine["name"])
    if machine["reach"] == "mounted":
        return MountedStorage(
            os.path.expanduser(machine["mount_root"]),
            machine["mount_base"],
            label=machine["name"],
        )
    return MountedStorage(Path.home().anchor, "/", label=machine["name"])


def machine_roots(machine: dict) -> list[str]:
    """The machine's roots in its storage's paths, blank lines dropped."""
    roots = [root.strip() for root in machine["roots"] if root.strip()]
    if machine["reach"] != "local":
        return roots
    return [
        "/" + Path(os.path.expanduser(root)).relative_to(Path.home().anchor).as_posix()
        for root in roots
    ]


def destination_state(storage: Storage, path: str) -> tuple[str, str]:
    """(path, "exists") or (path, "new"); raises OSError when the parent is missing or path is a file."""
    parent, name = posixpath.split(path.rstrip("/"))
    parent = parent or "/"
    if not storage.exists(parent):
        raise OSError(
            f"{parent} does not exist; a transfer makes at most one new folder"
        )
    for entry in storage.listdir(parent):
        if entry.name == name:
            if not entry.is_dir:
                raise OSError(f"{path} is a file")
            return path, "exists"
    return path, "new"


def download(storage: Storage, path: str, local: Path, progress: dict) -> Path:
    """Copy path to local through a .part file, counting bytes in progress["done"]; a failed copy leaves nothing."""
    local.parent.mkdir(parents=True, exist_ok=True)
    part = local.with_name(local.name + ".part")
    try:
        with storage.open_read(path) as src, part.open("wb") as dst:
            while chunk := src.read(SIZE_CHUNK):
                if progress.get("cancelled"):
                    raise InterruptedError(
                        f"copy of {posixpath.basename(path)} cancelled"
                    )
                dst.write(chunk)
                progress["done"] += len(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, local)
    return local


def write_text(storage: Storage, path: str, text: str) -> None:
    with storage.open_write(path) as f:
        f.write(text.encode())


class Transfer:
    """
    Copies local files and folders into dest under their own names, with a sidecar beside each when metadata is
    given. Run it on a worker; the window reads its counters.

    Args:
        dest (str): BioHPC folder, made when missing; its parent must exist
        metadata (dict | None): Written as <name>.lab4.json beside each source
        overwrite (bool): Replace existing files instead of stopping
    """

    def __init__(
        self,
        storage: Storage,
        sources: list[Path],
        dest: str,
        metadata: Optional[dict],
        overwrite: bool,
    ):
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
        """Copy every file, then the sidecars, through one open_tree: over ssh, one login and one tar."""
        names_taken = set()
        if not self.overwrite and self.storage.exists(self.dest):
            names_taken = {entry.name for entry in self.storage.listdir(self.dest)}
        plan = []
        for source in self.sources:
            target = f"{self.dest}/{source.name}"
            if source.name in names_taken:
                raise FileExistsError(
                    f"{target} already exists; tick overwrite to replace it"
                )
            if (
                self.metadata is not None
                and f"{source.name}{SIDECAR_SUFFIX}" in names_taken
            ):
                raise FileExistsError(
                    f"{target}{SIDECAR_SUFFIX} already exists; tick overwrite to replace it"
                )
            if source.is_dir():
                plan += [
                    (p, f"{source.name}/{p.relative_to(source).as_posix()}")
                    for p in sorted(source.rglob("*"))
                    if p.is_file()
                ]
            else:
                plan.append((source, source.name))
        self.total = sum(p.stat().st_size for p, _ in plan)
        self.storage.mkdir(self.dest)
        with self.storage.open_tree(self.dest) as tree:
            for local, relative in plan:
                self.current = f"{self.dest}/{relative}"
                with local.open("rb") as src:
                    tree.add(
                        relative, local.stat().st_size, Counted(transfer=self, src=src)
                    )
            if self.metadata is not None:
                for source in self.sources:
                    text = json.dumps(
                        {**self.metadata, "source": str(source)}, indent=2
                    ).encode()
                    tree.add(
                        f"{source.name}{SIDECAR_SUFFIX}", len(text), io.BytesIO(text)
                    )
        self.current = ""


class Counted:
    """A Transfer's source file: what is read adds to its done, and reading stops once it is cancelled."""

    def __init__(self, transfer: Transfer, src: BinaryIO):
        self.transfer = transfer
        self.src = src

    def read(self, n: int = -1) -> bytes:
        if self.transfer.cancelled:
            raise InterruptedError(f"cancelled; {self.transfer.current} is incomplete")
        chunk = self.src.read(n)
        self.transfer.done += len(chunk)
        return chunk
