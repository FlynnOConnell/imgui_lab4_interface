"""The `lab4` window: a sign-in card, then a page rail. Storage calls run on worker threads."""

import copy
import json
import os
import posixpath
import shutil
import subprocess
import sys
import webbrowser
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import imgui_data_loader as idl
from imgui_bundle import hello_imgui, imgui, imgui_ctx, immapp
from imgui_bundle import icons_fontawesome_6 as fa
from imgui_bundle import portable_file_dialogs as pfd

from imgui_lab4_interface import __version__, lab4, processing, viewers
from imgui_lab4_interface.catalog import KINDS, Catalog, classify_file
from imgui_lab4_interface.config import BACKENDS, Settings, data_dir
from imgui_lab4_interface.database import BIOHPC, Credentials, Lab4Database, Schema, secrets_path
from imgui_lab4_interface.processing import Asap7Params, Job, MescUnit
from imgui_lab4_interface.slices import ADMIN, COLUMNS, PEOPLE, ROOT, SliceDatabase, can_edit, file_dates, locked, match_files
from imgui_lab4_interface.storage import SIDECAR_SUFFIX, Entry, Mount, SshStorage, Transfer, destination_state, download, find_mounts, open_storage, write_text

ACCENT = imgui.ImVec4(0.40, 0.68, 1.00, 1.0)
KEY = imgui.ImVec4(1.00, 0.80, 0.20, 1.0)
DIM = imgui.ImVec4(0.62, 0.62, 0.65, 1.0)
TEXT = imgui.ImVec4(0.92, 0.92, 0.94, 1.0)
CARD = imgui.ImVec4(0.17, 0.18, 0.21, 1.0)
CARD_HOVER = imgui.ImVec4(0.21, 0.22, 0.26, 1.0)
EDGE = imgui.ImVec4(0.35, 0.35, 0.37, 1.0)
OK = imgui.ImVec4(0.40, 0.90, 0.40, 1.0)
WARN = imgui.ImVec4(1.0, 0.75, 0.3, 1.0)
ERROR = imgui.ImVec4(1.0, 0.4, 0.4, 1.0)
PATH = imgui.ImVec4(0.55, 0.85, 0.55, 1.0)
CODE = imgui.ImVec4(0.75, 0.85, 0.65, 1.0)
LIT = imgui.ImVec4(ACCENT.x, ACCENT.y, ACCENT.z, 0.22)
CLEAR = imgui.ImVec4(0, 0, 0, 0)
COLORS_GO = (imgui.ImVec4(0.13, 0.55, 0.13, 1.0), imgui.ImVec4(0.18, 0.65, 0.18, 1.0), imgui.ImVec4(0.10, 0.45, 0.10, 1.0))
THEME = idl.Theme.dark().replace(
    accent=(ACCENT.x, ACCENT.y, ACCENT.z, 1.0),
    accent_hover=(0.50, 0.75, 1.00, 1.0),
    accent_active=(0.32, 0.60, 0.95, 1.0),
    text=(TEXT.x, TEXT.y, TEXT.z, 1.0),
    text_dim=(DIM.x, DIM.y, DIM.z, 1.0),
    border=(EDGE.x, EDGE.y, EDGE.z, 0.8),
    button_face=(CARD.x, CARD.y, CARD.z, 1.0),
    button_face_hover=(CARD_HOVER.x, CARD_HOVER.y, CARD_HOVER.z, 1.0),
)

TITLE_WINDOW = "Lab4 · BioHPC"
SIZE_WINDOW = (1040, 720)
WIDTH_RAIL_EM = 13
WIDTH_CAPTION_EM = 9
WIDTH_LOGIN_EM = 22
TITLE_PICKER = "Choose destination"
NUMBER_JOBS_SHOWN = 8
STATES_FAILED = ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED")

# rail pages: key, icon, name, subtitle
PAGES = (
    ("home", fa.ICON_FA_HOUSE, "Overview", "what this does and where data goes"),
    ("transfer", fa.ICON_FA_CLOUD_ARROW_UP, "Transfer", "copy data to BioHPC with its metadata"),
    ("browse", fa.ICON_FA_SITEMAP, "Browse storage", "browse BioHPC, open datasets, add metadata"),
    ("slices", fa.ICON_FA_TABLE, "Human slices", "track human slices from collection to assay (demo data)"),
    ("settings", fa.ICON_FA_GEAR, "Settings", "connection, destinations, viewers and processing"),
)
# overview links: icon, name, url, tooltip
LINKS = (
    (fa.ICON_FA_BOOK, "Lab docs", "https://doc.memorylongevity.org/", "lab documentation"),
    (fa.ICON_FA_FLASK, "Lab4 docs", "https://lab4.memorylongevity.org/", "lab4 package docs"),
    (fa.ICON_FA_SERVER, "Lab4 workspace", "http://server.memorylongevity.org", "sign in with GitLab and download your credentials"),
)
# slice table colors for outcome and incubator
COLORS_SLICE = {"ok": OK, "failed": ERROR, "in incubator": ACCENT, "out, recorded": OK, "out, imaged": CODE, "out, fixed": KEY}
# file icons by extension
ICONS_FILE = {
    ".mesc": fa.ICON_FA_FILE_IMAGE, ".tif": fa.ICON_FA_FILE_IMAGE, ".tiff": fa.ICON_FA_FILE_IMAGE, ".png": fa.ICON_FA_FILE_IMAGE,
    ".avi": fa.ICON_FA_FILE_VIDEO, ".mp4": fa.ICON_FA_FILE_VIDEO, ".pdf": fa.ICON_FA_FILE_PDF,
    ".pxp": fa.ICON_FA_CHART_LINE, ".nwb": fa.ICON_FA_CHART_LINE,
}
# files a transfer writes
FILES = (
    ("<name>", "the file or folder, copied as is"),
    (f"<name>{SIDECAR_SUFFIX}", "its metadata: Lab4 key, notes and extra fields"),
)


def draw_wrapped(text: str, color: Optional[imgui.ImVec4] = None) -> None:
    """Wrapped text, colored when color is given."""
    if color is None:
        imgui.text_wrapped(text)
    else:
        idl.text_wrapped_colored(color, text)


def draw_hint(text: str) -> None:
    """A (?) after the last item that shows text on hover."""
    imgui.same_line()
    imgui.text_disabled("(?)")
    if imgui.is_item_hovered():
        idl.wrapped_tooltip(text)


def draw_cards(cards: list[tuple[str, str, str]]) -> Optional[int]:
    """A full-width row of (icon, name, tooltip) cards; returns the index clicked, or None."""
    em = imgui.get_font_size()
    gap = 1.0 * em
    width = (imgui.get_content_region_avail().x - gap * (len(cards) - 1)) / len(cards)
    clicked = None
    for i, (icon, name, tooltip) in enumerate(cards):
        if i:
            imgui.same_line(0, gap)
        if idl.icon_button(icon, name, imgui.ImVec2(width, 3.2 * em), theme=THEME, tooltip=tooltip):
            clicked = i
    return clicked


def read_mesc(sources: list[Path]) -> list[tuple[str, str, list[MescUnit] | str]]:
    """(local path, destination path, units or error) for each .mesc in sources."""
    found = []
    for local, relative in processing.find_mesc(sources):
        try:
            found.append((str(local), relative, processing.list_units(local)))
        except OSError as e:
            found.append((str(local), relative, f"cannot read its units: {e}"))
    return found


def measure(sources: list[Path]) -> list[tuple[Path, int]]:
    """Each source with its size in bytes; a folder's is every file under it."""
    return [(s, sum(f.stat().st_size for f in s.rglob("*") if f.is_file()) if s.is_dir() else s.stat().st_size) for s in sources]


def is_recording_data(path: str) -> bool:
    """Whether path is an .h5, the file lab4.convert writes and Lab4 registers."""
    return path.lower().endswith((".h5", ".hdf5"))


def summarize(kinds: dict) -> str:
    """e.g. "3 tiff, 1 masknmf-run", most common first."""
    return ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items(), key=lambda item: -item[1]))


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else ""


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


class Lab4App:
    """The window's state. Settings are read from the data folder when not given."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings.load()
        self.storage = open_storage(self.settings)
        # the Lab4 user, "offline", or "" when signed out
        self.user = ""
        self.error_login = ""
        self.db: Optional[Lab4Database] = None
        self.schema: Optional[Schema] = None
        self.subject_recordings: list[dict] = []
        # local mounts of mount_base; None until detected
        self.mounts: Optional[list[Mount]] = None
        # whether the first mount detection has run; only it falls back to ssh
        self.detected = False
        self.page = "home"
        # None while checking, "" when reachable, else the error
        self.status: Optional[str] = None
        # footer message, and whether it is an error
        self.message = ""
        self.failed = False
        self.sources: list[str] = []
        self.dest = self.settings.destinations[0][0] if self.settings.destinations else ""
        # last destination checked and its state: "exists", "new", or an error
        self.dest_checked = ""
        self.dest_state: Optional[str] = None
        # destination picker state
        self.picker_path = ""
        self.picker_entries: list[Entry] = []
        self.picker_error = ""
        self.picker_selected = ""
        self.picker_open = False
        self.overwrite = False
        self.attach = True
        self.recording = lab4.Recording()
        self.transfer: Optional[Transfer] = None
        # processing: on or off, the sources the .mesc list was read for, and each .mesc's units or error
        self.process = False
        self.params = Asap7Params()
        self.mesc_for: Optional[tuple] = None
        self.mesc: list[tuple[str, str, list[MescUnit] | str]] = []
        self.chosen: set[tuple[str, str]] = set()
        # jobs to submit once the transfer lands, jobs submitted, and their states
        self.pending: list[Job] = []
        self.pending_params = Asap7Params()
        self.jobs = processing.load_jobs()
        self.job_states: dict[str, str] = {}
        self.path_browse = self.settings.destinations[0][0] if self.settings.destinations else "/"
        self.entries: list[Entry] = []
        self.error_browse = ""
        self.selected = ""
        self.sidecar = ""
        self.catalog = Catalog()
        # catalog info for the browse folder
        self.described: dict = {"folder": None, "scan": None, "items": {}}
        self.count_scanned = 0
        self.slices = SliceDatabase()
        if self.slices.moved_aside is not None:
            self.message = f"slice table schema changed; old file saved as {self.slices.moved_aside}"
        self.tab_slices = "slices"
        # who edits slice rows; the demo picks a user instead of signing in
        self.user_slices = ADMIN
        # tab to switch to next frame (Show its slices)
        self.tab_request: Optional[str] = None
        self.rows_slices = {table: self.slices.rows(table) for table in COLUMNS}
        self.filter_slices = {table: "" for table in COLUMNS}
        # sort columns per table, primary first
        self.sort_slices: dict[str, list[tuple[str, bool]]] = {table: [] for table in COLUMNS}
        self.order_slices: dict[str, list[dict]] = {table: [] for table in COLUMNS}
        self.picked: dict[str, set[int]] = {table: set() for table in COLUMNS}
        self.anchor: dict[str, Optional[int]] = {table: None for table in COLUMNS}
        # cell being edited (table, row id, column), its text, and whether to focus it
        self.editing: Optional[tuple[str, int, str]] = None
        self.text_edit = ""
        self.focus_edit = False
        self.open_choice = False
        # row to scroll to, e.g. a new or copied one
        self.scroll_slices: Optional[int] = None
        # file index under ROOT, when it was taken, and the last indexing error
        self.files_index, self.indexed = self.slices.files()
        self.error_index = ""
        # the storage label the running index was started for
        self.index_for = ""
        # bumped when slice rows or the file index change, invalidating cache_files: (key, files_of's result)
        self.version_rows = 0
        self.cache_files: tuple = (None, None)
        # file being downloaded to open: paths, kind, bytes done and total
        self.fetch: Optional[dict] = None
        self.saver = None
        # the table the open save dialog exports
        self.table_export = ""
        # confirm dialog: title, lines, button, and the call it runs
        self.confirm: Optional[dict] = None
        for table in COLUMNS:
            self.order_rows(table=table)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.futures: dict[str, Future] = {}
        self.picker = None
        self.store = idl.JsonPreferenceStore(path=str(data_dir() / "recent.json"))
        self.config = idl.FileDialogConfig(
            title="Lab4",
            buttons=[],
            theme=THEME,
            header_draw=self.draw_header,
            body_draw=self.draw_body,
            footer_draw=self.draw_footer,
            show_options_button=False,
            close_on_select=False,
            quit_on_escape=False,
            on_cancel=self.quit,
        )
        self.dialog = idl.FileDialog(self.config)
        self.detect_mounts()

    def detect_mounts(self) -> None:
        self.mounts = None
        self.submit("mounts", find_mounts, self.settings.mount_base)

    def use_mount(self, mount: Mount) -> None:
        self.settings.backend = "mounted"
        self.settings.mount_root = mount.root
        if self.user:
            self.connect()

    def sign_in(self) -> None:
        """Sign in to Lab4 on a worker thread."""
        self.error_login = ""
        try:
            self.db = Lab4Database(Credentials.load(secrets_path()))
        except (OSError, ValueError, KeyError) as e:
            self.error_login = f"cannot read {secrets_path()}: {e}"
            return
        self.submit("sign_in", self.db.schema)

    def work_offline(self) -> None:
        """Skip Lab4. Transfers and sidecars still work; registering does not."""
        self.db = None
        self.schema = None
        self.user = "offline"
        self.connect()

    def sign_out(self) -> None:
        if self.transfer is not None:
            self.transfer.cancelled = True
        self.user = ""
        self.db = None
        self.schema = None
        self.page = "home"
        self.message = ""

    def register_problems(self, path: str) -> list[str]:
        """Why path cannot be registered yet."""
        found = []
        if self.schema is None:
            found.append("sign in to Lab4 to register")
        if "register" in self.futures:
            found.append("a registration is running")
        if not path.startswith(f"{BIOHPC}/"):
            found.append(f"Lab4 only stores paths under {BIOHPC}")
        if not is_recording_data(path):
            found.append("only .h5 files (from lab4.convert) can be registered")
        found += self.recording.problems()
        if self.schema is not None:
            found += self.schema.problems(self.recording)
        return found

    def register(self, path: str) -> None:
        self.message = ""
        self.submit("register", self.db.register, self.schema, copy.deepcopy(self.recording), path)

    def look_up_subject(self) -> None:
        """List the subject's recordings already in Lab4."""
        subject = self.recording.subject_id.strip()
        self.subject_recordings = []
        if self.db is not None and self.schema is not None and subject in self.schema.subjects:
            self.submit("subject", self.db.recordings, subject)

    def quit(self) -> None:
        if self.transfer is not None:
            self.transfer.cancelled = True
        hello_imgui.get_runner_params().app_shall_exit = True

    def submit(self, name: str, fn, *args) -> None:
        """Run fn on a worker; poll handles the result by name."""
        self.futures[name] = self.executor.submit(fn, *args)

    def connect(self) -> None:
        """Rebuild storage from settings, check it, and relist the browse folder."""
        self.storage = open_storage(self.settings)
        self.status = None
        self.submit("check", self.storage.check)
        self.browse(path=self.path_browse)
        self.dest_checked = ""
        self.index_files()

    def index_files(self) -> None:
        """Index the files under ROOT, unless an index is already running."""
        if "files_index" not in self.futures:
            self.index_for = self.storage.label
            self.submit("files_index", self.slices.index_files, self.storage)

    def browse(self, path: str) -> None:
        self.path_browse = path.rstrip("/") or "/"
        self.selected = ""
        self.sidecar = ""
        self.submit("list", self.storage.listdir, self.path_browse)

    def describe(self) -> None:
        self.submit("describe", self.catalog.describe, self.storage.label, self.path_browse)

    def scan(self) -> None:
        """Catalog the browse folder and everything under it."""
        self.count_scanned = 0
        self.submit("scan", self.catalog.scan, self.storage, self.path_browse, self.count_scan)

    def count_scan(self, n: int) -> None:
        self.count_scanned = n

    def open_dataset(self, path: str, kind: str) -> None:
        """Open path in the viewer for its kind."""
        local = self.storage.local_path(path)
        if local is None:
            self.message, self.failed = "cannot open over ssh; mount the share", True
            return
        self.open_local(local=local, kind=kind)

    def open_local(self, local: Path, kind: Optional[str]) -> None:
        """Open a local file in its viewer, or its default app when it has no kind."""
        self.failed = True
        try:
            if kind is not None:
                log = viewers.launch(viewers.command(self.settings, kind, local))
                self.message = f"opened {local.name} in {KINDS[kind][0]} (log: {log})"
            elif sys.platform == "win32":
                os.startfile(local)
                self.message = f"opened {local.name} in its default app"
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(local)])
                self.message = f"opened {local.name} in its default app"
        except OSError as e:
            self.message = str(e)
            return
        self.failed = False

    def open_file(self, path: str, size: int) -> None:
        """Open a file under ROOT: in place when mounted, else from a cached copy, after asking."""
        remote = f"{ROOT}/{path}"
        name = posixpath.basename(remote)
        kind = classify_file(name)
        try:
            local = self.storage.local_path(remote)
        except OSError as e:
            self.message, self.failed = str(e), True
            return
        if local is not None and not local.is_file():
            self.message, self.failed = f"{local} not found; is the share mounted?", True
            return
        if local is not None:
            self.open_local(local=local, kind=kind)
            return
        cached = data_dir() / "cache" / remote.lstrip("/")
        if cached.is_file() and cached.stat().st_size == size:
            self.open_local(local=cached, kind=kind)
            return
        lines = [
            f"{name}, {human_bytes(size)}",
            f"from {self.storage.label}: {remote}",
            f"to {cached}",
            f"then open it in {KINDS[kind][0] if kind else 'its default app'}. Nothing on BioHPC changes.",
        ]
        if cached.exists():
            lines.append("The cached copy is replaced.")
        self.confirm = {"title": "Copy to this machine?", "lines": lines, "button": "Copy and open", "run": self.start_fetch, "args": (remote, cached, kind, size)}

    def start_fetch(self, remote: str, cached: Path, kind: Optional[str], size: int) -> None:
        self.fetch = {"remote": remote, "local": cached, "kind": kind, "done": 0, "total": size}
        self.submit("download", download, self.storage, remote, cached, self.fetch)

    def send(self, transfer: Transfer, pending: list[Job], params: Asap7Params) -> None:
        self.transfer = transfer
        self.pending = pending
        self.pending_params = params
        self.submit("transfer", transfer.run)

    def delete_rows(self, table: str, ids: list[int]) -> None:
        try:
            self.slices.delete(table, ids, self.user_slices)
        except ValueError as e:
            self.message, self.failed = str(e), True
            return
        self.message, self.failed = f"deleted {len(ids)} row(s) from {table}", False
        self.picked[table].clear()
        self.reload_slices()

    def reset_slices(self) -> None:
        self.slices.reset()
        self.editing = None
        self.reload_slices()
        self.message, self.failed = "rows reset from the file names", False

    def order_rows(self, table: str) -> None:
        """
        Filter and sort a slice table's rows, unpicking rows the filter hides. column:value matches part of one
        column, column=value all of it; empty cells sort last.
        """
        names = [name for name, _, _ in COLUMNS[table]]
        rows = self.rows_slices[table]
        for term in self.filter_slices[table].lower().split():
            exact, _, whole = term.partition("=")
            column, _, value = term.partition(":")
            if whole and exact in names:
                rows = [r for r in rows if r[exact] is not None and str(r[exact]).lower() == whole]
            elif value and column in names:
                rows = [r for r in rows if r[column] is not None and value in str(r[column]).lower()]
            else:
                rows = [r for r in rows if any(r[n] is not None and term in str(r[n]).lower() for n in names)]
        for column, ascending in reversed(self.sort_slices[table]):
            filled = sorted((r for r in rows if r[column] is not None), key=lambda r: r[column], reverse=not ascending)
            rows = filled + [r for r in rows if r[column] is None]
        self.order_slices[table] = rows
        self.picked[table] &= {r["id"] for r in rows}

    def reload_slices(self) -> None:
        """Reread the slice tables, keeping filters, sorts and picks."""
        self.version_rows += 1
        for table in COLUMNS:
            self.rows_slices[table] = self.slices.rows(table)
            self.order_rows(table=table)

    def files_of(self, table: str, row: Optional[dict], slice_: Optional[dict]) -> tuple[list[dict], set[str], str]:
        """The picked row's files, the paths its patient's slices claim, and a heading; cached until anything changes."""
        key = (self.version_rows, table, None if row is None else row["id"])
        if self.cache_files[0] == key:
            return self.cache_files[1]
        claimed: set[str] = set()
        files: list[dict] = []
        heading = ""
        if slice_ is not None:
            files = match_files(self.files_index, slice_["file_match"] or "")
            heading = f"{len(files)} files match {slice_['file_match']}" if slice_["file_match"] else "no file_match set"
        elif row is not None and row["collection_date"]:
            for s in self.rows_slices["slices"]:
                if s["patient_id"] == row["patient_id"]:
                    claimed |= {f["path"] for f in match_files(self.files_index, s["file_match"] or "")}
            files = [f for f in self.files_index if row["collection_date"] in file_dates(f["path"])]
            heading = f"{len(files)} files dated {row['collection_date']}, {sum(f['path'] not in claimed for f in files)} matching no slice"
        self.cache_files = (key, (files, claimed, heading))
        return files, claimed, heading

    def start_edit(self, table: str, row: dict, column: int) -> None:
        """Start editing a double-clicked cell, or say in the footer why it cannot be edited."""
        name, kind, _ = COLUMNS[table][column]
        if kind == "joined":
            self.message, self.failed = f"{name} comes from the patient; edit it on Patients", True
        elif kind == "owner":
            self.message, self.failed = "owner cannot be changed", True
        elif not can_edit(row["owner"], self.user_slices):
            self.message, self.failed = f"this row is {row['owner']}'s; {locked(row['owner'])}", True
        else:
            self.editing = (table, row["id"], name)
            self.text_edit = "" if row[name] is None else str(row[name])
            self.focus_edit = True
            self.open_choice = kind in ("choice", "ref")

    def commit_edit(self, text: str) -> None:
        """Save the edited cell; errors show in the footer."""
        table, id_, column = self.editing
        self.editing = None
        try:
            self.slices.update(table, id_, column, text, self.user_slices)
        except ValueError as e:
            self.message, self.failed = str(e), True
            return
        self.message, self.failed = f"{column} set to {text.strip() or 'empty'}", False
        self.reload_slices()

    def select(self, name: str) -> None:
        self.selected = name
        self.sidecar = ""
        names = {e.name for e in self.entries}
        if f"{name}{SIDECAR_SUFFIX}" in names:
            self.submit("sidecar", self.storage.read_text, f"{self.path_browse.rstrip('/')}/{name}{SIDECAR_SUFFIX}")

    def poll(self) -> None:
        """Handle finished workers and file dialogs."""
        for name, future in list(self.futures.items()):
            if not future.done():
                continue
            del self.futures[name]
            error = future.exception()
            if name == "mounts":
                self.mounts = [] if error is not None else future.result()
                unset = self.settings.backend == "mounted" and not self.settings.mount_root.strip()
                if unset and len(self.mounts) == 1:
                    self.use_mount(self.mounts[0])
                elif unset and not self.mounts and not self.detected:
                    # nothing is mounted here, so reach BioHPC over ssh
                    self.settings.backend = "ssh"
                    if self.user:
                        self.connect()
                self.detected = True
            elif name == "sign_in":
                if error is None:
                    self.schema = future.result()
                    self.user = self.schema.user
                    self.connect()
                else:
                    self.db = None
                    self.error_login = f"sign-in failed: {error}"
            elif name == "refresh" and error is None:
                self.schema = future.result()
                self.look_up_subject()
            elif name == "subject":
                self.subject_recordings = [] if error is not None else future.result()
            elif name == "register":
                if error is None:
                    added = [table.replace("_", " ") for table, new in future.result().items() if new and table != "recording"]
                    self.message = f"registered {self.recording.label()} in Lab4" + (f", with a new {', '.join(added)}" if added else "")
                    self.submit("refresh", self.db.schema)
                else:
                    self.message = f"registering failed: {error}"
                self.failed = error is not None
            elif name == "check":
                self.status = "" if error is None else str(error)
            elif name == "list":
                self.entries = [] if error is not None else future.result()
                self.error_browse = "" if error is None else str(error)
                if self.selected:
                    self.select(name=self.selected)
                self.describe()
            elif name == "describe" and error is None:
                self.described = future.result()
            elif name == "scan":
                found = None if error is not None else future.result()
                self.message = f"scan failed: {error}" if found is None else f"scanned {found['root']}: {found['datasets']} datasets in {found['records']} items ({found['seconds']:.1f} s)"
                self.failed = found is None
                self.describe()
            elif name == "files_index":
                if error is None:
                    self.files_index, self.indexed = future.result()
                    self.version_rows += 1
                self.error_index = "" if error is None else str(error)
                if self.index_for != self.storage.label:
                    self.index_files()
            elif name == "sizes":
                if error is None:
                    self.confirm_send(future.result())
                else:
                    self.message, self.failed = f"cannot read the sources: {error}", True
            elif name == "download":
                if error is None:
                    self.open_local(local=self.fetch["local"], kind=self.fetch["kind"])
                else:
                    self.message, self.failed = f"copy failed: {error}", True
                self.fetch = None
            elif name == "sidecar":
                self.sidecar = "" if error is not None else future.result()
            elif name == "attach":
                self.message = f"metadata attached to {self.selected}" if error is None else f"attach failed: {error}"
                self.failed = error is not None
                self.submit("list", self.storage.listdir, self.path_browse)
            elif name == "transfer":
                self.message = f"sent {len(self.transfer.sources)} item(s) to {self.transfer.dest}" if error is None else str(error)
                self.failed = error is not None
                self.dest_checked = ""
                if error is None:
                    self.transfer = None
                    self.browse(path=self.path_browse)
                if error is None and self.pending:
                    self.message += f"; submitting {len(self.pending)} processing job(s)"
                    self.submit("process", processing.submit, self.run_remote, copy.deepcopy(self.settings), self.pending, self.pending_params, self.storage)
                else:
                    self.pending = []
            elif name == "process":
                self.jobs = processing.load_jobs()
                self.failed = error is not None
                if error is None:
                    ids = [job["job_id"] for job in future.result()]
                    self.message = f"submitted job(s) {', '.join(ids)} on {self.settings.partition}"
                else:
                    waiting = sum(job["job_id"] is None for job in self.jobs)
                    self.message = f"{waiting} job(s) not submitted ({error}); retry with Submit under step 4"
                self.pending = []
            elif name == "mesc":
                self.mesc = [] if error is not None else future.result()
                keys = {(local, unit.key) for local, _, units in self.mesc if isinstance(units, list) for unit in units}
                self.chosen &= keys
            elif name == "states":
                self.job_states = {} if error is not None else future.result()
                self.message = f"job states: {error}" if error is not None else ""
                self.failed = error is not None
            elif name == "dest_check":
                _, state = (None, str(error)) if error is not None else future.result()
                if self.dest_checked == self.destination():
                    self.dest_state = state
            elif name == "picker":
                self.picker_entries = [] if error is not None else [e for e in future.result() if e.is_dir]
                self.picker_error = "" if error is None else str(error)
        if self.saver is not None and self.saver.ready():
            chosen = self.saver.result()
            self.saver = None
            table = self.table_export
            if chosen:
                try:
                    self.slices.export(table, Path(chosen))
                except OSError as e:
                    self.message, self.failed = str(e), True
                else:
                    self.message, self.failed = f"wrote {len(self.rows_slices[table])} {table} to {chosen}", False
        if self.picker is not None and self.picker.ready():
            chosen = self.picker.result()
            chosen = chosen if isinstance(chosen, list) else [chosen]
            chosen = [c for c in chosen if c and c not in self.sources]
            self.sources += chosen
            if chosen:
                self.store.record_selection(idl.DialogResult(paths=chosen))
            self.picker = None

    def run_remote(self, command: str) -> str:
        """Run a shell command on the processing ssh host."""
        return SshStorage(self.settings.processing_host).run(command)

    def read_mesc(self) -> None:
        """Read the .mesc units in the sources, once per list of sources."""
        sources = tuple(self.sources)
        if sources != self.mesc_for and "mesc" not in self.futures:
            self.mesc_for = sources
            self.submit("mesc", read_mesc, [Path(s) for s in sources])

    def jobs_to_send(self) -> list[Job]:
        """One Job per chosen unit."""
        dest = self.destination()
        when = datetime.now()
        jobs = []
        for local, relative, units in self.mesc:
            if not isinstance(units, list):
                continue
            mesc = f"{dest}/{relative}"
            jobs += [Job(local, mesc, unit.key, processing.dir_save(mesc, unit.key, when)) for unit in units if (local, unit.key) in self.chosen]
        return jobs

    def submit_waiting(self) -> None:
        """Submit jobs that failed to submit earlier."""
        waiting = [job for job in self.jobs if job["job_id"] is None]
        if waiting and "process" not in self.futures:
            self.message = ""
            self.submit("process", processing.submit_waiting, self.run_remote, copy.deepcopy(self.settings), waiting, self.storage)

    def check_jobs(self) -> None:
        ids = [job["job_id"] for job in self.jobs[-NUMBER_JOBS_SHOWN:]]
        if ids and "states" not in self.futures:
            self.submit("states", processing.job_states, self.run_remote, ids)

    def open_picker(self, folders: bool) -> None:
        if self.picker is not None:
            return
        start = self.store.default_dir() or str(Path.home())
        if folders:
            self.picker = pfd.select_folder("Add folder", start)
        else:
            self.picker = pfd.open_file("Add files", start, ["All Files", "*"], pfd.opt.multiselect)

    def destination(self) -> str:
        """The destination folder, without a trailing slash."""
        dest = self.dest.strip()
        return dest.rstrip("/") or dest

    def check_destination(self) -> None:
        """Check the destination once each time it changes."""
        dest = self.destination()
        if dest.startswith("/") and dest != self.dest_checked and "dest_check" not in self.futures:
            self.dest_checked = dest
            self.dest_state = None
            self.submit("dest_check", destination_state, self.storage, dest)

    def list_picker(self, path: str) -> None:
        self.picker_path = path.rstrip("/") or "/"
        self.picker_selected = ""
        self.submit("picker", self.storage.listdir, self.picker_path)

    def open_destination_picker(self) -> None:
        """Open the picker at the destination, or at its parent when it is new."""
        dest = self.destination()
        start = dest if self.dest_state == "exists" else posixpath.dirname(dest) if dest.startswith("/") else ""
        self.list_picker(start or (self.settings.destinations[0][0] if self.settings.destinations else "/"))
        self.picker_open = True

    def problems(self) -> list[str]:
        """Why the page's action button is disabled."""
        found = []
        if self.page in ("transfer", "browse") and self.status:
            found.append(f"storage unreachable: {self.status}")
        if self.page == "transfer":
            if self.transfer is not None and "transfer" in self.futures:
                found.append("a transfer is running")
            if "sizes" in self.futures:
                found.append("measuring sources...")
            if not self.sources:
                found.append("add a file or folder to send")
            found += [f"{s} not found" for s in self.sources if not Path(s).exists()]
            dest = self.destination()
            if not dest:
                found.append("choose a destination folder")
            elif not dest.startswith("/"):
                found.append("destination must be a BioHPC path, e.g. /biohpc/data2/M231")
            elif self.dest_checked != dest or self.dest_state is None:
                found.append("checking destination...")
            elif self.dest_state not in ("exists", "new"):
                found.append(self.dest_state)
            if self.attach:
                found += self.recording.problems()
            if self.process:
                found += self.process_problems()
        elif self.page == "browse":
            if not self.selected:
                found.append("select a file or folder")
            elif self.selected.endswith(SIDECAR_SUFFIX):
                found.append("that is already a sidecar")
            found += self.recording.problems()
        return found

    def process_problems(self) -> list[str]:
        """Why processing cannot run."""
        found = []
        if not self.settings.partition.strip():
            found.append("set a GPU partition in Settings > Processing")
        if "mesc" in self.futures or self.mesc_for != tuple(self.sources):
            found.append("reading .mesc units...")
        elif not self.mesc:
            found.append("no .mesc files in the sources")
        elif not self.chosen:
            found.append("pick units to process")
        base = "/" + self.settings.mount_base.strip("/")
        if not self.destination().startswith(f"{base}/"):
            found.append(f"destination must be under {base} to process")
        return found + self.params.problems()

    def label_action(self) -> Optional[str]:
        if self.page == "transfer":
            label = f"{fa.ICON_FA_CLOUD_ARROW_UP}  Send {len(self.sources)} item{'' if len(self.sources) == 1 else 's'}"
            return label + (f", process {len(self.chosen)} unit{'' if len(self.chosen) == 1 else 's'}" if self.process else "")
        if self.page == "browse":
            return f"{fa.ICON_FA_TAGS}  Attach metadata"
        if self.page == "settings":
            return f"{fa.ICON_FA_FLOPPY_DISK}  Save"
        if self.page == "slices":
            return f"{fa.ICON_FA_DOWNLOAD}  Export {self.tab_slices} to csv"
        return None

    def confirm_send(self, sized: list[tuple[Path, int]]) -> None:
        """Ask before sending the measured sources."""
        metadata = self.recording.sidecar(user=self.user) if self.attach else None
        dest = self.destination()
        lines = [f"to {dest} on {self.storage.label}:"]
        for source, size in sized:
            lines.append(f"  {source}  ->  {dest}/{source.name}  ({human_bytes(size)})")
            if metadata is not None:
                lines.append(f"  and {dest}/{source.name}{SIDECAR_SUFFIX}")
        if self.overwrite:
            lines.append("Overwrite is on: existing files are replaced.")
        else:
            lines.append("Stops if any of these already exist.")
        lines.append("Local files are only read.")
        transfer = Transfer(self.storage, [source for source, _ in sized], dest, metadata, self.overwrite)
        pending = self.jobs_to_send() if self.process else []
        if pending:
            lines.append(f"Then submits {len(pending)} processing job(s) on {self.settings.partition}.")
        args = (transfer, pending, copy.deepcopy(self.params))
        self.confirm = {"title": "Send to BioHPC?", "lines": lines, "button": "Send", "run": self.send, "args": args}

    def act(self) -> None:
        self.message = ""
        if self.page == "transfer":
            # a big folder takes a while to measure, so the dialog opens once "sizes" lands
            self.submit("sizes", measure, [Path(s) for s in self.sources])
        elif self.page == "browse":
            path = f"{self.path_browse.rstrip('/')}/{self.selected}{SIDECAR_SUFFIX}"
            text = json.dumps({**self.recording.sidecar(user=self.user), "source": f"{self.path_browse.rstrip('/')}/{self.selected}"}, indent=2)
            lines = [f"write {path}", f"on {self.storage.label}. {self.selected} is not changed."]
            if f"{self.selected}{SIDECAR_SUFFIX}" in {e.name for e in self.entries}:
                lines.append("The existing sidecar is replaced.")
            self.confirm = {"title": "Attach metadata?", "lines": lines, "button": "Write", "run": self.submit, "args": ("attach", write_text, self.storage, path, text)}
        elif self.page == "settings":
            self.settings.save()
            self.connect()
            self.message = f"saved to {data_dir() / 'settings.json'}"
        elif self.page == "slices" and self.saver is None:
            # the save dialog asks before overwriting
            self.table_export = self.tab_slices
            self.saver = pfd.save_file(f"Export {self.tab_slices}", str(Path.home() / f"{self.tab_slices}.csv"), ["CSV", "*.csv"])

    def draw_header(self) -> None:
        """Name, version, user and storage status."""
        em = imgui.get_font_size()
        imgui.dummy(imgui.ImVec2(0, 0.2 * em))
        imgui.push_font(None, 1.5 * em)
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FLASK}  Lab4")
        imgui.pop_font()
        imgui.same_line(0, 0.6 * em)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 0.45 * em)
        imgui.text_disabled(f"v{__version__}")
        if self.user:
            if self.status is None:
                color, state = DIM, "checking"
            elif self.settings.backend == "mounted" and not self.settings.mount_root.strip():
                color, state = WARN, "not set up"
            elif self.status:
                color, state = ERROR, "unreachable"
            else:
                color, state = OK, "connected"
            text = f"{fa.ICON_FA_USER}  {self.user}    {fa.ICON_FA_CIRCLE}  {self.settings.backend}: {state}"
            imgui.same_line()
            room = imgui.get_content_region_avail().x - imgui.calc_text_size(text).x
            imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + max(0.0, room))
            imgui.text_colored(color, text)
            if imgui.is_item_hovered():
                lab = f"Lab4: {self.db.credentials.label()}" if self.db else "Lab4: offline"
                idl.wrapped_tooltip(f"{lab}\n{self.storage.label}" + (f": {self.status}" if self.status else ""))
        draw_wrapped("Move PML data to BioHPC with Lab4 metadata", DIM)

    def draw_body(self) -> None:
        """The sign-in card, or the rail and current page. Esc goes back to Overview."""
        self.poll()
        if not self.user:
            self.draw_login()
            return
        if imgui.is_key_pressed(imgui.Key.escape) and not imgui.is_any_item_active() and not imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id):
            self.page = "home"
        em = imgui.get_font_size()
        avail = imgui.get_content_region_avail()
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        with imgui_ctx.begin_child("##rail", imgui.ImVec2(WIDTH_RAIL_EM * em, avail.y), 0, imgui.WindowFlags_.no_scrollbar):
            self.draw_rail()
        x = p.x + WIDTH_RAIL_EM * em + 0.4 * em
        dl.add_line(imgui.ImVec2(x, p.y), imgui.ImVec2(x, p.y + avail.y), imgui.color_convert_float4_to_u32(EDGE), 1.0)
        imgui.same_line(0, 0.8 * em)
        imgui.push_style_var(imgui.StyleVar_.window_padding, imgui.ImVec2(1.2 * em, 0.6 * em))
        with imgui_ctx.begin_child("##content", imgui.ImVec2(0, avail.y), imgui.ChildFlags_.always_use_window_padding):
            self.draw_content()
        imgui.pop_style_var()
        if self.confirm is not None and not imgui.is_popup_open("###confirm"):
            imgui.open_popup("###confirm")
        if imgui.begin_popup_modal(f"{self.confirm['title'] if self.confirm else ''}###confirm", None, imgui.WindowFlags_.always_auto_resize)[0]:
            if self.confirm is None:
                imgui.close_current_popup()
            else:
                for line in self.confirm["lines"]:
                    imgui.text(line)
                imgui.dummy(imgui.ImVec2(0, 0.4 * em))
                idl.push_button_style(THEME, primary=True)
                go = imgui.button(self.confirm["button"], imgui.ImVec2(8 * em, 0))
                idl.pop_button_style()
                imgui.same_line()
                if go:
                    self.confirm["run"](*self.confirm["args"])
                if go or imgui.button("Cancel", imgui.ImVec2(8 * em, 0)) or imgui.is_key_pressed(imgui.Key.escape):
                    self.confirm = None
                    imgui.close_current_popup()
            imgui.end_popup()

    def draw_login(self) -> None:
        """Sign-in card: credentials file, Sign In, Work offline."""
        em = imgui.get_font_size()
        imgui.dummy(imgui.ImVec2(0, 3 * em))
        avail = imgui.get_content_region_avail().x
        width = min(avail, WIDTH_LOGIN_EM * em)
        imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + max(0.0, (avail - width) / 2))
        imgui.push_style_color(imgui.Col_.child_bg, CARD)
        imgui.push_style_var(imgui.StyleVar_.child_rounding, 8.0)
        imgui.push_style_var(imgui.StyleVar_.window_padding, imgui.ImVec2(1.2 * em, 1.0 * em))
        flags = imgui.ChildFlags_.borders | imgui.ChildFlags_.auto_resize_y | imgui.ChildFlags_.always_use_window_padding
        with imgui_ctx.begin_child("##login", imgui.ImVec2(width, 0), flags, imgui.WindowFlags_.no_scrollbar):
            idl.center_text(f"{fa.ICON_FA_LOCK}  Lab4 · BioHPC sign in", ACCENT)
            idl.center_text("move data, attach metadata, register recordings", DIM)
            imgui.dummy(imgui.ImVec2(0, 0.3 * em))
            imgui.separator()
            imgui.dummy(imgui.ImVec2(0, 0.3 * em))
            full = imgui.get_content_region_avail().x
            path = secrets_path()
            imgui.text_colored(DIM, f"{fa.ICON_FA_KEY}  Credentials")
            draw_wrapped(str(path), PATH if path.is_file() else ERROR)
            if not path.is_file():
                draw_wrapped("Download your credentials from the Lab4 workspace and save them here.", DIM)
            else:
                try:
                    credentials = Credentials.load(path)
                except (OSError, ValueError, KeyError) as e:
                    draw_wrapped(f"unreadable: {e}", ERROR)
                else:
                    local = credentials.host in ("127.0.0.1", "localhost", "::1")
                    draw_wrapped(f"signs in as {credentials.label()}" + ("" if local else "  (remote server)"), DIM if local else WARN)
            if self.error_login:
                draw_wrapped(f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.error_login}", ERROR)
            imgui.dummy(imgui.ImVec2(0, 0.4 * em))
            signing_in = "sign_in" in self.futures
            imgui.begin_disabled(signing_in or not path.is_file())
            idl.push_button_style(THEME, primary=True)
            if imgui.button("Signing in..." if signing_in else "Sign In", imgui.ImVec2(full, 1.9 * em)):
                self.sign_in()
            idl.pop_button_style()
            imgui.end_disabled()
            imgui.begin_disabled(signing_in)
            if imgui.button("Work offline", imgui.ImVec2(full, 1.7 * em)):
                self.work_offline()
            imgui.end_disabled()
            if imgui.is_item_hovered():
                idl.wrapped_tooltip("transfers and sidecars work without Lab4; registering does not")
        imgui.pop_style_var(2)
        imgui.pop_style_color()

    def draw_rail(self) -> None:
        """Page buttons, then recent sources; clicking one adds it to Transfer."""
        em = imgui.get_font_size()
        width = imgui.get_content_region_avail().x
        imgui.push_style_var(imgui.StyleVar_.button_text_align, imgui.ImVec2(0.0, 0.5))
        imgui.push_style_var(imgui.StyleVar_.frame_rounding, 5.0)
        for key, icon, name, hint in PAGES:
            lit = key == self.page
            imgui.push_style_color(imgui.Col_.button, LIT if lit else CLEAR)
            imgui.push_style_color(imgui.Col_.button_hovered, LIT if lit else CARD_HOVER)
            imgui.push_style_color(imgui.Col_.button_active, LIT)
            imgui.push_style_color(imgui.Col_.text, ACCENT if lit else TEXT)
            if imgui.button(f"{icon}   {name}##rail_{key}", imgui.ImVec2(width, 2.2 * em)):
                self.page = key
            imgui.pop_style_color(4)
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(f"{name}: {hint}")
        recent = [p for p in self.store.recent() if p][:6]
        if recent:
            imgui.dummy(imgui.ImVec2(0, 0.8 * em))
            imgui.text_disabled("RECENT")
            imgui.separator()
            imgui.push_style_color(imgui.Col_.button, CLEAR)
            imgui.push_style_color(imgui.Col_.button_hovered, CARD_HOVER)
            imgui.push_style_color(imgui.Col_.button_active, LIT)
            imgui.push_style_color(imgui.Col_.text, DIM)
            for i, path in enumerate(recent):
                if imgui.button(f"{fa.ICON_FA_CLOCK_ROTATE_LEFT}  {Path(path).name or path}##recent_{i}", imgui.ImVec2(width, 1.7 * em)):
                    self.page = "transfer"
                    if path not in self.sources:
                        self.sources.append(path)
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(f"Add to transfer: {path}")
            imgui.pop_style_color(4)
        imgui.pop_style_var(2)

    def draw_content(self) -> None:
        """Page title and subtitle, then the page."""
        em = imgui.get_font_size()
        key, icon, name, hint = next(page for page in PAGES if page[0] == self.page)
        imgui.push_font(None, 1.35 * em)
        imgui.text_colored(ACCENT, f"{icon}  {name}")
        imgui.pop_font()
        imgui.text_colored(DIM, hint)
        imgui.separator()
        imgui.dummy(imgui.ImVec2(0, 0.4 * em))
        if key == "home":
            self.draw_home()
        elif key == "transfer":
            self.draw_transfer()
        elif key == "browse":
            self.draw_browse()
        elif key == "slices":
            self.draw_slices()
        else:
            self.draw_settings()

    def step(self, n: int, title: str, hint: str) -> None:
        """A numbered step heading with a (?) hint."""
        em = imgui.get_font_size()
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        r = 0.75 * em
        center = imgui.ImVec2(p.x + r, p.y + r)
        dl.add_circle_filled(center, r, imgui.color_convert_float4_to_u32(LIT))
        dl.add_circle(center, r, imgui.color_convert_float4_to_u32(ACCENT), 0, 1.0)
        size = imgui.calc_text_size(str(n))
        dl.add_text(imgui.ImVec2(center.x - size.x / 2, center.y - size.y / 2), imgui.color_convert_float4_to_u32(ACCENT), str(n))
        imgui.dummy(imgui.ImVec2(2 * r, 2 * r))
        imgui.same_line(0, 0.6 * em)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 0.25 * em)
        imgui.text_colored(ACCENT, title)
        draw_hint(text=hint)
        imgui.dummy(imgui.ImVec2(0, 0.1 * em))

    def begin_form(self, name: str) -> bool:
        """Start a two-column form (caption, widget); close it with end_table."""
        if not imgui.begin_table(f"##form_{name}", 2, imgui.TableFlags_.sizing_stretch_prop):
            return False
        imgui.table_setup_column("##caption", imgui.TableColumnFlags_.width_fixed, WIDTH_CAPTION_EM * imgui.get_font_size())
        imgui.table_setup_column("##widget", imgui.TableColumnFlags_.width_stretch)
        return True

    def row(self, caption: str) -> None:
        """Start a form row: the caption, then a full-width widget."""
        imgui.table_next_row()
        imgui.table_next_column()
        imgui.align_text_to_frame_padding()
        imgui.text_disabled(caption)
        imgui.table_next_column()
        imgui.set_next_item_width(-1)

    def choice(self, label: str, value: str, items: list[str]) -> str:
        """A combo of items; value is added when missing."""
        items = items if value in items else [value, *items]
        _, index = imgui.combo(label, items.index(value), items)
        return items[index]

    def pick(self, label: str, value: str, items: list[str], hint: str = "") -> str:
        """A text field, with a dropdown of items when there are any."""
        if not items:
            return imgui.input_text_with_hint(label, hint, value)[1]
        spacing = imgui.get_style().item_inner_spacing.x
        imgui.set_next_item_width(imgui.get_content_region_avail().x - imgui.get_frame_height() - spacing)
        _, value = imgui.input_text_with_hint(label, hint, value)
        imgui.same_line(0, spacing)
        if imgui.begin_combo(f"{label}_pick", "", imgui.ComboFlags_.no_preview | imgui.ComboFlags_.popup_align_left):
            for item in items:
                if imgui.selectable(item, item == value)[0]:
                    value = item
            imgui.end_combo()
        return value

    def lookup(self, label: str, value: str, column: str, hint: str) -> str:
        """A combo over the column's Lab4 lookup values, or a text field offline."""
        items = self.schema.choices.get(column, []) if self.schema else []
        return self.choice(label, value, items) if items else imgui.input_text_with_hint(label, hint, value)[1]

    def draw_home(self) -> None:
        """Page cards, links, destinations and the files a transfer writes."""
        em = imgui.get_font_size()
        pages = [page for page in PAGES if page[0] in ("transfer", "browse", "slices")]
        i = draw_cards([(icon, name, hint) for key, icon, name, hint in pages])
        if i is not None:
            self.page = pages[i][0]
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        draw_wrapped(
            "Transfer copies data to BioHPC with a metadata sidecar beside it. Browse storage opens datasets, adds "
            "sidecars to data already there, and registers .h5 files in Lab4. Human slices tracks each slice from "
            "patient to assay (demo data).",
            DIM,
        )
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_LINK}  Links")
        draw_hint("opens in your browser")
        i = draw_cards([(icon, name, f"{hint}\n{url}") for icon, name, url, hint in LINKS])
        if i is not None:
            webbrowser.open(LINKS[i][2])
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FOLDER_OPEN}  Destinations")
        draw_hint("edit these in Settings")
        if imgui.begin_table("##destinations", 2, flags):
            imgui.table_setup_column("path", imgui.TableColumnFlags_.width_fixed, 15 * em)
            imgui.table_setup_column("holds", imgui.TableColumnFlags_.width_stretch)
            for path, description in self.settings.destinations:
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text_colored(KEY, path)
                imgui.table_next_column()
                imgui.text_wrapped(description)
            imgui.end_table()
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FILE}  Files written")
        if imgui.begin_table("##files", 2, flags):
            imgui.table_setup_column("file", imgui.TableColumnFlags_.width_fixed, 15 * em)
            imgui.table_setup_column("holds", imgui.TableColumnFlags_.width_stretch)
            for name, text in FILES:
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text_colored(KEY, name)
                imgui.table_next_column()
                imgui.text_wrapped(text)
            imgui.end_table()

    def draw_transfer(self) -> None:
        """The four transfer steps, then progress."""
        em = imgui.get_font_size()
        self.step(1, "Sources", "local files and folders, copied under their own names")
        remove = None
        for i, source in enumerate(self.sources):
            if imgui.small_button(f"{fa.ICON_FA_XMARK}##remove_{i}"):
                remove = i
            imgui.same_line()
            path = Path(source)
            icon = fa.ICON_FA_FOLDER if path.is_dir() else fa.ICON_FA_FILE
            imgui.text_colored(PATH if path.exists() else ERROR, f"{icon}  {path.name}")
            imgui.same_line()
            imgui.text_disabled(str(path.parent))
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(source)
        if remove is not None:
            self.sources.pop(remove)
        imgui.begin_disabled(self.picker is not None)
        if imgui.button(f"{fa.ICON_FA_FILE}  Add files"):
            self.open_picker(folders=False)
        imgui.same_line()
        if imgui.button(f"{fa.ICON_FA_FOLDER_OPEN}  Add folder"):
            self.open_picker(folders=True)
        imgui.end_disabled()

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(2, "Destination", "a BioHPC folder; a new one is made when its parent exists")
        self.check_destination()
        dest = self.destination()
        spacing = imgui.get_style().item_inner_spacing.x
        label_browse = f"{fa.ICON_FA_FOLDER_OPEN}  Browse..."
        imgui.set_next_item_width(imgui.get_content_region_avail().x - imgui.calc_text_size(label_browse).x - 2 * imgui.get_style().frame_padding.x - spacing)
        _, self.dest = imgui.input_text_with_hint("##dest", "/biohpc/data2/... (new folders are made)", self.dest)
        imgui.same_line(0, spacing)
        if imgui.button(label_browse):
            self.open_destination_picker()
        presets = {path.rstrip("/"): description for path, description in self.settings.destinations}
        if not dest:
            draw_wrapped("type a BioHPC folder or browse for one", DIM)
        elif not dest.startswith("/") or self.dest_checked != dest:
            pass
        elif self.dest_state is None:
            imgui.text_disabled("checking...")
        elif self.dest_state == "exists":
            draw_wrapped(f"{fa.ICON_FA_FOLDER}  goes to {dest}" + (f"  ·  {presets[dest]}" if dest in presets else ""), PATH)
        elif self.dest_state == "new":
            parent, name = posixpath.split(dest)
            draw_wrapped(f"{fa.ICON_FA_PLUS}  makes {name} in {parent or '/'}", ACCENT)
        else:
            draw_wrapped(f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.dest_state}", ERROR)
        self.draw_destination_picker()

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(3, "Metadata", f"saved as <name>{SIDECAR_SUFFIX} beside each source")
        _, self.attach = imgui.checkbox("Attach a metadata sidecar", self.attach)
        if self.attach:
            self.draw_recording()
        _, self.overwrite = imgui.checkbox("Overwrite existing files", self.overwrite)

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(4, "Processing", "run the ASAP7 spine pipeline on BioHPC after the transfer")
        _, self.process = imgui.checkbox("Process .mesc units overnight", self.process)
        if self.process:
            self.draw_processing()
        self.draw_jobs()

        if self.transfer is not None:
            imgui.dummy(imgui.ImVec2(0, 0.6 * em))
            running = "transfer" in self.futures
            done = f"{human_bytes(self.transfer.done)} / {human_bytes(self.transfer.total)}"
            imgui.progress_bar(self.transfer.fraction, imgui.ImVec2(-1, 1.3 * em), done)
            if running:
                draw_wrapped(self.transfer.current or "preparing", DIM)
                if imgui.button(f"{fa.ICON_FA_XMARK}  Cancel"):
                    self.transfer.cancelled = True
            else:
                draw_wrapped(self.message, ERROR)
                if imgui.small_button("dismiss"):
                    self.transfer = None
                    self.message = ""

    def draw_processing(self) -> None:
        """Unit picker for each .mesc, then the pipeline settings."""
        em = imgui.get_font_size()
        self.read_mesc()
        if "mesc" in self.futures:
            imgui.text_disabled("reading .mesc units...")
        elif not self.mesc:
            draw_wrapped("No .mesc files in the sources.", DIM)
        for local, relative, units in self.mesc:
            imgui.text_colored(PATH, f"{fa.ICON_FA_FILE}  {relative}")
            if isinstance(units, str):
                draw_wrapped(units, ERROR)
                continue
            runnable = [u for u in units if processing.is_runnable(u, self.params)]
            flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.sizing_stretch_prop
            if runnable and imgui.begin_table(f"##units_{local}", 5, flags):
                imgui.table_setup_column("##pick", imgui.TableColumnFlags_.width_fixed, 1.6 * em)
                imgui.table_setup_column("unit", imgui.TableColumnFlags_.width_fixed, 11 * em)
                imgui.table_setup_column("frames", imgui.TableColumnFlags_.width_fixed, 5 * em)
                imgui.table_setup_column("length", imgui.TableColumnFlags_.width_fixed, 8 * em)
                imgui.table_setup_column("comment", imgui.TableColumnFlags_.width_stretch)
                imgui.table_headers_row()
                for unit in runnable:
                    key = (local, unit.key)
                    imgui.table_next_row()
                    imgui.table_next_column()
                    ticked, on = imgui.checkbox(f"##{local}_{unit.key}", key in self.chosen)
                    if ticked:
                        self.chosen = self.chosen | {key} if on else self.chosen - {key}
                    imgui.table_next_column()
                    imgui.text(unit.key)
                    imgui.table_next_column()
                    imgui.text(str(unit.frames))
                    imgui.table_next_column()
                    imgui.text(f"{unit.seconds:.0f} s at {unit.fs:.0f} Hz")
                    imgui.table_next_column()
                    imgui.text_disabled(unit.comment)
                imgui.end_table()
            hidden = len(units) - len(runnable)
            if hidden:
                imgui.text_disabled(f"{hidden} unit(s) hidden: not a movie, or too few frames")
        self.subsection("Pipeline", "mbo asap7 (asap7_spine_pipeline_detrend.ipynb); defaults match the notebook")
        params = self.params
        if self.begin_form("asap7"):
            self.row("channel")
            _, params.channel = imgui.input_int("##channel", params.channel)
            self.row("first frame")
            _, params.first_frame = imgui.input_int("##first_frame", params.first_frame, 50)
            self.row("MAD threshold")
            _, params.mad_correlation_threshold = imgui.input_float("##mad", params.mad_correlation_threshold, 0.05, 0.1, "%.2f")
            self.row("detrend window")
            _, params.detrend_window_s = imgui.input_float("##detrend", params.detrend_window_s, 0.05, 0.1, "%.2f s")
            imgui.end_table()
        partition = self.settings.partition or "(no partition set)"
        draw_wrapped(
            f"One job per unit on {partition} via ssh {self.settings.processing_host}, after the transfer finishes. "
            f"Results go to <folder>/{processing.NAME_RESULTS}/<unit>_detrend_<time> beside the .mesc."
            + (f" Emails {self.settings.email} when each ends." if self.settings.email else ""),
            DIM,
        )

    def draw_jobs(self) -> None:
        """Recent jobs and their SLURM state."""
        if not self.jobs:
            return
        em = imgui.get_font_size()
        self.subsection("Submitted", f"last {NUMBER_JOBS_SHOWN} jobs from this machine, newest first")
        imgui.same_line()
        imgui.begin_disabled("states" in self.futures)
        if imgui.small_button(f"{fa.ICON_FA_ARROWS_ROTATE}  Check"):
            self.check_jobs()
        imgui.end_disabled()
        waiting = sum(job["job_id"] is None for job in self.jobs)
        if waiting:
            imgui.same_line()
            imgui.begin_disabled("process" in self.futures or not self.settings.partition.strip())
            if imgui.small_button(f"{fa.ICON_FA_PLAY}  Submit {waiting} waiting"):
                self.submit_waiting()
            imgui.end_disabled()
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.sizing_stretch_prop
        if imgui.begin_table("##jobs", 4, flags):
            imgui.table_setup_column("job", imgui.TableColumnFlags_.width_fixed, 5 * em)
            imgui.table_setup_column("unit", imgui.TableColumnFlags_.width_fixed, 11 * em)
            imgui.table_setup_column("state", imgui.TableColumnFlags_.width_fixed, 10 * em)
            imgui.table_setup_column("results", imgui.TableColumnFlags_.width_stretch)
            for job in reversed(self.jobs[-NUMBER_JOBS_SHOWN:]):
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text(job["job_id"] or "-")
                imgui.table_next_column()
                imgui.text(job["unit"])
                imgui.table_next_column()
                state = self.job_states.get(job["job_id"], "") if job["job_id"] else "waiting to submit"
                color = OK if state.startswith("COMPLETED") else ERROR if state.split(" ")[0] in STATES_FAILED else WARN if not job["job_id"] else TEXT
                imgui.text_colored(color, state or "not checked")
                imgui.table_next_column()
                imgui.text_disabled(job["dir_save"])
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(f"{job['mesc']}\nchosen {job['chosen_at']}, submitted {job['submitted_at'] or 'not yet'}")
            imgui.end_table()

    def subsection(self, title: str, hint: str) -> None:
        """A small heading inside a step, with a (?) hint."""
        imgui.dummy(imgui.ImVec2(0, 0.3 * imgui.get_font_size()))
        imgui.text_disabled(title.upper())
        draw_hint(text=hint)

    def draw_destination_picker(self) -> None:
        """Modal BioHPC folder picker; double-click enters a folder."""
        em = imgui.get_font_size()
        if self.picker_open:
            imgui.open_popup(TITLE_PICKER)
            self.picker_open = False
        imgui.set_next_window_size(imgui.ImVec2(44 * em, 28 * em), imgui.Cond_.appearing)
        if not imgui.begin_popup_modal(TITLE_PICKER, None)[0]:
            return
        for path, description in self.settings.destinations:
            if imgui.small_button(f"{path}##shortcut_{path}"):
                self.list_picker(path)
            if imgui.is_item_hovered() and description:
                idl.wrapped_tooltip(description)
            imgui.same_line()
        imgui.new_line()
        if imgui.button(f"{fa.ICON_FA_ARROW_UP}##picker_up"):
            self.list_picker(posixpath.dirname(self.picker_path) or "/")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        entered, self.picker_path = imgui.input_text("##picker_path", self.picker_path, imgui.InputTextFlags_.enter_returns_true)
        if entered:
            self.list_picker(self.picker_path)
        if "picker" in self.futures:
            imgui.text_disabled("listing...")
        elif self.picker_error:
            draw_wrapped(self.picker_error, ERROR)
        flags_row = imgui.SelectableFlags_.allow_double_click
        if imgui.begin_child("##picker_list", imgui.ImVec2(0, -2.4 * em), imgui.ChildFlags_.borders):
            for entry in self.picker_entries:
                clicked, _ = imgui.selectable(f"{fa.ICON_FA_FOLDER}  {entry.name}", entry.name == self.picker_selected, flags_row)
                if clicked:
                    self.picker_selected = entry.name
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    self.list_picker(f"{self.picker_path.rstrip('/')}/{entry.name}")
            if not self.picker_entries and "picker" not in self.futures and not self.picker_error:
                imgui.text_disabled("no folders here")
        imgui.end_child()
        chosen = f"{self.picker_path.rstrip('/')}/{self.picker_selected}" if self.picker_selected else self.picker_path
        idl.push_button_style(THEME, primary=True)
        if imgui.button(f"Use {chosen}"):
            self.dest = chosen
            imgui.close_current_popup()
        idl.pop_button_style()
        imgui.same_line()
        if imgui.button("Cancel"):
            imgui.close_current_popup()
        imgui.same_line()
        imgui.text_disabled("for a new folder, type its name in the destination field")
        imgui.end_popup()

    def draw_recording(self) -> None:
        """The metadata form and sidecar preview."""
        em = imgui.get_font_size()
        rec = self.recording
        schema = self.schema
        self.subsection("Recording", "the Lab4 key: group, subject, session, device")
        if self.begin_form("key"):
            self.row("experimenter")
            rec.experimenter = self.lookup("##experimenter", rec.experimenter, "experimenter", "full name")
            self.row("experiment group")
            groups = schema.groups_of(rec.experimenter.strip()) if schema else []
            rec.collection_name = self.pick("##group", rec.collection_name, groups, "study or cohort; new ones are added")
            self.row("subject")
            subject_before = rec.subject_id
            rec.subject_id = self.pick("##subject", rec.subject_id, schema.subjects if schema else [], "mouse id")
            if rec.subject_id != subject_before:
                self.look_up_subject()
            self.row("session start")
            _, rec.session_datetime = imgui.input_text_with_hint("##when", "YYYY-MM-DD HH:MM", rec.session_datetime)
            self.row("device")
            rec.device_name = self.lookup("##device", rec.device_name, "device_name", "rig")
            imgui.end_table()
        problems = rec.problems() + (schema.problems(rec) if schema else [])
        for problem in problems:
            draw_wrapped(f"{fa.ICON_FA_CIRCLE_INFO}  {problem}", WARN)
        if schema is None:
            draw_wrapped("offline: not checked against Lab4", DIM)
        elif rec.subject_id.strip() in schema.subjects:
            n = len(self.subject_recordings)
            draw_wrapped(f"{rec.subject_id.strip()} is in Lab4 with {n} recording{'' if n == 1 else 's'}", DIM)
            for row in self.subject_recordings[:5]:
                imgui.text_disabled(f"    {row['session_datetime']}  {row['device_name']}  {row['data_path']}")

        self.subsection("Details", "saved with the recording")
        if self.begin_form("details"):
            self.row("label")
            _, rec.ui_label = imgui.input_text_with_hint("##label", "optional", rec.ui_label)
            self.row("weight (g)")
            _, rec.weight_g = imgui.input_text_with_hint("##weight", "optional", rec.weight_g)
            self.row("notes")
            _, rec.notes = imgui.input_text_multiline("##notes", rec.notes, imgui.ImVec2(-1, 3.5 * em))
            imgui.end_table()

        if schema is not None and rec.subject_id.strip() and rec.subject_id.strip() not in schema.subjects and schema.subject_fields():
            self.subsection("New subject", f"{rec.subject_id.strip()} is new; registering adds it")
            if self.begin_form("subject"):
                for column in schema.subject_fields():
                    self.row(column.name.replace("_", " ") + ("" if column.required else " (optional)"))
                    value = rec.subject_fields.get(column.name, "")
                    rec.subject_fields[column.name] = self.pick(f"##subject_{column.name}", value, schema.choices.get(column.name, []), "YYYY-MM-DD" if column.type == "date" else "")
                imgui.end_table()

        self.subsection("Extra fields", "extra key/value pairs, sidecar only")
        remove = None
        for i, pair in enumerate(rec.extra):
            if imgui.small_button(f"{fa.ICON_FA_XMARK}##extra_{i}"):
                remove = i
            imgui.same_line()
            imgui.set_next_item_width(WIDTH_CAPTION_EM * em)
            _, pair[0] = imgui.input_text_with_hint(f"##key_{i}", "key", pair[0])
            imgui.same_line()
            imgui.set_next_item_width(-1)
            _, pair[1] = imgui.input_text_with_hint(f"##value_{i}", "value", pair[1])
        if remove is not None:
            rec.extra.pop(remove)
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add field"):
            rec.extra.append(["", ""])

        imgui.dummy(imgui.ImVec2(0, 0.3 * em))
        if not imgui.tree_node(f"{fa.ICON_FA_FILE_CODE}  sidecar preview: {rec.label()}{SIDECAR_SUFFIX}###sidecar_preview"):
            return
        text = json.dumps(rec.sidecar(user=self.user), indent=2)
        lines = text.count("\n") + 1
        imgui.push_style_color(imgui.Col_.text, CODE)
        imgui.input_text_multiline("##preview", text, imgui.ImVec2(-1, lines * imgui.get_text_line_height() + em), imgui.InputTextFlags_.read_only)
        imgui.pop_style_color()
        imgui.tree_pop()

    def draw_browse(self) -> None:
        """The folder listing with catalog info, the selected item's actions, and the metadata form."""
        em = imgui.get_font_size()
        parent = self.path_browse.rsplit("/", 1)[0] or "/"
        if imgui.button(f"{fa.ICON_FA_ARROW_UP}##up"):
            self.browse(path=parent)
        imgui.same_line()
        if imgui.button(f"{fa.ICON_FA_ARROWS_ROTATE}##refresh"):
            self.browse(path=self.path_browse)
        imgui.same_line()
        scanning = "scan" in self.futures
        imgui.begin_disabled(scanning)
        if imgui.button(f"{fa.ICON_FA_DATABASE}  Scan"):
            self.scan()
        imgui.end_disabled()
        if imgui.is_item_hovered():
            idl.wrapped_tooltip("index this folder and its subfolders for datasets")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        entered, self.path_browse = imgui.input_text("##path", self.path_browse, imgui.InputTextFlags_.enter_returns_true)
        if entered:
            self.browse(path=self.path_browse)

        summary, scan, items = self.described["folder"], self.described["scan"], self.described["items"]
        if scanning:
            imgui.text_colored(ACCENT, f"scanning {self.path_browse}... {self.count_scanned} items listed")
        elif scan is None:
            imgui.text_disabled("not scanned yet")
        else:
            kinds = summary["kinds"] if summary else {}
            below = f"{sum(kinds.values())} datasets below: {summarize(kinds)}" if kinds else "no datasets below"
            size = f" · {human_bytes(summary['size'])} in {summary['n_files']} files" if summary else ""
            imgui.text_disabled(f"scanned {scan['scanned_at'].replace('T', ' ')} · {below}{size}")
        if "list" in self.futures:
            imgui.text_disabled("listing...")
        elif self.error_browse:
            draw_wrapped(self.error_browse, ERROR)

        names = {e.name for e in self.entries}
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.scroll_y | imgui.TableFlags_.resizable
        height = max(8 * em, imgui.get_content_region_avail().y - 12 * em)
        if imgui.begin_table("##entries", 5, flags, imgui.ImVec2(0, height)):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column("name", imgui.TableColumnFlags_.width_stretch, 2.0)
            imgui.table_setup_column("kind", imgui.TableColumnFlags_.width_fixed, 8 * em)
            imgui.table_setup_column("holds", imgui.TableColumnFlags_.width_stretch, 1.5)
            imgui.table_setup_column("size", imgui.TableColumnFlags_.width_fixed, 5 * em)
            imgui.table_setup_column("##tag", imgui.TableColumnFlags_.width_fixed, 1.5 * em)
            imgui.table_headers_row()
            for entry in self.entries:
                item = items.get(entry.name, {})
                kind = item.get("kind")
                imgui.table_next_row()
                imgui.table_next_column()
                icon = fa.ICON_FA_FOLDER if entry.is_dir else fa.ICON_FA_FILE
                sidecar = entry.name.endswith(SIDECAR_SUFFIX)
                if sidecar or kind is not None:
                    imgui.push_style_color(imgui.Col_.text, DIM if sidecar else KEY)
                flags_row = imgui.SelectableFlags_.span_all_columns | imgui.SelectableFlags_.allow_double_click
                clicked, _ = imgui.selectable(f"{icon}  {entry.name}", entry.name == self.selected, flags_row)
                if sidecar or kind is not None:
                    imgui.pop_style_color()
                if clicked:
                    self.select(name=entry.name)
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    path = f"{self.path_browse.rstrip('/')}/{entry.name}"
                    if entry.is_dir:
                        self.browse(path=path)
                    elif kind is not None:
                        self.open_dataset(path=path, kind=kind)
                imgui.table_next_column()
                imgui.text_colored(KEY, kind or "")
                imgui.table_next_column()
                if kind is not None and item.get("detail"):
                    imgui.text_disabled(item["detail"])
                elif entry.is_dir and item.get("kinds"):
                    imgui.text_disabled(summarize(item["kinds"]))
                imgui.table_next_column()
                size = item.get("size", 0 if entry.is_dir else entry.size)
                imgui.text_disabled(human_bytes(size) if size else "")
                imgui.table_next_column()
                if f"{entry.name}{SIDECAR_SUFFIX}" in names:
                    imgui.text_colored(OK, fa.ICON_FA_TAGS)
            imgui.end_table()
        imgui.text_disabled("double-click to open a folder or dataset")

        if self.selected:
            item = items.get(self.selected, {})
            imgui.text_colored(ACCENT, f"{fa.ICON_FA_TAGS}  {self.selected}")
            path = f"{self.path_browse.rstrip('/')}/{self.selected}"
            if is_recording_data(path) and not any(e.is_dir for e in self.entries if e.name == self.selected):
                blockers = self.register_problems(path)
                imgui.same_line()
                imgui.begin_disabled(bool(blockers))
                if imgui.small_button(f"{fa.ICON_FA_DATABASE}  Register in Lab4"):
                    self.register(path)
                imgui.end_disabled()
                if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
                    idl.wrapped_tooltip("\n".join(blockers) if blockers else f"register {self.recording.label()} in Lab4 with this file")
            if item.get("kind"):
                tool, _, description = KINDS[item["kind"]]
                imgui.same_line()
                if imgui.small_button(f"{fa.ICON_FA_EYE}  Open in {tool}"):
                    self.open_dataset(path=f"{self.path_browse.rstrip('/')}/{self.selected}", kind=item["kind"])
                imgui.same_line()
                imgui.text_disabled(description)
            if "sidecar" in self.futures:
                imgui.text_disabled("reading metadata...")
            elif self.sidecar:
                imgui.push_style_color(imgui.Col_.text, CODE)
                imgui.input_text_multiline("##sidecar", self.sidecar, imgui.ImVec2(-1, 10 * em), imgui.InputTextFlags_.read_only)
                imgui.pop_style_color()
            else:
                draw_wrapped("no sidecar yet", DIM)
        imgui.dummy(imgui.ImVec2(0, 0.4 * em))
        if imgui.collapsing_header(f"{fa.ICON_FA_TAGS}  Metadata: {self.recording.label()}###browse_metadata"):
            self.draw_recording()

    def draw_settings(self) -> None:
        """One tab per group of settings; Save writes them all."""
        if imgui.begin_tab_bar("##settings"):
            tabs = (
                ("Connection", self.draw_connection),
                ("Destinations", self.draw_destinations),
                ("Viewers", self.draw_viewers),
                ("Processing", self.draw_processing_settings),
            )
            for name, draw in tabs:
                if imgui.begin_tab_item(name)[0]:
                    imgui.dummy(imgui.ImVec2(0, 0.3 * imgui.get_font_size()))
                    draw()
                    imgui.end_tab_item()
            imgui.end_tab_bar()
        imgui.dummy(imgui.ImVec2(0, 0.6 * imgui.get_font_size()))
        draw_wrapped(f"Save writes all tabs to {data_dir() / 'settings.json'}", DIM)

    def draw_slices(self) -> None:
        """The slices, patients and assays tables, then details and files for the picked row."""
        em = imgui.get_font_size()
        if imgui.begin_tab_bar("##slice_tabs"):
            for table in COLUMNS:
                flags = imgui.TabItemFlags_.set_selected if self.tab_request == table else 0
                selected, _ = imgui.begin_tab_item(f"{table.capitalize()} ({len(self.rows_slices[table])})###tab_{table}", None, flags)
                if selected:
                    if self.tab_slices != table:
                        self.editing = None
                    self.tab_slices = table
                    imgui.end_tab_item()
            imgui.end_tab_bar()
        self.tab_request = None
        table = self.tab_slices
        columns = COLUMNS[table]
        picked = self.picked[table]
        rows_picked = [r for r in self.rows_slices[table] if r["id"] in picked]

        if imgui.button(f"{fa.ICON_FA_PLUS}  Add"):
            if table == "slices":
                parent = rows_picked[0]["patient_id"] if rows_picked else ""
            elif table == "assays":
                parent = rows_picked[0]["slice_id"] if rows_picked else next((r["slice_id"] for r in self.rows_slices["slices"] if r["id"] in self.picked["slices"]), "")
            else:
                parent = ""
            try:
                id_ = self.slices.add(table, self.user_slices, parent)
            except ValueError as e:
                self.message, self.failed = str(e), True
            else:
                # a new row shows even when the filter would hide it
                self.filter_slices[table] = ""
                self.reload_slices()
                picked.clear()
                picked.add(id_)
                self.scroll_slices = id_
                self.message, self.failed = f"added a row to {table}; double-click cells to edit", False
        if imgui.is_item_hovered():
            hints = {
                "slices": "new slice for the picked slice's patient, or the newest patient",
                "patients": "new patient",
                "assays": "new assay for the picked slice",
            }
            idl.wrapped_tooltip(hints[table])
        imgui.same_line()
        imgui.begin_disabled(not picked)
        if imgui.button(f"{fa.ICON_FA_COPY}  Duplicate"):
            made = self.slices.duplicate(table, sorted(picked), self.user_slices)
            self.filter_slices[table] = ""
            self.reload_slices()
            picked.clear()
            picked.update(made)
            self.scroll_slices = made[0] if made else None
            self.message, self.failed = f"copied {len(made)} row(s)", False
        imgui.same_line()
        pressed = imgui.is_key_pressed(imgui.Key.delete) and not imgui.is_any_item_active() and not imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id)
        if imgui.button(f"{fa.ICON_FA_TRASH}  Delete") or (picked and pressed):
            key = "patient_id" if table == "patients" else "slice_id"
            names = [str(r[key]) for r in rows_picked]
            lines = [f"{len(names)} row(s) from {table}: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}", f"in {self.slices.path}", "Data files are not touched."]
            self.confirm = {"title": "Delete rows?", "lines": lines, "button": "Delete", "run": self.delete_rows, "args": (table, sorted(picked))}
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            idl.wrapped_tooltip("delete picked rows (Delete key); for a failed slice, set its outcome instead")
        imgui.end_disabled()
        if table in ("patients", "slices"):
            below, key = ("slices", "patient_id") if table == "patients" else ("assays", "slice_id")
            imgui.same_line()
            imgui.begin_disabled(len(rows_picked) != 1)
            if imgui.button(f"{fa.ICON_FA_ARROW_RIGHT}  Show its {below}"):
                self.filter_slices[below] = f"{key}={rows_picked[0][key]}"
                self.order_rows(table=below)
                self.tab_request = below
            imgui.end_disabled()
        text_reset = f"{fa.ICON_FA_ARROWS_ROTATE}  Reset rows"
        spacing = imgui.get_style().item_spacing.x
        width_user = 8 * em
        room = imgui.get_content_region_avail().x - imgui.calc_text_size(text_reset).x - 2 * imgui.get_style().frame_padding.x
        room -= width_user + imgui.calc_text_size("acting as").x + 3 * spacing
        imgui.same_line()
        imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + max(0.0, room))
        imgui.align_text_to_frame_padding()
        imgui.text_disabled("acting as")
        imgui.same_line()
        imgui.set_next_item_width(width_user)
        changed, index = imgui.combo("##user_slices", PEOPLE.index(self.user_slices), PEOPLE)
        if changed:
            self.user_slices = PEOPLE[index]
            self.editing = None
        if imgui.is_item_hovered():
            idl.wrapped_tooltip(f"{ADMIN} edits every row; others edit only their own")
        imgui.same_line()
        if imgui.button(text_reset):
            lines = [
                f"Deletes every row in {self.slices.path}",
                "and reloads the rows from the file names. Edits are lost.",
                "Data files are not touched.",
            ]
            self.confirm = {"title": "Reset rows?", "lines": lines, "button": "Reset", "run": self.reset_slices, "args": ()}

        imgui.set_next_item_width(-1)
        hints = {"slices": "outcome:failed ephys:synaptic day:2", "patients": "diagnosis:epilepsy sex:f", "assays": "assay:live-dead owner:dana"}
        changed, self.filter_slices[table] = imgui.input_text_with_hint("##filter", f"{fa.ICON_FA_FILTER}  filter, e.g. {hints[table]}", self.filter_slices[table])
        if changed:
            self.order_rows(table=table)
        order = self.order_slices[table]
        line = f"{len(order)} of {len(self.rows_slices[table])} {table}"
        line += f" · {len(picked)} picked" if picked else ""
        if table == "slices" and order:
            line += f" · {summarize(Counter(r['outcome'] for r in order if r['outcome']))}"
        n_locked = sum(not can_edit(r["owner"], self.user_slices) for r in order)
        line += f" · {n_locked} locked" if n_locked else ""
        imgui.text_disabled(line)

        flags = (
            imgui.TableFlags_.sortable
            | imgui.TableFlags_.sort_multi
            | imgui.TableFlags_.sort_tristate
            | imgui.TableFlags_.row_bg
            | imgui.TableFlags_.borders_inner_v
            | imgui.TableFlags_.resizable
            | imgui.TableFlags_.reorderable
            | imgui.TableFlags_.hideable
            | imgui.TableFlags_.scroll_x
            | imgui.TableFlags_.scroll_y
            | imgui.TableFlags_.sizing_fixed_fit
        )
        height = max(8 * em, imgui.get_content_region_avail().y - 11 * em)
        if imgui.begin_table(f"##db_{table}", len(columns), flags, imgui.ImVec2(0, height)):
            imgui.table_setup_scroll_freeze(1, 1)
            for i, (name, _, _) in enumerate(columns):
                # default sort: collection date, region, patient, day
                flags_column = imgui.TableColumnFlags_.default_sort if table == "slices" and i < 4 else 0
                imgui.table_setup_column(name, flags_column | (imgui.TableColumnFlags_.no_hide if i == 0 else 0))
            imgui.table_headers_row()
            specs = imgui.table_get_sort_specs()
            if specs is not None and specs.specs_dirty:
                self.sort_slices[table] = [
                    (columns[specs.get_specs(n).column_index][0], specs.get_specs(n).sort_direction == imgui.SortDirection.ascending)
                    for n in range(specs.specs_count)
                ]
                specs.specs_dirty = False
                self.order_rows(table=table)
                order = self.order_slices[table]
            hits = [i for i, row in enumerate(order) if row["id"] == self.scroll_slices]
            clipper = imgui.ListClipper()
            clipper.begin(len(order))
            if hits:
                clipper.include_item_by_index(hits[0])
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    row = order[i]
                    imgui.table_next_row()
                    for c, (name, kind, _) in enumerate(columns):
                        if not imgui.table_set_column_index(c):
                            continue
                        value = row[name]
                        if c == 0:
                            x = imgui.get_cursor_pos_x()
                            flags_row = imgui.SelectableFlags_.span_all_columns | imgui.SelectableFlags_.allow_overlap | imgui.SelectableFlags_.allow_double_click
                            clicked, _ = imgui.selectable(f"##row_{row['id']}", row["id"] in picked, flags_row)
                            if hits and i == hits[0]:
                                imgui.set_scroll_here_y(0.5)
                                self.scroll_slices = None
                            if clicked:
                                io = imgui.get_io()
                                ids = [r["id"] for r in order]
                                if io.key_ctrl:
                                    picked ^= {row["id"]}
                                elif io.key_shift and self.anchor[table] in ids:
                                    lo, hi = sorted((ids.index(self.anchor[table]), i))
                                    picked.update(ids[lo : hi + 1])
                                else:
                                    picked.clear()
                                    picked.add(row["id"])
                                if not io.key_shift:
                                    self.anchor[table] = row["id"]
                                column = imgui.table_get_hovered_column()
                                if imgui.is_mouse_double_clicked(imgui.MouseButton_.left) and 0 <= column < len(columns):
                                    self.start_edit(table, row, column)
                            imgui.same_line()
                            imgui.set_cursor_pos_x(x)
                            if not can_edit(row["owner"], self.user_slices):
                                imgui.text_colored(DIM, fa.ICON_FA_LOCK)
                                imgui.same_line()
                        editing = self.editing == (table, row["id"], name)
                        if editing and kind not in ("choice", "ref"):
                            if self.focus_edit:
                                imgui.set_keyboard_focus_here()
                                self.focus_edit = False
                            imgui.set_next_item_width(max(imgui.get_content_region_avail().x, 8 * em))
                            flags_edit = imgui.InputTextFlags_.enter_returns_true | imgui.InputTextFlags_.auto_select_all
                            entered, self.text_edit = imgui.input_text("##edit", self.text_edit, flags_edit)
                            if entered or imgui.is_item_deactivated_after_edit():
                                self.commit_edit(text=self.text_edit)
                            elif imgui.is_item_deactivated():
                                self.editing = None
                        elif value is None or value == "":
                            imgui.text("")
                        elif editing:
                            imgui.text_colored(ACCENT, str(value))
                        elif name in ("outcome", "incubator"):
                            imgui.text_colored(COLORS_SLICE.get(value, TEXT), value)
                        elif name == "failure_reason":
                            imgui.text_colored(WARN, value)
                        elif kind == "day":
                            imgui.text(f"Day {value}")
                        elif kind == "id":
                            imgui.text_colored(KEY, value)
                        elif kind == "ref" or name == "data_path":
                            imgui.text_colored(PATH, value)
                        elif kind == "owner":
                            imgui.text_disabled(value)
                        else:
                            imgui.text(str(value))
                        if kind == "text" and value and imgui.is_item_hovered():
                            idl.wrapped_tooltip(value)
            imgui.end_table()

        if self.open_choice:
            imgui.open_popup("##choice")
            self.open_choice = False
        if imgui.begin_popup("##choice"):
            if self.editing is None:
                imgui.close_current_popup()
            else:
                _, kind, choices = next(column for column in COLUMNS[self.editing[0]] if column[0] == self.editing[2])
                imgui.text_disabled(self.editing[2])
                imgui.separator()
                # a choice cell can be cleared; a ref cell cannot
                options = [r[self.editing[2]] for r in self.rows_slices[choices]] if kind == "ref" else ["", *choices]
                for option in options:
                    if imgui.selectable(option or "(none)", option == self.text_edit)[0]:
                        self.commit_edit(text=option)
            imgui.end_popup()
        elif self.editing is not None and next(column[1] for column in COLUMNS[self.editing[0]] if column[0] == self.editing[2]) in ("choice", "ref"):
            self.editing = None

        row = rows_picked[0] if len(rows_picked) == 1 else None
        slice_ = None
        if row is not None and table != "patients":
            slice_ = row if table == "slices" else next((s for s in self.rows_slices["slices"] if s["slice_id"] == row["slice_id"]), None)
        files, claimed, heading = self.files_of(table, row, slice_)

        flags_detail = imgui.TableFlags_.sizing_stretch_same | imgui.TableFlags_.borders_inner_v
        if not imgui.begin_table("##slice_detail", 2, flags_detail, imgui.ImVec2(0, imgui.get_content_region_avail().y)):
            return
        imgui.table_next_row()
        imgui.table_next_column()
        with imgui_ctx.begin_child("##slice_record"):
            if row is None:
                draw_wrapped("Pick a row to see its details and files.", DIM)
                draw_wrapped(
                    "Double-click a cell to edit. Ctrl or shift-click picks several rows. Shift-click headers to sort by "
                    "several; right-click one to hide columns. Filter with words, column:value or column=value for an exact "
                    "match. file_match: every word must be in the path, a date matches any spelling, and -word excludes.",
                    DIM,
                )
            elif table == "patients":
                theirs = [s for s in self.rows_slices["slices"] if s["patient_id"] == row["patient_id"]]
                draw_wrapped(f"{fa.ICON_FA_USER}  {row['patient_id']}: {row['age'] or '?'} y {row['sex'] or '?'}, {row['diagnosis'] or 'diagnosis not set'}, collected {row['collection_date']}")
                outcomes = summarize(Counter(s["outcome"] for s in theirs if s["outcome"]))
                days = summarize(Counter(f"on Day {s['day']}" for s in theirs if s["day"] is not None))
                draw_wrapped(f"{len(theirs)} slices{': ' + outcomes if outcomes else ''}{'; ' + days if days else ''}", DIM)
                if row["notes"]:
                    draw_wrapped(row["notes"], DIM)
            elif slice_ is not None:
                failed = f" ({slice_['failure_reason']})" if slice_["failure_reason"] else ""
                day = f"Day {slice_['day']}" if slice_["day"] is not None else "day not set"
                draw_wrapped(f"{fa.ICON_FA_TABLE}  {slice_['slice_id']}: {day}, {slice_['region'] or 'region not set'}, {slice_['outcome']}{failed}", COLORS_SLICE.get(slice_["outcome"], TEXT))
                patient = next((p for p in self.rows_slices["patients"] if p["patient_id"] == slice_["patient_id"]), None)
                if patient is not None:
                    draw_wrapped(f"{fa.ICON_FA_USER}  {patient['patient_id']}, collected {patient['collection_date']}{', ' + patient['diagnosis'] if patient['diagnosis'] else ''}", DIM)
                construct = f" {slice_['ep_construct']}" if slice_["ep_construct"] else ""
                notes = f" ({slice_['ephys_notes']})" if slice_["ephys_notes"] else ""
                draw_wrapped(f"electroporated {slice_['ep_material'] or '?'}{construct}, dye {slice_['ep_dye'] or '?'} · ephys {slice_['ephys'] or '?'}{notes} · {slice_['incubator'] or 'incubator not set'}", DIM)
                for assay in [a for a in self.rows_slices["assays"] if a["slice_id"] == slice_["slice_id"]]:
                    live = f"{assay['percent_live']}% live, " if assay["percent_live"] is not None else ""
                    draw_wrapped(f"{fa.ICON_FA_FLASK}  {assay['assay']} {assay['assay_date'] or ''} by {assay['owner']}: {live}{assay['result'] or ''}", CODE)
                if slice_["notes"]:
                    draw_wrapped(slice_["notes"], DIM)
                if slice_["source"]:
                    draw_wrapped(f"from {slice_['source']}", DIM)
        imgui.table_next_column()
        with imgui_ctx.begin_child("##slice_files"):
            indexing = "files_index" in self.futures
            if self.indexed is None:
                imgui.text_disabled("indexing..." if indexing else "not indexed yet")
            else:
                imgui.text_disabled(f"{len(self.files_index)} files indexed {self.indexed['indexed_at'].replace('T', ' ')} ({self.indexed['storage']})")
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(f"all files under {ROOT}; read-only")
            imgui.same_line()
            imgui.begin_disabled(indexing)
            if imgui.small_button(f"{fa.ICON_FA_ARROWS_ROTATE}##index"):
                self.index_files()
            imgui.end_disabled()
            if self.error_index:
                draw_wrapped(f"indexing failed on {self.storage.label}: {self.error_index}", WARN)
            if self.fetch is not None:
                total = max(self.fetch["total"], 1)
                imgui.progress_bar(self.fetch["done"] / total, imgui.ImVec2(-6 * em, 0), f"copying {self.fetch['local'].name}")
                imgui.same_line()
                if imgui.small_button("cancel##fetch"):
                    self.fetch["cancelled"] = True
            if heading:
                imgui.text_colored(PATH, f"{fa.ICON_FA_FOLDER_OPEN}  {heading}")
            copies: dict[tuple[str, int], list[str]] = {}
            for f in files:
                copies.setdefault((posixpath.basename(f["path"]), f["size"]), []).append(f["path"])
            imgui.begin_disabled(self.fetch is not None)
            for (name, size), paths in copies.items():
                kind = classify_file(name)
                icon = ICONS_FILE.get(posixpath.splitext(name)[1].lower(), fa.ICON_FA_FILE)
                stray = bool(claimed) and not any(path in claimed for path in paths)
                color = WARN if stray else KEY if kind is not None else None
                if color is not None:
                    imgui.push_style_color(imgui.Col_.text, color)
                clicked, _ = imgui.selectable(f"{icon}  {name}##file_{paths[0]}", False, imgui.SelectableFlags_.allow_double_click)
                if color is not None:
                    imgui.pop_style_color()
                if imgui.is_item_hovered():
                    how = f"double-click to open in {KINDS[kind][0]}" if kind else "double-click to open in its default app"
                    where = "\n".join(paths)
                    idl.wrapped_tooltip(f"{how}{'; matches no slice' if stray else ''}\n{where}")
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    self.open_file(path=paths[0], size=size)
                imgui.same_line()
                imgui.text_disabled(f"{human_bytes(size)}{f' · {len(paths)} copies' if len(paths) > 1 else ''}")
            imgui.end_disabled()
        imgui.end_table()

    def draw_connection(self) -> None:
        """Backend choice and its settings."""
        s = self.settings
        draw_wrapped("No login here: a mounted share uses your mount, ssh uses your keys.", DIM)
        for backend, label in zip(BACKENDS, ("Mounted share", "SSH")):
            if imgui.radio_button(label, s.backend == backend):
                s.backend = backend
            imgui.same_line()
        imgui.new_line()
        if self.begin_form("connection"):
            if s.backend == "mounted":
                self.row("mount root")
                _, s.mount_root = imgui.input_text_with_hint("##root", r"e.g. X:\ or \\lamella.biohpc.swmed.edu\share", s.mount_root)
                self.row("shows")
                _, s.mount_base = imgui.input_text_with_hint("##base", "BioHPC path of the mount root, e.g. /biohpc", s.mount_base)
            else:
                self.row("host")
                _, s.ssh_host = imgui.input_text_with_hint("##host", "ssh alias or user@host, e.g. lab-research", s.ssh_host)
            imgui.end_table()
        if s.backend == "mounted":
            if imgui.small_button(f"{fa.ICON_FA_MAGNIFYING_GLASS}  Detect"):
                self.detect_mounts()
            imgui.same_line()
            if self.mounts is None:
                imgui.text_disabled(f"looking for {s.mount_base} mounts...")
            elif not self.mounts:
                imgui.text_disabled(f"no {s.mount_base} mount found; map the share first (e.g. SSHFS-Win)")
            else:
                imgui.text_disabled(f"{s.mount_base} mounts found:")
                for i, mount in enumerate(self.mounts):
                    if imgui.small_button(f"use##mount_{i}"):
                        self.use_mount(mount)
                    imgui.same_line()
                    imgui.text_colored(OK if mount.root == s.mount_root else TEXT, mount.root)
                    imgui.same_line()
                    imgui.text_disabled(mount.remote)
        hint = (
            f"{s.mount_base.rstrip('/')}/data2 maps to <root>/data2"
            if s.backend == "mounted"
            else "ssh needs a key set up already (ssh-copy-id)"
        )
        draw_wrapped(hint, DIM)

    def draw_destinations(self) -> None:
        """Folders offered on Transfer."""
        em = imgui.get_font_size()
        s = self.settings
        draw_wrapped("The folders Transfer offers.", DIM)
        remove = None
        for i, pair in enumerate(s.destinations):
            if imgui.small_button(f"{fa.ICON_FA_XMARK}##dest_{i}"):
                remove = i
            imgui.same_line()
            imgui.set_next_item_width(15 * em)
            _, pair[0] = imgui.input_text_with_hint(f"##dpath_{i}", "/path/on/biohpc", pair[0])
            imgui.same_line()
            imgui.set_next_item_width(-1)
            _, pair[1] = imgui.input_text_with_hint(f"##ddesc_{i}", "what goes here", pair[1])
        if remove is not None:
            s.destinations.pop(remove)
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add destination"):
            s.destinations.append(["", ""])

    def draw_viewers(self) -> None:
        """Viewer commands."""
        s = self.settings
        draw_wrapped("Programs that open datasets: a name on PATH or a full path.", DIM)
        if self.begin_form("viewers"):
            self.row("mbo")
            _, s.command_mbo = imgui.input_text_with_hint("##mbo", "mbo", s.command_mbo)
            self.row("masknmf")
            _, s.command_masknmf = imgui.input_text_with_hint("##masknmf", "masknmf", s.command_masknmf)
            imgui.end_table()
        for name, command in (("mbo", s.command_mbo), ("masknmf", s.command_masknmf)):
            found = shutil.which(command)
            imgui.text_colored(OK if found else WARN, f"{name}: {found or 'not found'}")

    def draw_processing_settings(self) -> None:
        """SLURM job settings."""
        s = self.settings
        draw_wrapped("Jobs are submitted over ssh with your keys. To set up BioHPC, see hpc/asap7/README.md in pml_utilities.", DIM)
        if self.begin_form("processing"):
            self.row("ssh host")
            _, s.processing_host = imgui.input_text_with_hint("##phost", "ssh alias or user@host of a BioHPC login node", s.processing_host)
            self.row("partition")
            _, s.partition = imgui.input_text_with_hint("##partition", "a SLURM partition with GPUs", s.partition)
            self.row("GPUs")
            _, s.gres = imgui.input_text_with_hint("##gres", "gpu:1", s.gres)
            self.row("email")
            _, s.email = imgui.input_text_with_hint("##email", "emailed when a job ends; empty for none", s.email)
            self.row("pipeline folder")
            _, s.dir_asap7 = imgui.input_text_with_hint("##dir_asap7", "~/repos/pml_utilities/hpc/asap7", s.dir_asap7)
            self.row(f"{s.mount_base} is")
            _, s.cluster_root = imgui.input_text_with_hint("##cluster_root", "the share's path on BioHPC nodes", s.cluster_root)
            imgui.end_table()
        draw_wrapped(
            f"~/ is your BioHPC home. Jobs see {s.mount_base}/data2 as "
            f"{s.cluster_root.rstrip('/')}/data2.",
            DIM,
        )

    def draw_footer(self, dialog: idl.FileDialog) -> None:
        """Status on the left; Sign out, Quit and the page action on the right."""
        em = imgui.get_font_size()
        imgui.separator()
        imgui.spacing()
        problems = self.problems() if self.user else []
        label = self.label_action() if self.user else None
        spacing = imgui.get_style().item_spacing.x
        padding = 2 * imgui.get_style().frame_padding.x + 1.5 * em
        text_out = f"{fa.ICON_FA_POWER_OFF}  Sign out"
        widths = [imgui.calc_text_size(t).x + padding for t in [text_out if self.user else None, "Quit", label] if t]
        height = 1.7 * em
        if not imgui.begin_table("##footer", 2, imgui.TableFlags_.sizing_stretch_prop):
            return
        imgui.table_setup_column("##status", imgui.TableColumnFlags_.width_stretch)
        imgui.table_setup_column("##buttons", imgui.TableColumnFlags_.width_fixed, sum(widths) + spacing * (len(widths) - 1))
        imgui.table_next_row()
        imgui.table_next_column()
        if not self.user:
            idl.text_wrapped_colored(DIM, "sign in to continue")
        elif problems and label is not None:
            for problem in problems[:3]:
                idl.text_wrapped_colored(WARN, f"{fa.ICON_FA_CIRCLE_INFO}  {problem}")
        elif self.message and self.failed:
            idl.text_wrapped_colored(ERROR, f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.message}")
        elif self.message:
            idl.text_wrapped_colored(OK, f"{fa.ICON_FA_CIRCLE_CHECK}  {self.message}")
        elif self.page == "transfer":
            idl.text_wrapped_colored(DIM, f"{fa.ICON_FA_CLOUD_ARROW_UP}  sends to {self.destination() or '...'} over {self.settings.backend}")
        imgui.table_next_column()
        idl.push_button_style(dialog.theme, primary=False)
        if self.user:
            if imgui.button(text_out, imgui.ImVec2(widths[0], height)):
                self.sign_out()
            imgui.same_line()
        if imgui.button("Quit", imgui.ImVec2(widths[1 if self.user else 0], height)):
            dialog.cancel()
        idl.pop_button_style()
        if label is not None:
            imgui.same_line()
            imgui.push_style_color(imgui.Col_.button, COLORS_GO[0])
            imgui.push_style_color(imgui.Col_.button_hovered, COLORS_GO[1])
            imgui.push_style_color(imgui.Col_.button_active, COLORS_GO[2])
            imgui.begin_disabled(bool(problems))
            if imgui.button(label, imgui.ImVec2(widths[-1], height)):
                self.act()
            imgui.end_disabled()
            imgui.pop_style_color(3)
        imgui.end_table()


def run() -> None:
    """Open the window and block until it is closed."""
    idl.ensure_assets()
    app = Lab4App()
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = TITLE_WINDOW
    params.app_window_params.window_geometry.size = SIZE_WINDOW
    params.imgui_window_params.tweaked_theme.theme = hello_imgui.ImGuiTheme_.darcula_darker
    params.ini_folder_type = hello_imgui.IniFolderType.absolute_path
    params.ini_filename = str(data_dir() / "window.ini")
    app.dialog.apply_host_theme(params)
    params.callbacks.show_gui = app.dialog.render
    immapp.run(params)
    app.executor.shutdown(wait=False, cancel_futures=True)
