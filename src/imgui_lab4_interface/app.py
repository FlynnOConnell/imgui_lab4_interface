"""The `lab4` window: a sign-in card, then a page rail. Storage calls run on worker threads."""

import copy
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time
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
from imgui_lab4_interface.catalog import KINDS, Catalog, classify_file, scan_machines
from imgui_lab4_interface.config import (
    BACKENDS,
    REACHES,
    Settings,
    data_dir,
    this_computer,
)
from imgui_lab4_interface.database import (
    BIOHPC,
    Credentials,
    Lab4Database,
    Schema,
    secrets_path,
)
from imgui_lab4_interface.processing import Asap7Params, Job, MescUnit
from imgui_lab4_interface.slices import (
    ADMIN,
    COLUMNS,
    PEOPLE,
    ROOT,
    SliceDatabase,
    can_edit,
    file_dates,
    locked,
    match_files,
)
from imgui_lab4_interface.storage import (
    SIDECAR_SUFFIX,
    Entry,
    Mount,
    SshStorage,
    Storage,
    Transfer,
    destination_state,
    download,
    find_mounts,
    open_machine,
    open_storage,
    write_text,
)

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
COLORS_GO = (
    imgui.ImVec4(0.13, 0.55, 0.13, 1.0),
    imgui.ImVec4(0.18, 0.65, 0.18, 1.0),
    imgui.ImVec4(0.10, 0.45, 0.10, 1.0),
)
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
STATES_FAILED = (
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
)
# how often Results checks whether another process, e.g. lab4-scan, changed the catalog
SECONDS_WATCH = 3.0
# workers that can run for minutes; they get their own threads, so listings and checks never wait behind them
NAMES_LONG = (
    "transfer",
    "process",
    "scan",
    "link",
    "scan_machines",
    "files_index",
    "download",
)
COLORS_RUN = {"done": OK, "completed": OK, "failed": ERROR, "running": ACCENT}
NAMES_REACH = {"local": "This computer", "mounted": "Mounted here", "ssh": "SSH"}

# rail pages: key, icon, name, subtitle
PAGES = (
    ("home", fa.ICON_FA_HOUSE, "Overview", "what this does and where data goes"),
    (
        "transfer",
        fa.ICON_FA_CLOUD_ARROW_UP,
        "Transfer",
        "copy data to BioHPC with its metadata",
    ),
    (
        "browse",
        fa.ICON_FA_SITEMAP,
        "Browse storage",
        "browse BioHPC, open datasets, add metadata",
    ),
    (
        "recordings",
        fa.ICON_FA_DATABASE,
        "Lab4 recordings",
        "the recordings in Lab4: edit their details and find their data",
    ),
    (
        "results",
        fa.ICON_FA_CHART_LINE,
        "Results",
        "each recording's units, with the runs and results on every machine",
    ),
    (
        "slices",
        fa.ICON_FA_TABLE,
        "Human slices",
        "track human slices from collection to assay (demo data)",
    ),
    (
        "settings",
        fa.ICON_FA_GEAR,
        "Settings",
        "connection, destinations, viewers and processing",
    ),
)
# overview links: icon, name, url, tooltip
LINKS = (
    (
        fa.ICON_FA_BOOK,
        "Lab docs",
        "https://doc.memorylongevity.org/",
        "lab documentation",
    ),
    (
        fa.ICON_FA_FLASK,
        "Lab4 docs",
        "https://lab4.memorylongevity.org/",
        "lab4 package docs",
    ),
    (
        fa.ICON_FA_SERVER,
        "Lab4 workspace",
        "http://server.memorylongevity.org",
        "sign in with GitLab and download your credentials",
    ),
)
# slice table colors for outcome and incubator
COLORS_SLICE = {
    "ok": OK,
    "failed": ERROR,
    "in incubator": ACCENT,
    "out, recorded": OK,
    "out, imaged": CODE,
    "out, fixed": KEY,
}
# file icons by extension
ICONS_FILE = {
    ".mesc": fa.ICON_FA_FILE_IMAGE,
    ".tif": fa.ICON_FA_FILE_IMAGE,
    ".tiff": fa.ICON_FA_FILE_IMAGE,
    ".png": fa.ICON_FA_FILE_IMAGE,
    ".avi": fa.ICON_FA_FILE_VIDEO,
    ".mp4": fa.ICON_FA_FILE_VIDEO,
    ".pdf": fa.ICON_FA_FILE_PDF,
    ".pxp": fa.ICON_FA_CHART_LINE,
    ".nwb": fa.ICON_FA_CHART_LINE,
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
    """Full-width rows of (icon, name, tooltip) cards, as many to a row as fit their names; returns the index clicked, or None."""
    em = imgui.get_font_size()
    gap = 1.0 * em
    avail = imgui.get_content_region_avail().x
    width_name = (
        max(imgui.calc_text_size(f"{icon}  {name}").x for icon, name, _ in cards)
        + 2 * em
    )
    per_row = min(len(cards), max(1, int((avail + gap) // (width_name + gap))))
    width = (avail - gap * (per_row - 1)) / per_row
    clicked = None
    for i, (icon, name, tooltip) in enumerate(cards):
        if i % per_row:
            imgui.same_line(0, gap)
        if idl.icon_button(
            icon, name, imgui.ImVec2(width, 3.2 * em), theme=THEME, tooltip=tooltip
        ):
            clicked = i
    return clicked


def format_rate(fs: Optional[float]) -> str:
    return "" if not fs else f"{fs:.0f}" if fs >= 100 else f"{fs:.1f}"


def draw_run_counts(runs: list[dict]) -> None:
    """Runs by status as colored counts, e.g. 3 done in green then 1 failed in red."""
    for i, (status, n) in enumerate(
        sorted(Counter(run["status"] for run in runs).items())
    ):
        if i:
            imgui.same_line(0, 0.4 * imgui.get_font_size())
        imgui.text_colored(COLORS_RUN.get(status, DIM), str(n))
        if imgui.is_item_hovered():
            idl.wrapped_tooltip(f"{n} {status or 'no status'}")


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
    return [
        (
            s,
            sum(f.stat().st_size for f in s.rglob("*") if f.is_file())
            if s.is_dir()
            else s.stat().st_size,
        )
        for s in sources
    ]


def read_sidecar(storage: Storage, path: str) -> Optional[dict]:
    """The sidecar json at path, or None when there is none."""
    return json.loads(storage.read_text(path)) if storage.exists(path) else None


def is_recording_data(path: str) -> bool:
    """Whether path is an .h5, the file lab4.convert writes and Lab4 registers."""
    return path.lower().endswith((".h5", ".hdf5"))


def summarize(kinds: dict) -> str:
    """e.g. "3 tiff, 1 masknmf-run", most common first."""
    return ", ".join(
        f"{n} {kind}" for kind, n in sorted(kinds.items(), key=lambda item: -item[1])
    )


def format_time(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        if timestamp
        else ""
    )


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def width_button(label: str) -> float:
    """How wide a button labeled label is."""
    return imgui.calc_text_size(label).x + 2 * imgui.get_style().frame_padding.x


def same_line_or_wrap(width: float) -> None:
    """Put the next item, width wide, beside the last one if it fits there, else on the next line."""
    # decided before moving: same_line then new_line would leave the line's end counted as content, widening the window
    right = imgui.get_item_rect_max().x + imgui.get_style().item_spacing.x + width
    if right <= imgui.get_cursor_screen_pos().x + imgui.get_content_region_avail().x:
        imgui.same_line()


def caret_at(text: str, x: float) -> int:
    """The utf-8 offset of the caret x pixels into text, where a click there would put it."""
    n = 0
    while (
        n < len(text)
        and imgui.calc_text_size(text[: n + 1]).x - imgui.calc_text_size(text[n]).x / 2
        < x
    ):
        n += 1
    return len(text[:n].encode())


def columns_shown(count: int) -> list[int]:
    """The current table's columns left to right as shown, hidden ones left out; the first never moves."""
    table = imgui.internal.get_current_table()
    rest = [
        c
        for c in range(1, count)
        if imgui.table_get_column_flags(c) & imgui.TableColumnFlags_.is_enabled
    ]
    return [
        0,
        *sorted(
            rest, key=lambda c: imgui.internal.table_get_cell_bg_rect(table, c).min.x
        ),
    ]


def rect_cell(column: int) -> imgui.internal.ImRect:
    """The current row's cell in column, on screen; rows are one line high."""
    rect = imgui.internal.table_get_cell_bg_rect(
        imgui.internal.get_current_table(), column
    )
    height = imgui.get_text_line_height() + 2 * imgui.get_style().cell_padding.y
    return imgui.internal.ImRect(
        rect.min.x, rect.min.y, rect.max.x, rect.min.y + height
    )


def scroll_to_cell(column: int) -> None:
    """Scroll the current table, not the page, until the current row's cell in column shows clear of the frozen header and first column."""
    cell = rect_cell(column)
    first = rect_cell(0).get_width() if column else 0.0
    rect = imgui.internal.ImRect(
        cell.min.x - first, cell.min.y - cell.get_height(), cell.max.x, cell.max.y
    )
    flags = (
        imgui.internal.ScrollFlags_.keep_visible_edge_x
        | imgui.internal.ScrollFlags_.keep_visible_edge_y
        | imgui.internal.ScrollFlags_.no_scroll_parent
    )
    imgui.internal.scroll_to_rect(imgui.internal.get_current_window(), rect, flags)


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
        # Lab4 recordings: every row of the tables the window edits, by table, and the recording at each data path
        self.lab4: dict[str, list[dict]] = {}
        self.registered: dict[str, dict] = {}
        self.filter_lab4 = ""
        # the picked recording's rows (recording, session, subject), their columns as edited, and a new injection's
        self.picked_lab4: Optional[dict[str, dict]] = None
        self.edits_lab4: dict[str, dict[str, str]] = {}
        self.injection: dict[str, str] = {}
        # whether the next reload follows a save, which takes every saved value into the edits
        self.saved_lab4 = False
        # the sidecar beside the picked recording's data: its json, None when there is none, and why it cannot be read
        self.sidecar_lab4: Optional[dict] = None
        self.error_sidecar_lab4 = ""
        self.rewrite_sidecar = True
        # the sidecar to rewrite once a save lands: path and text
        self.sidecar_pending: Optional[tuple[str, str]] = None
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
        self.dest = (
            self.settings.destinations[0][0] if self.settings.destinations else ""
        )
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
        self.path_browse = (
            self.settings.destinations[0][0] if self.settings.destinations else "/"
        )
        self.entries: list[Entry] = []
        self.error_browse = ""
        self.selected = ""
        self.sidecar = ""
        self.catalog = Catalog()
        # catalog info for the browse folder
        self.described: dict = {"folder": None, "scan": None, "items": {}}
        self.count_scanned = 0
        # Results: the catalog's rows and scans, the picked (recording, unit), and the machine scan's progress
        self.overview: dict = {"rows": [], "scans": []}
        self.picked_result: Optional[tuple[str, str]] = None
        self.filter_results = ""
        self.all_units = False
        self.scanning = ("", 0)
        self.errors_scan: list[str] = []
        self.mtime_catalog: Optional[float] = None
        self.watched = 0.0
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
        self.sort_slices: dict[str, list[tuple[str, bool]]] = {
            table: [] for table in COLUMNS
        }
        self.order_slices: dict[str, list[dict]] = {table: [] for table in COLUMNS}
        self.picked: dict[str, set[int]] = {table: set() for table in COLUMNS}
        self.anchor: dict[str, Optional[int]] = {table: None for table in COLUMNS}
        # cell being edited (table, row id, column), its text before and now, and whether to focus it
        self.editing: Optional[tuple[str, int, str]] = None
        self.text_before = ""
        self.text_edit = ""
        self.focus_edit = False
        self.open_choice = False
        # the outlined cell, as editing; keys move it and typing replaces it, as in Excel
        self.cell: Optional[tuple[str, int, str]] = None
        # the table's columns as shown, and whether to scroll the outlined cell into view
        self.shown_slices: list[int] = []
        self.scroll_cell = False
        # where the double-click that opened the edit box landed, the caret offset it gives, and what was typed before
        # the box took focus
        self.x_click: Optional[float] = None
        self.caret_edit: Optional[int] = None
        self.chars_held = ""
        # a choice cell's list: its filter, the option it highlights, and the cell it drops from
        self.filter_choice = ""
        self.pick_choice: Optional[str] = None
        self.rect_choice = None
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
        self.executor_long = ThreadPoolExecutor(max_workers=len(NAMES_LONG))
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
        self.lab4, self.registered, self.picked_lab4 = {}, {}, None
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
        self.submit(
            "register",
            self.db.register,
            self.schema,
            copy.deepcopy(self.recording),
            path,
        )

    def look_up_subject(self) -> None:
        """List the subject's recordings already in Lab4."""
        subject = self.recording.subject_id.strip()
        self.subject_recordings = []
        if (
            self.db is not None
            and self.schema is not None
            and subject in self.schema.subjects
        ):
            self.submit("subject", self.db.recordings, subject)

    def load_lab4(self) -> None:
        """Read the recordings, sessions, subjects and injections from Lab4."""
        if self.db is not None and self.schema is not None:
            self.submit("lab4_rows", self.db.contents, self.schema)

    def rows_lab4(self, recording: dict) -> dict[str, dict]:
        """The recording row with its session's and its subject's, by table."""
        rows = {"recording": recording}
        for table in ("session", "subject"):
            key = self.schema.key(table, recording)
            rows[table] = next(
                row for row in self.lab4[table] if self.schema.key(table, row) == key
            )
        return rows

    def pick_lab4(self, recording: dict) -> None:
        """Pick a recording to edit, and look for the sidecar beside its data. Picking it again keeps its edits."""
        if self.picked_lab4 is not None and self.schema.key(
            "recording", self.picked_lab4["recording"]
        ) == self.schema.key("recording", recording):
            return
        self.picked_lab4 = self.rows_lab4(recording)
        self.edits_lab4 = {
            table: {
                c.name: lab4.text_of(row[c.name]) for c in self.schema.details(table)
            }
            for table, row in self.picked_lab4.items()
        }
        self.injection = {}
        self.sidecar_lab4, self.error_sidecar_lab4 = None, ""
        self.submit(
            "lab4_sidecar",
            read_sidecar,
            self.storage,
            f"{recording['data_path']}{SIDECAR_SUFFIX}",
        )

    def follow_lab4(self) -> None:
        """
        Follow the picked recording to its reloaded rows. Columns left as they were take the new values, so a save
        never puts back what someone else changed; after a save, every column does.
        """
        if self.picked_lab4 is None:
            return
        key = self.schema.key("recording", self.picked_lab4["recording"])
        recording = next(
            (
                row
                for row in self.lab4["recording"]
                if self.schema.key("recording", row) == key
            ),
            None,
        )
        if recording is None:
            self.picked_lab4 = None
            return
        rows = self.rows_lab4(recording)
        for table, fields in self.edits_lab4.items():
            for name, text in fields.items():
                if (
                    self.saved_lab4
                    or text.strip()
                    == lab4.text_of(self.picked_lab4[table][name]).strip()
                ):
                    fields[name] = lab4.text_of(rows[table][name])
        self.picked_lab4 = rows

    def changes_lab4(self) -> list[tuple[str, dict, dict]]:
        """(table, key, column -> text) for each of the picked recording's rows the edits change."""
        found = []
        for table, row in (self.picked_lab4 or {}).items():
            changed = {
                name: text
                for name, text in self.edits_lab4[table].items()
                if text.strip() != lab4.text_of(row[name]).strip()
            }
            if changed:
                found.append((table, self.schema.key(table, row), changed))
        return found

    def recording_lab4(self) -> lab4.Recording:
        """The picked recording as edited, as the metadata form holds one."""
        edits = self.edits_lab4
        notes = edits["session"].get("session_notes", "")
        return lab4.Recording.from_lab4(
            {**self.picked_lab4["recording"], **edits["recording"]},
            notes,
            edits["subject"],
        )

    def moves_data(self) -> bool:
        """Whether the edits change the picked recording's data path."""
        return (
            self.edits_lab4["recording"].get("data_path", "").strip()
            != self.picked_lab4["recording"]["data_path"]
        )

    def sidecar_rewrite(self) -> Optional[tuple[str, str]]:
        """
        Path and new text of the picked recording's sidecar, when a save rewrites it: it keeps the sidecar's other
        fields, such as extra and source, and takes the edited ones. None when there is no sidecar, the data path
        changes, or rewriting is off.
        """
        if self.sidecar_lab4 is None or not self.rewrite_sidecar or self.moves_data():
            return None
        rec = self.recording_lab4()
        rec.extra = [[k, str(v)] for k, v in self.sidecar_lab4.get("extra", {}).items()]
        text = json.dumps(
            {**self.sidecar_lab4, **rec.sidecar(user=self.user)}, indent=2
        )
        return f"{self.picked_lab4['recording']['data_path']}{SIDECAR_SUFFIX}", text

    def save_lab4(
        self, changes: list[tuple[str, dict, dict]], sidecar: Optional[tuple[str, str]]
    ) -> None:
        """Save the changes to Lab4; poll rewrites the sidecar once they land."""
        self.sidecar_pending = sidecar
        self.submit("lab4_save", self.db.update, self.schema, changes)

    def injection_problems(self, subject: str) -> list[str]:
        """Why the new injection cannot be added to subject yet."""
        if "lab4_add" in self.futures:
            return ["adding..."]
        values = {
            c.name: self.injection.get(c.name, "")
            for c in self.schema.tables["injection"]
            if c.name != "subject_id"
        }
        found = self.schema.edit_problems("injection", values)
        key = self.schema.key(
            "injection",
            {**{k: v.strip() for k, v in values.items()}, "subject_id": subject},
        )
        if any(
            {k: lab4.text_of(v) for k, v in self.schema.key("injection", row).items()}
            == key
            for row in self.lab4.get("injection", [])
        ):
            found.append(f"{subject} already has that injection")
        return found

    def confirm_injection(self, subject: str) -> None:
        values = {"subject_id": subject, **self.injection}
        lines = [
            f"add to Lab4's injections ({self.db.credentials.label()}):",
            "  "
            + ", ".join(
                f"{k.replace('_', ' ')} {v.strip()}"
                for k, v in values.items()
                if v.strip()
            ),
        ]
        self.confirm = {
            "title": "Add to Lab4?",
            "lines": lines,
            "button": "Add",
            "run": self.submit,
            "args": ("lab4_add", self.db.add, self.schema, "injection", values),
        }

    def show_in_browse(self, path: str) -> None:
        """List path's folder in Browse storage, with path selected."""
        self.browse(path=posixpath.dirname(path))
        self.selected = posixpath.basename(path)
        self.page = "browse"

    def show_in_lab4(self, path: str) -> None:
        """Pick the recording registered at path on the Lab4 recordings page."""
        self.pick_lab4(self.registered[path])
        self.page = "recordings"

    def load_sidecar(self) -> None:
        """Fill the metadata form from the selected item's sidecar."""
        try:
            self.recording = lab4.Recording.from_sidecar(json.loads(self.sidecar))
        except (ValueError, AttributeError) as e:
            self.message, self.failed = (
                f"cannot read {self.selected}{SIDECAR_SUFFIX}: {e}",
                True,
            )
            return
        self.message, self.failed = (
            f"the metadata form holds {self.recording.label()}, from {self.selected}{SIDECAR_SUFFIX}",
            False,
        )

    def quit(self) -> None:
        if self.transfer is not None:
            self.transfer.cancelled = True
        hello_imgui.get_runner_params().app_shall_exit = True

    def submit(self, name: str, fn, *args) -> None:
        """Run fn on a worker; poll handles the result by name."""
        executor = self.executor_long if name in NAMES_LONG else self.executor
        self.futures[name] = executor.submit(fn, *args)

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
        self.submit(
            "describe", self.catalog.describe, self.storage.label, self.path_browse
        )

    def scan(self) -> None:
        """Catalog the browse folder and everything under it."""
        self.count_scanned = 0
        self.submit(
            "scan", self.catalog.scan, self.storage, self.path_browse, self.count_scan
        )

    def count_scan(self, n: int) -> None:
        self.count_scanned = n

    def scan_machines(self) -> None:
        """Catalog every root of every machine in Settings, then reload Results."""
        self.errors_scan = []
        self.submit(
            "scan_machines",
            scan_machines,
            self.catalog,
            copy.deepcopy(self.settings.machines),
            self.count_machine_scan,
        )

    def count_machine_scan(self, where: str, n: int) -> None:
        self.scanning = (where, n)

    def watch_catalog(self) -> None:
        """Reload Results when the catalog changed, every SECONDS_WATCH at most."""
        now = time.monotonic()
        if now - self.watched < SECONDS_WATCH or "overview" in self.futures:
            return
        self.watched = now
        try:
            mtime = self.catalog.path.stat().st_mtime
        except OSError:
            return
        if mtime != self.mtime_catalog:
            self.mtime_catalog = mtime
            self.submit("overview", self.catalog.overview)

    def open_on(self, label: str, path: str, kind: Optional[str]) -> None:
        """Open a path on a machine, or on BioHPC, in place: in kind's viewer, or its default app when kind is None."""
        if label == self.storage.label:
            storage = self.storage
        else:
            storage = next(
                (open_machine(m) for m in self.settings.machines if m["name"] == label),
                None,
            )
        if storage is None:
            self.message, self.failed = f"{label} is not in Settings > Machines", True
            return
        try:
            local = storage.local_path(path)
        except OSError as e:
            self.message, self.failed = str(e), True
            return
        if local is None:
            self.message, self.failed = (
                f"{label} is reached over ssh; mount it to open {posixpath.basename(path)}",
                True,
            )
        elif not local.exists():
            self.message, self.failed = f"{local} not found; is {label} mounted?", True
        else:
            self.open_local(local=local, kind=kind)

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
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", str(local)]
                )
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
            self.message, self.failed = (
                f"{local} not found; is the share mounted?",
                True,
            )
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
        self.confirm = {
            "title": "Copy to this machine?",
            "lines": lines,
            "button": "Copy and open",
            "run": self.start_fetch,
            "args": (remote, cached, kind, size),
        }

    def start_fetch(
        self, remote: str, cached: Path, kind: Optional[str], size: int
    ) -> None:
        self.fetch = {
            "remote": remote,
            "local": cached,
            "kind": kind,
            "done": 0,
            "total": size,
        }
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
                rows = [
                    r
                    for r in rows
                    if r[exact] is not None and str(r[exact]).lower() == whole
                ]
            elif value and column in names:
                rows = [
                    r
                    for r in rows
                    if r[column] is not None and value in str(r[column]).lower()
                ]
            else:
                rows = [
                    r
                    for r in rows
                    if any(
                        r[n] is not None and term in str(r[n]).lower() for n in names
                    )
                ]
        for column, ascending in reversed(self.sort_slices[table]):
            filled = sorted(
                (r for r in rows if r[column] is not None),
                key=lambda r: r[column],
                reverse=not ascending,
            )
            rows = filled + [r for r in rows if r[column] is None]
        self.order_slices[table] = rows
        self.picked[table] &= {r["id"] for r in rows}

    def reload_slices(self) -> None:
        """Reread the slice tables, keeping filters, sorts and picks."""
        self.version_rows += 1
        for table in COLUMNS:
            self.rows_slices[table] = self.slices.rows(table)
            self.order_rows(table=table)

    def files_of(
        self, table: str, row: Optional[dict], slice_: Optional[dict]
    ) -> tuple[list[dict], set[str], str]:
        """The picked row's files, the paths its patient's slices claim, and a heading; cached until anything changes."""
        key = (self.version_rows, table, None if row is None else row["id"])
        if self.cache_files[0] == key:
            return self.cache_files[1]
        claimed: set[str] = set()
        files: list[dict] = []
        heading = ""
        if slice_ is not None:
            files = match_files(self.files_index, slice_["file_match"] or "")
            heading = (
                f"{len(files)} files match {slice_['file_match']}"
                if slice_["file_match"]
                else "no file_match set"
            )
        elif row is not None and row["collection_date"]:
            for s in self.rows_slices["slices"]:
                if s["patient_id"] == row["patient_id"]:
                    claimed |= {
                        f["path"]
                        for f in match_files(self.files_index, s["file_match"] or "")
                    }
            files = [
                f
                for f in self.files_index
                if row["collection_date"] in file_dates(f["path"])
            ]
            heading = f"{len(files)} files dated {row['collection_date']}, {sum(f['path'] not in claimed for f in files)} matching no slice"
        self.cache_files = (key, (files, claimed, heading))
        return files, claimed, heading

    def start_edit(
        self,
        table: str,
        row: dict,
        column: int,
        seed: Optional[str] = None,
        x_click: Optional[float] = None,
    ) -> None:
        """
        Edit a cell, or say in the footer why it cannot be edited. seed replaces its text, as typing over a cell in
        Excel does; otherwise the caret goes where the cell was double-clicked (x_click), or at the end. A choice cell
        opens its list instead, filtered by seed.
        """
        name, kind, _ = COLUMNS[table][column]
        if kind == "joined":
            self.message, self.failed = (
                f"{name} comes from the patient; edit it on Patients",
                True,
            )
        elif kind == "owner":
            self.message, self.failed = "owner cannot be changed", True
        elif not can_edit(row["owner"], self.user_slices):
            self.message, self.failed = (
                f"this row is {row['owner']}'s; {locked(row['owner'])}",
                True,
            )
        else:
            self.cell = self.editing = (table, row["id"], name)
            self.text_before = "" if row[name] is None else str(row[name])
            self.text_edit = self.text_before if seed is None else seed
            self.focus_edit = True
            self.x_click = x_click
            self.chars_held = ""
            self.open_choice = kind in ("choice", "ref")
            self.filter_choice = seed or ""
            # Delete highlights (none), typing the first match, and otherwise the value there
            self.pick_choice = None if seed else self.text_edit

    def commit_edit(self, text: str) -> None:
        """Save the edited cell unless it is unchanged; errors show in the footer."""
        table, id_, column = self.editing
        self.editing = None
        if text.strip() == self.text_before:
            return
        try:
            self.slices.update(table, id_, column, text, self.user_slices)
        except ValueError as e:
            self.message, self.failed = str(e), True
            return
        self.message, self.failed = f"{column} set to {text.strip() or 'empty'}", False
        self.reload_slices()

    def move_cell(self, table: str, rows: int, columns: int) -> None:
        """Move the outlined cell by rows and columns as the table shows them, picking its row alone, and scroll to it."""
        ids = [r["id"] for r in self.order_slices[table]]
        names = [name for name, _, _ in COLUMNS[table]]
        if self.cell is None or self.cell[1] not in ids:
            return
        shown = self.shown_slices or list(range(len(names)))
        column = names.index(self.cell[2])
        at = shown.index(column) if column in shown else 0
        id_ = ids[min(max(ids.index(self.cell[1]) + rows, 0), len(ids) - 1)]
        self.cell = (
            table,
            id_,
            names[shown[min(max(at + columns, 0), len(shown) - 1)]],
        )
        self.picked[table].clear()
        self.picked[table].add(id_)
        self.anchor[table] = id_
        self.scroll_cell = True

    def keys_cell(self, table: str) -> None:
        """
        Excel's keys on the outlined cell, unless a widget or popup has them: arrows, Tab and Enter move it (Shift goes
        back), F2 edits it, Delete or Backspace edits it empty, typing replaces it, and Escape lets it go.
        """
        if (
            self.cell is None
            or self.editing is not None
            or imgui.is_any_item_active()
            or imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id)
        ):
            return
        if not imgui.is_window_focused(imgui.FocusedFlags_.root_and_child_windows):
            return
        names = [name for name, _, _ in COLUMNS[table]]
        row = next(
            (r for r in self.order_slices[table] if r["id"] == self.cell[1]), None
        )
        if row is None or names.index(self.cell[2]) not in self.shown_slices:
            self.cell = None
            return
        io = imgui.get_io()
        back = -1 if io.key_shift else 1
        moves = {
            imgui.Key.up_arrow: (-1, 0),
            imgui.Key.down_arrow: (1, 0),
            imgui.Key.left_arrow: (0, -1),
            imgui.Key.right_arrow: (0, 1),
            imgui.Key.tab: (0, back),
            imgui.Key.enter: (back, 0),
            imgui.Key.keypad_enter: (back, 0),
        }
        move = next(
            (step for key, step in moves.items() if imgui.is_key_pressed(key)), None
        )
        # Ctrl drops typed characters, as imgui's text boxes do, except with Alt for AltGr
        typed = (
            ""
            if (io.key_ctrl and not io.key_alt) or io.key_super
            else "".join(
                chr(c) for c in io.input_queue_characters if c >= 32 and c != 127
            )
        )
        column = names.index(self.cell[2])
        if imgui.is_key_pressed(imgui.Key.escape):
            self.cell = None
        elif move is not None:
            self.move_cell(table, *move)
        elif imgui.is_key_pressed(imgui.Key.f2):
            self.start_edit(table, row, column)
        elif not io.key_ctrl and (
            imgui.is_key_pressed(imgui.Key.delete)
            or imgui.is_key_pressed(imgui.Key.backspace)
        ):
            self.start_edit(table, row, column, seed="")
        elif typed:
            self.start_edit(table, row, column, seed=typed)
            io.input_queue_characters.clear()
        self.scroll_cell = self.scroll_cell or self.editing is not None

    def hold_chars(self, label: str) -> None:
        """
        Hold what is typed while the edit box label waits for focus, for place_caret to add: imgui focuses it a frame
        late, and would drop those characters or type them over the selection.
        """
        if imgui.internal.get_active_id() != imgui.get_id(label):
            io = imgui.get_io()
            self.chars_held += "".join(
                chr(c) for c in io.input_queue_characters if c >= 32 and c != 127
            )
            io.input_queue_characters.clear()

    def place_caret(self, data: imgui.InputTextCallbackData) -> int:
        """When an edit box opens, put the caret at caret_edit, else at the end, with nothing selected, and add what was held."""
        if data.event_activated:
            end = data.buf_text_len
            data.cursor_pos = data.selection_start = data.selection_end = (
                end if self.caret_edit is None else min(self.caret_edit, end)
            )
            if self.chars_held:
                data.insert_chars(data.cursor_pos, self.chars_held)
            self.caret_edit, self.chars_held = None, ""
        return 0

    def draw_choices(self) -> None:
        """
        A choice or ref cell's list, dropped from the cell: typing filters it, Up and Down move through it, Enter or
        Tab picks and moves on as in the table, a click picks, and Escape keeps what was there.
        """
        em = imgui.get_font_size()
        table, _, name = self.editing
        _, kind, choices = next(
            column for column in COLUMNS[table] if column[0] == name
        )
        # a choice cell can be cleared; a ref cell cannot
        options = (
            [r[name] for r in self.rows_slices[choices]]
            if kind == "ref"
            else ["", *choices]
        )
        width = max(
            12 * em,
            self.rect_choice.get_width() if self.rect_choice else 0.0,
            *(imgui.calc_text_size(o).x + 2 * em for o in options),
        )
        if imgui.is_window_appearing():
            imgui.set_keyboard_focus_here()
        self.hold_chars("##filter_choice")
        imgui.set_next_item_width(width)
        _, self.filter_choice = imgui.input_text_with_hint(
            "##filter_choice",
            "type to filter",
            self.filter_choice,
            imgui.InputTextFlags_.callback_always,
            self.place_caret,
        )
        typed = self.filter_choice.strip().lower()
        shown = [o for o in options if typed in (o or "(none)").lower()]
        if imgui.is_item_edited() or self.pick_choice not in shown:
            self.pick_choice = next(
                (o for o in shown if (o or "(none)").lower().startswith(typed)),
                shown[0] if shown else None,
            )
        step = int(imgui.is_key_pressed(imgui.Key.down_arrow)) - int(
            imgui.is_key_pressed(imgui.Key.up_arrow)
        )
        if step and self.pick_choice in shown:
            self.pick_choice = shown[
                min(max(shown.index(self.pick_choice) + step, 0), len(shown) - 1)
            ]
        clicked = None
        with imgui_ctx.begin_child(
            "##options",
            imgui.ImVec2(
                width,
                max(min(len(shown), 12), 1) * imgui.get_text_line_height_with_spacing(),
            ),
        ):
            if not shown:
                imgui.text_disabled("no match")
            for option in shown:
                # the value there now stands out
                if option == self.text_before:
                    imgui.push_style_color(imgui.Col_.text, ACCENT)
                if imgui.selectable(
                    f"{option or '(none)'}##{option}", option == self.pick_choice
                )[0]:
                    clicked = option
                if option == self.text_before:
                    imgui.pop_style_color()
                if step and option == self.pick_choice:
                    imgui.set_scroll_here_y()
        back = -1 if imgui.get_io().key_shift else 1
        enter = imgui.is_key_pressed(imgui.Key.enter) or imgui.is_key_pressed(
            imgui.Key.keypad_enter
        )
        tab = imgui.is_key_pressed(imgui.Key.tab)
        if imgui.is_key_pressed(imgui.Key.escape):
            self.editing = None
            imgui.close_current_popup()
        elif clicked is not None or ((enter or tab) and self.pick_choice is not None):
            self.commit_edit(text=self.pick_choice if clicked is None else clicked)
            if clicked is None:
                self.move_cell(table, back if enter else 0, 0 if enter else back)
            imgui.close_current_popup()

    def select(self, name: str) -> None:
        self.selected = name
        self.sidecar = ""
        names = {e.name for e in self.entries}
        if f"{name}{SIDECAR_SUFFIX}" in names:
            self.submit(
                "sidecar",
                self.storage.read_text,
                f"{self.path_browse.rstrip('/')}/{name}{SIDECAR_SUFFIX}",
            )

    def poll(self) -> None:
        """Handle finished workers and file dialogs."""
        for name, future in list(self.futures.items()):
            if not future.done():
                continue
            del self.futures[name]
            error = future.exception()
            if name == "mounts":
                self.mounts = [] if error is not None else future.result()
                unset = (
                    self.settings.backend == "mounted"
                    and not self.settings.mount_root.strip()
                )
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
                    self.load_lab4()
                else:
                    self.db = None
                    self.error_login = f"sign-in failed: {error}"
            elif name == "refresh" and error is None:
                self.schema = future.result()
                self.look_up_subject()
                self.load_lab4()
            elif name == "lab4_rows":
                if error is None:
                    self.lab4 = future.result()
                    self.registered = {
                        row["data_path"]: row for row in self.lab4["recording"]
                    }
                    self.follow_lab4()
                else:
                    self.message, self.failed = f"cannot read Lab4: {error}", True
                self.saved_lab4 = False
            elif name == "lab4_sidecar":
                self.sidecar_lab4 = None if error is not None else future.result()
                self.error_sidecar_lab4 = "" if error is None else str(error)
            elif name == "lab4_save":
                self.failed = error is not None
                if error is None:
                    self.message = "saved to Lab4"
                    if self.sidecar_pending is not None:
                        self.message += (
                            f"; rewriting {posixpath.basename(self.sidecar_pending[0])}"
                        )
                        self.submit(
                            "lab4_rewrite",
                            write_text,
                            self.storage,
                            *self.sidecar_pending,
                        )
                    self.saved_lab4 = True
                    self.load_lab4()
                else:
                    self.message = f"saving failed: {error}"
                self.sidecar_pending = None
            elif name == "lab4_rewrite":
                self.failed = error is not None
                self.message = (
                    "saved to Lab4 and rewrote the sidecar"
                    if error is None
                    else f"saved to Lab4, but rewriting the sidecar failed: {error}"
                )
                if self.picked_lab4 is not None:
                    self.submit(
                        "lab4_sidecar",
                        read_sidecar,
                        self.storage,
                        f"{self.picked_lab4['recording']['data_path']}{SIDECAR_SUFFIX}",
                    )
            elif name == "lab4_add":
                self.failed = error is not None
                self.message = (
                    "added the injection to Lab4"
                    if error is None
                    else f"adding failed: {error}"
                )
                if error is None:
                    self.injection = {}
                    self.load_lab4()
            elif name == "subject":
                self.subject_recordings = [] if error is not None else future.result()
            elif name == "register":
                if error is None:
                    added = [
                        table.replace("_", " ")
                        for table, new in future.result().items()
                        if new and table != "recording"
                    ]
                    self.message = f"registered {self.recording.label()} in Lab4" + (
                        f", with a new {', '.join(added)}" if added else ""
                    )
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
                self.message = (
                    f"scan failed: {error}"
                    if found is None
                    else f"scanned {found['root']}: {found['datasets']} datasets in {found['records']} items ({found['seconds']:.1f} s)"
                )
                self.failed = found is None
                self.describe()
                # its runs and results show on Results once linked
                self.submit("link", self.catalog.link)
            elif name == "link":
                self.watched = 0.0
            elif name == "scan_machines":
                found, self.errors_scan = (
                    ([], [str(error)]) if error is not None else future.result()
                )
                runs = sum(scan["runs"] for scan in found)
                self.message = f"scanned {len(found)} folder(s), {runs} runs" + (
                    f"; {len(self.errors_scan)} not reachable"
                    if self.errors_scan
                    else ""
                )
                self.failed = bool(self.errors_scan) and not found
                self.scanning = ("", 0)
                self.watched = 0.0
            elif name == "overview":
                if error is None:
                    self.overview = future.result()
                else:
                    self.message, self.failed = (
                        f"cannot read the catalog: {error}",
                        True,
                    )
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
                    self.message, self.failed = (
                        f"cannot read the sources: {error}",
                        True,
                    )
            elif name == "download":
                if error is None:
                    self.open_local(local=self.fetch["local"], kind=self.fetch["kind"])
                else:
                    self.message, self.failed = f"copy failed: {error}", True
                self.fetch = None
            elif name == "sidecar":
                self.sidecar = "" if error is not None else future.result()
            elif name == "attach":
                self.message = (
                    f"metadata attached to {self.selected}"
                    if error is None
                    else f"attach failed: {error}"
                )
                self.failed = error is not None
                self.submit("list", self.storage.listdir, self.path_browse)
            elif name == "transfer":
                self.message = (
                    f"sent {len(self.transfer.sources)} item(s) to {self.transfer.dest}"
                    if error is None
                    else str(error)
                )
                self.failed = error is not None
                self.dest_checked = ""
                if error is None:
                    self.transfer = None
                    self.browse(path=self.path_browse)
                if error is None and self.pending:
                    self.message += (
                        f"; submitting {len(self.pending)} processing job(s)"
                    )
                    self.submit(
                        "process",
                        processing.submit,
                        self.run_remote,
                        copy.deepcopy(self.settings),
                        self.pending,
                        self.pending_params,
                        self.storage,
                    )
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
                keys = {
                    (local, unit.key)
                    for local, _, units in self.mesc
                    if isinstance(units, list)
                    for unit in units
                }
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
                self.picker_entries = (
                    []
                    if error is not None
                    else [e for e in future.result() if e.is_dir]
                )
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
                    self.message, self.failed = (
                        f"wrote {len(self.rows_slices[table])} {table} to {chosen}",
                        False,
                    )
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
            jobs += [
                Job(local, mesc, unit.key, processing.dir_save(mesc, unit.key, when))
                for unit in units
                if (local, unit.key) in self.chosen
            ]
        return jobs

    def submit_waiting(self) -> None:
        """Submit jobs that failed to submit earlier."""
        waiting = [job for job in self.jobs if job["job_id"] is None]
        if waiting and "process" not in self.futures:
            self.message = ""
            self.submit(
                "process",
                processing.submit_waiting,
                self.run_remote,
                copy.deepcopy(self.settings),
                waiting,
                self.storage,
            )

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
            self.picker = pfd.open_file(
                "Add files", start, ["All Files", "*"], pfd.opt.multiselect
            )

    def destination(self) -> str:
        """The destination folder, without a trailing slash."""
        dest = self.dest.strip()
        return dest.rstrip("/") or dest

    def check_destination(self) -> None:
        """Check the destination once each time it changes."""
        dest = self.destination()
        if (
            dest.startswith("/")
            and dest != self.dest_checked
            and "dest_check" not in self.futures
        ):
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
        start = (
            dest
            if self.dest_state == "exists"
            else posixpath.dirname(dest)
            if dest.startswith("/")
            else ""
        )
        self.list_picker(
            start
            or (self.settings.destinations[0][0] if self.settings.destinations else "/")
        )
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
                found.append(
                    "destination must be a BioHPC path, e.g. /biohpc/data2/M231"
                )
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
        elif self.page == "recordings":
            if self.schema is None:
                found.append("sign in to Lab4 to edit its recordings")
            elif self.picked_lab4 is None:
                found.append("pick a recording")
            else:
                if "lab4_save" in self.futures:
                    found.append("saving...")
                changes = self.changes_lab4()
                if not changes:
                    found.append("nothing changed")
                for table, _, values in changes:
                    found += self.schema.edit_problems(table, values)
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
            return label + (
                f", process {len(self.chosen)} unit{'' if len(self.chosen) == 1 else 's'}"
                if self.process
                else ""
            )
        if self.page == "browse":
            return f"{fa.ICON_FA_TAGS}  Attach metadata"
        if self.page == "recordings":
            return f"{fa.ICON_FA_FLOPPY_DISK}  Save to Lab4"
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
        transfer = Transfer(
            self.storage,
            [source for source, _ in sized],
            dest,
            metadata,
            self.overwrite,
        )
        pending = self.jobs_to_send() if self.process else []
        if pending:
            lines.append(
                f"Then submits {len(pending)} processing job(s) on {self.settings.partition}."
            )
        args = (transfer, pending, copy.deepcopy(self.params))
        self.confirm = {
            "title": "Send to BioHPC?",
            "lines": lines,
            "button": "Send",
            "run": self.send,
            "args": args,
        }

    def act(self) -> None:
        self.message = ""
        if self.page == "transfer":
            # a big folder takes a while to measure, so the dialog opens once "sizes" lands
            self.submit("sizes", measure, [Path(s) for s in self.sources])
        elif self.page == "browse":
            path = f"{self.path_browse.rstrip('/')}/{self.selected}{SIDECAR_SUFFIX}"
            text = json.dumps(
                {
                    **self.recording.sidecar(user=self.user),
                    "source": f"{self.path_browse.rstrip('/')}/{self.selected}",
                },
                indent=2,
            )
            lines = [
                f"write {path}",
                f"on {self.storage.label}. {self.selected} is not changed.",
            ]
            if f"{self.selected}{SIDECAR_SUFFIX}" in {e.name for e in self.entries}:
                lines.append("The existing sidecar is replaced.")
            self.confirm = {
                "title": "Attach metadata?",
                "lines": lines,
                "button": "Write",
                "run": self.submit,
                "args": ("attach", write_text, self.storage, path, text),
            }
        elif self.page == "recordings":
            changes = self.changes_lab4()
            lines = [f"change in Lab4 ({self.db.credentials.label()}):"]
            for table, _, values in changes:
                row = self.picked_lab4[table]
                lines += [
                    f"  {table} {name.replace('_', ' ')}: {lab4.text_of(row[name])!r} -> {text.strip()!r}"
                    for name, text in values.items()
                ]
            shared = [table for table, _, _ in changes if table != "recording"]
            if shared:
                lines.append(
                    f"Every recording of this {' and '.join(shared)} shows the change."
                )
            sidecar = self.sidecar_rewrite()
            if sidecar is not None:
                lines.append(f"then rewrite {sidecar[0]} on {self.storage.label}")
            self.confirm = {
                "title": "Save to Lab4?",
                "lines": lines,
                "button": "Save",
                "run": self.save_lab4,
                "args": (changes, sidecar),
            }
        elif self.page == "settings":
            self.settings.save()
            self.connect()
            self.message = f"saved to {data_dir() / 'settings.json'}"
        elif self.page == "slices" and self.saver is None:
            # the save dialog asks before overwriting
            self.table_export = self.tab_slices
            self.saver = pfd.save_file(
                f"Export {self.tab_slices}",
                str(Path.home() / f"{self.tab_slices}.csv"),
                ["CSV", "*.csv"],
            )

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
            elif (
                self.settings.backend == "mounted"
                and not self.settings.mount_root.strip()
            ):
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
                lab = (
                    f"Lab4: {self.db.credentials.label()}"
                    if self.db
                    else "Lab4: offline"
                )
                idl.wrapped_tooltip(
                    f"{lab}\n{self.storage.label}"
                    + (f": {self.status}" if self.status else "")
                )
        draw_wrapped("Move PML data to BioHPC with Lab4 metadata", DIM)

    def draw_body(self) -> None:
        """The sign-in card, or the rail and current page. Esc goes back to Overview, once no slice cell is outlined."""
        self.poll()
        if not self.user:
            self.draw_login()
            return
        outlined = self.page == "slices" and self.cell is not None
        if (
            imgui.is_key_pressed(imgui.Key.escape)
            and not imgui.is_any_item_active()
            and not imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id)
            and not outlined
        ):
            self.page = "home"
        em = imgui.get_font_size()
        avail = imgui.get_content_region_avail()
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        with imgui_ctx.begin_child(
            "##rail",
            imgui.ImVec2(WIDTH_RAIL_EM * em, avail.y),
            0,
            imgui.WindowFlags_.no_scrollbar,
        ):
            self.draw_rail()
        x = p.x + WIDTH_RAIL_EM * em + 0.4 * em
        dl.add_line(
            imgui.ImVec2(x, p.y),
            imgui.ImVec2(x, p.y + avail.y),
            imgui.color_convert_float4_to_u32(EDGE),
            1.0,
        )
        imgui.same_line(0, 0.8 * em)
        imgui.push_style_var(
            imgui.StyleVar_.window_padding, imgui.ImVec2(1.2 * em, 0.6 * em)
        )
        with imgui_ctx.begin_child(
            "##content",
            imgui.ImVec2(0, avail.y),
            imgui.ChildFlags_.always_use_window_padding,
        ):
            self.draw_content()
        imgui.pop_style_var()
        if self.confirm is not None and not imgui.is_popup_open("###confirm"):
            imgui.open_popup("###confirm")
        if imgui.begin_popup_modal(
            f"{self.confirm['title'] if self.confirm else ''}###confirm",
            None,
            imgui.WindowFlags_.always_auto_resize,
        )[0]:
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
                if (
                    go
                    or imgui.button("Cancel", imgui.ImVec2(8 * em, 0))
                    or imgui.is_key_pressed(imgui.Key.escape)
                ):
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
        imgui.push_style_var(
            imgui.StyleVar_.window_padding, imgui.ImVec2(1.2 * em, 1.0 * em)
        )
        flags = (
            imgui.ChildFlags_.borders
            | imgui.ChildFlags_.auto_resize_y
            | imgui.ChildFlags_.always_use_window_padding
        )
        with imgui_ctx.begin_child(
            "##login", imgui.ImVec2(width, 0), flags, imgui.WindowFlags_.no_scrollbar
        ):
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
                draw_wrapped(
                    "Download your credentials from the Lab4 workspace and save them here.",
                    DIM,
                )
            else:
                try:
                    credentials = Credentials.load(path)
                except (OSError, ValueError, KeyError) as e:
                    draw_wrapped(f"unreadable: {e}", ERROR)
                else:
                    local = credentials.host in ("127.0.0.1", "localhost", "::1")
                    draw_wrapped(
                        f"signs in as {credentials.label()}"
                        + ("" if local else "  (remote server)"),
                        DIM if local else WARN,
                    )
            if self.error_login:
                draw_wrapped(
                    f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.error_login}", ERROR
                )
            imgui.dummy(imgui.ImVec2(0, 0.4 * em))
            signing_in = "sign_in" in self.futures
            imgui.begin_disabled(signing_in or not path.is_file())
            idl.push_button_style(THEME, primary=True)
            if imgui.button(
                "Signing in..." if signing_in else "Sign In",
                imgui.ImVec2(full, 1.9 * em),
            ):
                self.sign_in()
            idl.pop_button_style()
            imgui.end_disabled()
            imgui.begin_disabled(signing_in)
            if imgui.button("Work offline", imgui.ImVec2(full, 1.7 * em)):
                self.work_offline()
            imgui.end_disabled()
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(
                    "transfers and sidecars work without Lab4; registering does not"
                )
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
            imgui.push_style_color(
                imgui.Col_.button_hovered, LIT if lit else CARD_HOVER
            )
            imgui.push_style_color(imgui.Col_.button_active, LIT)
            imgui.push_style_color(imgui.Col_.text, ACCENT if lit else TEXT)
            if imgui.button(
                f"{icon}   {name}##rail_{key}", imgui.ImVec2(width, 2.2 * em)
            ):
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
                if imgui.button(
                    f"{fa.ICON_FA_CLOCK_ROTATE_LEFT}  {Path(path).name or path}##recent_{i}",
                    imgui.ImVec2(width, 1.7 * em),
                ):
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
        draw_wrapped(hint, DIM)
        imgui.separator()
        imgui.dummy(imgui.ImVec2(0, 0.4 * em))
        if key == "home":
            self.draw_home()
        elif key == "transfer":
            self.draw_transfer()
        elif key == "browse":
            self.draw_browse()
        elif key == "recordings":
            self.draw_lab4()
        elif key == "results":
            self.draw_results()
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
        dl.add_text(
            imgui.ImVec2(center.x - size.x / 2, center.y - size.y / 2),
            imgui.color_convert_float4_to_u32(ACCENT),
            str(n),
        )
        imgui.dummy(imgui.ImVec2(2 * r, 2 * r))
        imgui.same_line(0, 0.6 * em)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 0.25 * em)
        imgui.text_colored(ACCENT, title)
        draw_hint(text=hint)
        imgui.dummy(imgui.ImVec2(0, 0.1 * em))

    def begin_form(self, name: str) -> bool:
        """Start a two-column form (caption, widget); close it with end_table."""
        if not imgui.begin_table(
            f"##form_{name}", 2, imgui.TableFlags_.sizing_stretch_prop
        ):
            return False
        imgui.table_setup_column(
            "##caption",
            imgui.TableColumnFlags_.width_fixed,
            WIDTH_CAPTION_EM * imgui.get_font_size(),
        )
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
        imgui.set_next_item_width(
            imgui.get_content_region_avail().x - imgui.get_frame_height() - spacing
        )
        _, value = imgui.input_text_with_hint(label, hint, value)
        imgui.same_line(0, spacing)
        if imgui.begin_combo(
            f"{label}_pick",
            "",
            imgui.ComboFlags_.no_preview | imgui.ComboFlags_.popup_align_left,
        ):
            for item in items:
                if imgui.selectable(item, item == value)[0]:
                    value = item
            imgui.end_combo()
        return value

    def lookup(self, label: str, value: str, column: str, hint: str) -> str:
        """A combo over the column's Lab4 lookup values, or a text field offline."""
        items = self.schema.choices.get(column, []) if self.schema else []
        return (
            self.choice(label, value, items)
            if items
            else imgui.input_text_with_hint(label, hint, value)[1]
        )

    def draw_home(self) -> None:
        """Page cards, links, destinations and the files a transfer writes."""
        em = imgui.get_font_size()
        pages = [
            page
            for page in PAGES
            if page[0] in ("transfer", "browse", "recordings", "results", "slices")
        ]
        i = draw_cards([(icon, name, hint) for key, icon, name, hint in pages])
        if i is not None:
            self.page = pages[i][0]
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        draw_wrapped(
            "Transfer copies data to BioHPC with a metadata sidecar beside it. Browse storage opens datasets, adds "
            "sidecars to data already there, and registers .h5 files in Lab4. Results lists each recording's units "
            "with the pipeline runs and figures found on your machines. Human slices tracks each slice from patient "
            "to assay (demo data).",
            DIM,
        )
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_LINK}  Links")
        draw_hint("opens in your browser")
        i = draw_cards(
            [(icon, name, f"{hint}\n{url}") for icon, name, url, hint in LINKS]
        )
        if i is not None:
            webbrowser.open(LINKS[i][2])
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FOLDER_OPEN}  Destinations")
        draw_hint("edit these in Settings")
        if imgui.begin_table("##destinations", 2, flags):
            imgui.table_setup_column(
                "path", imgui.TableColumnFlags_.width_fixed, 15 * em
            )
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
            imgui.table_setup_column(
                "file", imgui.TableColumnFlags_.width_fixed, 15 * em
            )
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
        self.step(
            2,
            "Destination",
            "a BioHPC folder; a new one is made when its parent exists",
        )
        self.check_destination()
        dest = self.destination()
        spacing = imgui.get_style().item_inner_spacing.x
        label_browse = f"{fa.ICON_FA_FOLDER_OPEN}  Browse..."
        imgui.set_next_item_width(
            imgui.get_content_region_avail().x
            - imgui.calc_text_size(label_browse).x
            - 2 * imgui.get_style().frame_padding.x
            - spacing
        )
        _, self.dest = imgui.input_text_with_hint(
            "##dest", "/biohpc/data2/... (new folders are made)", self.dest
        )
        imgui.same_line(0, spacing)
        if imgui.button(label_browse):
            self.open_destination_picker()
        presets = {
            path.rstrip("/"): description
            for path, description in self.settings.destinations
        }
        if not dest:
            draw_wrapped("type a BioHPC folder or browse for one", DIM)
        elif not dest.startswith("/") or self.dest_checked != dest:
            pass
        elif self.dest_state is None:
            imgui.text_disabled("checking...")
        elif self.dest_state == "exists":
            draw_wrapped(
                f"{fa.ICON_FA_FOLDER}  goes to {dest}"
                + (f"  ·  {presets[dest]}" if dest in presets else ""),
                PATH,
            )
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
        self.step(
            4, "Processing", "run the ASAP7 spine pipeline on BioHPC after the transfer"
        )
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
            flags = (
                imgui.TableFlags_.row_bg
                | imgui.TableFlags_.borders_inner_h
                | imgui.TableFlags_.sizing_stretch_prop
            )
            if runnable and imgui.begin_table(f"##units_{local}", 5, flags):
                imgui.table_setup_column(
                    "##pick", imgui.TableColumnFlags_.width_fixed, 1.6 * em
                )
                imgui.table_setup_column(
                    "unit", imgui.TableColumnFlags_.width_fixed, 11 * em
                )
                imgui.table_setup_column(
                    "frames", imgui.TableColumnFlags_.width_fixed, 5 * em
                )
                imgui.table_setup_column(
                    "length", imgui.TableColumnFlags_.width_fixed, 8 * em
                )
                imgui.table_setup_column(
                    "comment", imgui.TableColumnFlags_.width_stretch
                )
                imgui.table_headers_row()
                for unit in runnable:
                    key = (local, unit.key)
                    imgui.table_next_row()
                    imgui.table_next_column()
                    ticked, on = imgui.checkbox(
                        f"##{local}_{unit.key}", key in self.chosen
                    )
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
                imgui.text_disabled(
                    f"{hidden} unit(s) hidden: not a movie, or too few frames"
                )
        self.subsection(
            "Pipeline",
            "mbo asap7 (asap7_spine_pipeline_detrend.ipynb); defaults match the notebook",
        )
        params = self.params
        if self.begin_form("asap7"):
            self.row("channel")
            _, params.channel = imgui.input_int("##channel", params.channel)
            self.row("first frame")
            _, params.first_frame = imgui.input_int(
                "##first_frame", params.first_frame, 50
            )
            self.row("MAD threshold")
            _, params.mad_correlation_threshold = imgui.input_float(
                "##mad", params.mad_correlation_threshold, 0.05, 0.1, "%.2f"
            )
            self.row("detrend window")
            _, params.detrend_window_s = imgui.input_float(
                "##detrend", params.detrend_window_s, 0.05, 0.1, "%.2f s"
            )
            imgui.end_table()
        partition = self.settings.partition or "(no partition set)"
        draw_wrapped(
            f"One job per unit on {partition} via ssh {self.settings.processing_host}, after the transfer finishes. "
            f"Results go to <folder>/{processing.NAME_RESULTS}/<unit>_detrend_<time> beside the .mesc."
            + (
                f" Emails {self.settings.email} when each ends."
                if self.settings.email
                else ""
            ),
            DIM,
        )

    def draw_jobs(self) -> None:
        """Recent jobs and their SLURM state."""
        if not self.jobs:
            return
        em = imgui.get_font_size()
        self.subsection(
            "Submitted",
            f"last {NUMBER_JOBS_SHOWN} jobs from this machine, newest first",
        )
        imgui.same_line()
        imgui.begin_disabled("states" in self.futures)
        if imgui.small_button(f"{fa.ICON_FA_ARROWS_ROTATE}  Check"):
            self.check_jobs()
        imgui.end_disabled()
        waiting = sum(job["job_id"] is None for job in self.jobs)
        if waiting:
            imgui.same_line()
            imgui.begin_disabled(
                "process" in self.futures or not self.settings.partition.strip()
            )
            if imgui.small_button(f"{fa.ICON_FA_PLAY}  Submit {waiting} waiting"):
                self.submit_waiting()
            imgui.end_disabled()
        flags = (
            imgui.TableFlags_.row_bg
            | imgui.TableFlags_.borders_inner_h
            | imgui.TableFlags_.sizing_stretch_prop
        )
        if imgui.begin_table("##jobs", 4, flags):
            imgui.table_setup_column("job", imgui.TableColumnFlags_.width_fixed, 5 * em)
            imgui.table_setup_column(
                "unit", imgui.TableColumnFlags_.width_fixed, 11 * em
            )
            imgui.table_setup_column(
                "state", imgui.TableColumnFlags_.width_fixed, 10 * em
            )
            imgui.table_setup_column("results", imgui.TableColumnFlags_.width_stretch)
            for job in reversed(self.jobs[-NUMBER_JOBS_SHOWN:]):
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text(job["job_id"] or "-")
                imgui.table_next_column()
                imgui.text(job["unit"])
                imgui.table_next_column()
                state = (
                    self.job_states.get(job["job_id"], "")
                    if job["job_id"]
                    else "waiting to submit"
                )
                color = (
                    OK
                    if state.startswith("COMPLETED")
                    else ERROR
                    if state.split(" ")[0] in STATES_FAILED
                    else WARN
                    if not job["job_id"]
                    else TEXT
                )
                imgui.text_colored(color, state or "not checked")
                imgui.table_next_column()
                imgui.text_disabled(job["dir_save"])
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(
                        f"{job['mesc']}\nchosen {job['chosen_at']}, submitted {job['submitted_at'] or 'not yet'}"
                    )
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
        imgui.set_next_window_size(
            imgui.ImVec2(44 * em, 28 * em), imgui.Cond_.appearing
        )
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
        entered, self.picker_path = imgui.input_text(
            "##picker_path", self.picker_path, imgui.InputTextFlags_.enter_returns_true
        )
        if entered:
            self.list_picker(self.picker_path)
        if "picker" in self.futures:
            imgui.text_disabled("listing...")
        elif self.picker_error:
            draw_wrapped(self.picker_error, ERROR)
        flags_row = imgui.SelectableFlags_.allow_double_click
        if imgui.begin_child(
            "##picker_list", imgui.ImVec2(0, -2.4 * em), imgui.ChildFlags_.borders
        ):
            for entry in self.picker_entries:
                clicked, _ = imgui.selectable(
                    f"{fa.ICON_FA_FOLDER}  {entry.name}",
                    entry.name == self.picker_selected,
                    flags_row,
                )
                if clicked:
                    self.picker_selected = entry.name
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    self.list_picker(f"{self.picker_path.rstrip('/')}/{entry.name}")
            if (
                not self.picker_entries
                and "picker" not in self.futures
                and not self.picker_error
            ):
                imgui.text_disabled("no folders here")
        imgui.end_child()
        chosen = (
            f"{self.picker_path.rstrip('/')}/{self.picker_selected}"
            if self.picker_selected
            else self.picker_path
        )
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
            rec.experimenter = self.lookup(
                "##experimenter", rec.experimenter, "experimenter", "full name"
            )
            self.row("experiment group")
            groups = schema.groups_of(rec.experimenter.strip()) if schema else []
            rec.collection_name = self.pick(
                "##group",
                rec.collection_name,
                groups,
                "study or cohort; new ones are added",
            )
            self.row("subject")
            subject_before = rec.subject_id
            rec.subject_id = self.pick(
                "##subject",
                rec.subject_id,
                schema.subjects if schema else [],
                "mouse id",
            )
            if rec.subject_id != subject_before:
                self.look_up_subject()
            self.row("session start")
            _, rec.session_datetime = imgui.input_text_with_hint(
                "##when", "YYYY-MM-DD HH:MM", rec.session_datetime
            )
            self.row("device")
            rec.device_name = self.lookup(
                "##device", rec.device_name, "device_name", "rig"
            )
            imgui.end_table()
        problems = rec.problems() + (schema.problems(rec) if schema else [])
        for problem in problems:
            draw_wrapped(f"{fa.ICON_FA_CIRCLE_INFO}  {problem}", WARN)
        if schema is None:
            draw_wrapped("offline: not checked against Lab4", DIM)
        elif rec.subject_id.strip() in schema.subjects:
            n = len(self.subject_recordings)
            draw_wrapped(
                f"{rec.subject_id.strip()} is in Lab4 with {n} recording{'' if n == 1 else 's'}",
                DIM,
            )
            for row in self.subject_recordings[:5]:
                imgui.text_disabled(
                    f"    {row['session_datetime']}  {row['device_name']}  {row['data_path']}"
                )

        self.subsection("Details", "saved with the recording")
        if self.begin_form("details"):
            self.row("label")
            _, rec.ui_label = imgui.input_text_with_hint(
                "##label", "optional", rec.ui_label
            )
            self.row("weight (g)")
            _, rec.weight_g = imgui.input_text_with_hint(
                "##weight", "optional", rec.weight_g
            )
            self.row("notes")
            _, rec.notes = imgui.input_text_multiline(
                "##notes", rec.notes, imgui.ImVec2(-1, 3.5 * em)
            )
            imgui.end_table()

        if (
            schema is not None
            and rec.subject_id.strip()
            and rec.subject_id.strip() not in schema.subjects
            and schema.subject_fields()
        ):
            self.subsection(
                "New subject", f"{rec.subject_id.strip()} is new; registering adds it"
            )
            if self.begin_form("subject"):
                for column in schema.subject_fields():
                    self.row(
                        column.name.replace("_", " ")
                        + ("" if column.required else " (optional)")
                    )
                    value = rec.subject_fields.get(column.name, "")
                    rec.subject_fields[column.name] = self.pick(
                        f"##subject_{column.name}",
                        value,
                        schema.choices.get(column.name, []),
                        "YYYY-MM-DD" if column.type == "date" else "",
                    )
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
        if not imgui.tree_node(
            f"{fa.ICON_FA_FILE_CODE}  sidecar preview: {rec.label()}{SIDECAR_SUFFIX}###sidecar_preview"
        ):
            return
        text = json.dumps(rec.sidecar(user=self.user), indent=2)
        lines = text.count("\n") + 1
        imgui.push_style_color(imgui.Col_.text, CODE)
        imgui.input_text_multiline(
            "##preview",
            text,
            imgui.ImVec2(-1, lines * imgui.get_text_line_height() + em),
            imgui.InputTextFlags_.read_only,
        )
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
        entered, self.path_browse = imgui.input_text(
            "##path", self.path_browse, imgui.InputTextFlags_.enter_returns_true
        )
        if entered:
            self.browse(path=self.path_browse)

        summary, scan, items = (
            self.described["folder"],
            self.described["scan"],
            self.described["items"],
        )
        if scanning:
            imgui.text_colored(
                ACCENT,
                f"scanning {self.path_browse}... {self.count_scanned} items listed",
            )
        elif scan is None:
            imgui.text_disabled("not scanned yet")
        else:
            kinds = summary["kinds"] if summary else {}
            below = (
                f"{sum(kinds.values())} datasets below: {summarize(kinds)}"
                if kinds
                else "no datasets below"
            )
            size = (
                f" · {human_bytes(summary['size'])} in {summary['n_files']} files"
                if summary
                else ""
            )
            imgui.text_disabled(
                f"scanned {scan['scanned_at'].replace('T', ' ')} · {below}{size}"
            )
        if "list" in self.futures:
            imgui.text_disabled("listing...")
        elif self.error_browse:
            draw_wrapped(self.error_browse, ERROR)

        names = {e.name for e in self.entries}
        flags = (
            imgui.TableFlags_.row_bg
            | imgui.TableFlags_.borders_inner_h
            | imgui.TableFlags_.scroll_y
            | imgui.TableFlags_.resizable
        )
        height = max(8 * em, imgui.get_content_region_avail().y - 12 * em)
        if imgui.begin_table("##entries", 5, flags, imgui.ImVec2(0, height)):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column("name", imgui.TableColumnFlags_.width_stretch, 2.0)
            imgui.table_setup_column(
                "kind", imgui.TableColumnFlags_.width_fixed, 8 * em
            )
            imgui.table_setup_column(
                "holds", imgui.TableColumnFlags_.width_stretch, 1.5
            )
            imgui.table_setup_column(
                "size", imgui.TableColumnFlags_.width_fixed, 5 * em
            )
            imgui.table_setup_column(
                "##tag", imgui.TableColumnFlags_.width_fixed, 3 * em
            )
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
                flags_row = (
                    imgui.SelectableFlags_.span_all_columns
                    | imgui.SelectableFlags_.allow_double_click
                )
                clicked, _ = imgui.selectable(
                    f"{icon}  {entry.name}", entry.name == self.selected, flags_row
                )
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
                registered = self.registered.get(
                    f"{self.path_browse.rstrip('/')}/{entry.name}"
                )
                if f"{entry.name}{SIDECAR_SUFFIX}" in names:
                    imgui.text_colored(OK, fa.ICON_FA_TAGS)
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip("has a sidecar")
                    if registered is not None:
                        imgui.same_line()
                if registered is not None:
                    imgui.text_colored(ACCENT, fa.ICON_FA_DATABASE)
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(
                            f"in Lab4: {registered['subject_id']}, {lab4.text_of(registered['session_datetime'])}, {registered['device_name']}"
                        )
            imgui.end_table()
        imgui.text_disabled("double-click to open a folder or dataset")

        if self.selected:
            item = items.get(self.selected, {})
            imgui.text_colored(ACCENT, f"{fa.ICON_FA_TAGS}  {self.selected}")
            path = f"{self.path_browse.rstrip('/')}/{self.selected}"
            if path in self.registered:
                imgui.same_line()
                if imgui.small_button(f"{fa.ICON_FA_DATABASE}  Show in Lab4"):
                    self.show_in_lab4(path)
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(
                        "this file is a Lab4 recording's data; edit its details on Lab4 recordings"
                    )
            elif is_recording_data(path) and not any(
                e.is_dir for e in self.entries if e.name == self.selected
            ):
                blockers = self.register_problems(path)
                imgui.same_line()
                imgui.begin_disabled(bool(blockers))
                if imgui.small_button(f"{fa.ICON_FA_DATABASE}  Register in Lab4"):
                    self.register(path)
                imgui.end_disabled()
                if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
                    idl.wrapped_tooltip(
                        "\n".join(blockers)
                        if blockers
                        else f"register {self.recording.label()} in Lab4 with this file"
                    )
            if item.get("kind"):
                tool, _, description = KINDS[item["kind"]]
                imgui.same_line()
                if imgui.small_button(f"{fa.ICON_FA_EYE}  Open in {tool}"):
                    self.open_dataset(
                        path=f"{self.path_browse.rstrip('/')}/{self.selected}",
                        kind=item["kind"],
                    )
                imgui.same_line()
                imgui.text_disabled(description)
            if "sidecar" in self.futures:
                imgui.text_disabled("reading metadata...")
            elif self.sidecar:
                if imgui.small_button(
                    f"{fa.ICON_FA_PEN_TO_SQUARE}  Load into the form"
                ):
                    self.load_sidecar()
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(
                        "fill the metadata form below from this sidecar, e.g. to register the recording or attach it elsewhere"
                    )
                imgui.push_style_color(imgui.Col_.text, CODE)
                imgui.input_text_multiline(
                    "##sidecar",
                    self.sidecar,
                    imgui.ImVec2(-1, 10 * em),
                    imgui.InputTextFlags_.read_only,
                )
                imgui.pop_style_color()
            else:
                draw_wrapped("no sidecar yet", DIM)
        imgui.dummy(imgui.ImVec2(0, 0.4 * em))
        if imgui.collapsing_header(
            f"{fa.ICON_FA_TAGS}  Metadata: {self.recording.label()}###browse_metadata"
        ):
            self.draw_recording()

    def draw_lab4(self) -> None:
        """Refresh and filter, a row per Lab4 recording, then the picked one's details to edit."""
        em = imgui.get_font_size()
        if self.schema is None:
            draw_wrapped(
                "Offline: sign in to Lab4 to see its recordings and edit their details.",
                DIM,
            )
            return
        loading = "lab4_rows" in self.futures
        imgui.begin_disabled(loading)
        if imgui.button(f"{fa.ICON_FA_ARROWS_ROTATE}##refresh_lab4"):
            self.load_lab4()
        imgui.end_disabled()
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            idl.wrapped_tooltip("read Lab4 again; edits not saved yet are kept")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        _, self.filter_lab4 = imgui.input_text_with_hint(
            "##filter_lab4",
            "filter by subject, session, device, label, group or data path",
            self.filter_lab4,
        )

        recordings = self.lab4.get("recording", [])
        needle = self.filter_lab4.strip().lower()
        rows = [
            row
            for row in recordings
            if needle in " ".join(lab4.text_of(value) for value in row.values()).lower()
        ]
        picked = (
            self.schema.key("recording", self.picked_lab4["recording"])
            if self.picked_lab4
            else None
        )
        flags = (
            imgui.TableFlags_.row_bg
            | imgui.TableFlags_.borders_inner_h
            | imgui.TableFlags_.scroll_y
            | imgui.TableFlags_.resizable
        )
        # as tall as its rows, up to 40% of the page, so a short list leaves room for the picked recording
        height = max(
            5 * em,
            min(
                0.4 * imgui.get_content_region_avail().y,
                (len(rows) + 1.5) * imgui.get_frame_height(),
            ),
        )
        width_inner = 0.0 if imgui.get_content_region_avail().x >= 40 * em else 40 * em
        if imgui.begin_table(
            "##lab4",
            5,
            flags | imgui.TableFlags_.scroll_x,
            imgui.ImVec2(0, height),
            width_inner,
        ):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column(
                "subject", imgui.TableColumnFlags_.width_fixed, 7 * em
            )
            imgui.table_setup_column(
                "session", imgui.TableColumnFlags_.width_fixed, 8 * em
            )
            imgui.table_setup_column(
                "device", imgui.TableColumnFlags_.width_fixed, 8 * em
            )
            imgui.table_setup_column(
                "label", imgui.TableColumnFlags_.width_stretch, 1.0
            )
            imgui.table_setup_column("data", imgui.TableColumnFlags_.width_stretch, 2.0)
            imgui.table_headers_row()
            clipper = imgui.ListClipper()
            clipper.begin(len(rows))
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    row = rows[i]
                    imgui.table_next_row()
                    imgui.table_next_column()
                    flags_row = (
                        imgui.SelectableFlags_.span_all_columns
                        | imgui.SelectableFlags_.allow_double_click
                    )
                    clicked, _ = imgui.selectable(
                        f"{row['subject_id']}##lab4_{i}",
                        self.schema.key("recording", row) == picked,
                        flags_row,
                    )
                    if clicked:
                        self.pick_lab4(row)
                    if clicked and imgui.is_mouse_double_clicked(
                        imgui.MouseButton_.left
                    ):
                        self.show_in_browse(row["data_path"])
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(
                            f"{row['experimenter']} · {row['collection_name']}\ndouble-click to show its data in Browse storage"
                        )
                    imgui.table_next_column()
                    imgui.text_disabled(lab4.text_of(row["session_datetime"]))
                    imgui.table_next_column()
                    imgui.text(row["device_name"])
                    imgui.table_next_column()
                    imgui.text(lab4.text_of(row.get("ui_label")))
                    imgui.table_next_column()
                    imgui.text_colored(PATH, row["data_path"])
            imgui.end_table()
        if recordings:
            draw_wrapped(f"{len(rows)} of {len(recordings)} recordings", DIM)
        else:
            draw_wrapped(
                "reading Lab4..."
                if loading
                else "no recordings in Lab4 yet; register one from Browse storage",
                DIM,
            )
        if self.picked_lab4 is not None:
            self.draw_lab4_recording()

    def draw_lab4_recording(self) -> None:
        """The picked recording's key and data, then its recording, session and subject columns to edit."""
        row = self.picked_lab4["recording"]
        rec = self.recording_lab4()
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_DATABASE}  {rec.label()}")
        imgui.same_line()
        imgui.text_disabled(f"{row['experimenter']} · {row['collection_name']}")
        if imgui.small_button(f"{fa.ICON_FA_SITEMAP}  Show in Browse"):
            self.show_in_browse(row["data_path"])
        if imgui.is_item_hovered():
            idl.wrapped_tooltip(
                f"list {posixpath.dirname(row['data_path'])} in Browse storage"
            )
        kind = classify_file(posixpath.basename(row["data_path"]))
        if kind is not None:
            imgui.same_line()
            if imgui.small_button(f"{fa.ICON_FA_EYE}  Open in {KINDS[kind][0]}"):
                self.open_dataset(path=row["data_path"], kind=kind)
        imgui.same_line()
        if imgui.small_button(f"{fa.ICON_FA_PEN_TO_SQUARE}  Use in the metadata form"):
            self.recording = rec
            self.message, self.failed = f"the metadata form holds {rec.label()}", False
        if imgui.is_item_hovered():
            idl.wrapped_tooltip(
                "fill the metadata form on Transfer and Browse storage from this recording, e.g. to send or register more data of its session"
            )
        with imgui_ctx.begin_child("##lab4_recording"):
            sections = (
                (
                    "recording",
                    "Recording",
                    "this recording's columns; its key (experimenter, group, subject, session, device) cannot change here",
                ),
                ("session", "Session", "shared by every recording of this session"),
                (
                    "subject",
                    f"Subject {row['subject_id']}",
                    "shared by every session of this subject",
                ),
            )
            for table, title, hint in sections:
                if self.schema.details(table):
                    self.subsection(title, hint)
                    self.draw_fields(table)
            self.draw_sidecar_lab4()
            if "injection" in self.schema.tables:
                self.draw_injections()

    def draw_fields(self, table: str) -> None:
        """A form row per column of the picked recording's table outside its key; a changed one's caption ends in *."""
        row, fields = self.picked_lab4[table], self.edits_lab4[table]
        if not self.begin_form(f"lab4_{table}"):
            return
        for column in self.schema.details(table):
            name = column.name
            changed = fields[name].strip() != lab4.text_of(row[name]).strip()
            self.row(name.replace("_", " ") + (" *" if changed else ""))
            if name.endswith("notes"):
                _, fields[name] = imgui.input_text_multiline(
                    f"##lab4_{table}_{name}",
                    fields[name],
                    imgui.ImVec2(-1, 3.5 * imgui.get_font_size()),
                )
            else:
                fields[name] = self.pick(
                    f"##lab4_{table}_{name}",
                    fields[name],
                    self.schema.choices.get(name, []),
                    "YYYY-MM-DD" if column.type == "date" else "",
                )
        imgui.end_table()

    def draw_sidecar_lab4(self) -> None:
        """Whether the picked recording's data has a sidecar, and whether Save rewrites it."""
        name = (
            posixpath.basename(self.picked_lab4["recording"]["data_path"])
            + SIDECAR_SUFFIX
        )
        imgui.dummy(imgui.ImVec2(0, 0.3 * imgui.get_font_size()))
        if "lab4_sidecar" in self.futures:
            imgui.text_disabled(f"looking for {name}...")
        elif self.error_sidecar_lab4:
            draw_wrapped(f"cannot read {name}: {self.error_sidecar_lab4}", WARN)
        elif self.sidecar_lab4 is None:
            draw_wrapped(
                f"no {name} beside the data; Attach metadata in Browse storage writes one",
                DIM,
            )
        elif self.moves_data():
            draw_wrapped(f"the data path changes, so {name} is left as it is", DIM)
        else:
            _, self.rewrite_sidecar = imgui.checkbox(
                f"Save also rewrites {name}", self.rewrite_sidecar
            )
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(
                    "the label, weight, notes and subject columns take the saved values; extra fields and the source stay"
                )

    def draw_injections(self) -> None:
        """The subject's injections, and a form to add one."""
        subject = self.picked_lab4["subject"]["subject_id"]
        self.subsection(
            "Injections",
            f"the indicators injected into {subject}; adding one asks first",
        )
        columns = [c for c in self.schema.tables["injection"] if c.name != "subject_id"]
        injections = [
            row
            for row in self.lab4.get("injection", [])
            if row["subject_id"] == subject
        ]
        for row in injections:
            imgui.text(
                " · ".join(
                    text
                    for text in (lab4.text_of(row[c.name]) for c in columns)
                    if text
                )
            )
        if not injections:
            imgui.text_disabled("none in Lab4")
        if self.begin_form("lab4_injection"):
            for column in columns:
                self.row(
                    column.name.replace("_", " ")
                    + ("" if column.key or column.required else " (optional)")
                )
                value = self.injection.get(column.name, "")
                self.injection[column.name] = self.pick(
                    f"##injection_{column.name}",
                    value,
                    self.schema.choices.get(column.name, []),
                    "YYYY-MM-DD" if column.type == "date" else "",
                )
            imgui.end_table()
        blockers = self.injection_problems(subject)
        imgui.begin_disabled(bool(blockers))
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add injection"):
            self.confirm_injection(subject)
        imgui.end_disabled()
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            idl.wrapped_tooltip(
                "\n".join(blockers)
                if blockers
                else f"add this injection of {subject} to Lab4"
            )

    def draw_results(self) -> None:
        """Scan and filter, each machine's last scan, a row per recording and unit, then the picked row's details."""
        em = imgui.get_font_size()
        self.watch_catalog()
        scanning = "scan_machines" in self.futures
        imgui.begin_disabled(scanning)
        if imgui.button(f"{fa.ICON_FA_DATABASE}  Scan machines"):
            self.scan_machines()
        imgui.end_disabled()
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            idl.wrapped_tooltip(
                "catalog the roots of every machine in Settings > Machines; lab4-scan does the same from a timer"
            )
        imgui.same_line()
        _, self.all_units = imgui.checkbox("all units", self.all_units)
        if imgui.is_item_hovered():
            idl.wrapped_tooltip("also list units no run or result uses yet")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        _, self.filter_results = imgui.input_text_with_hint(
            "##filter_results",
            "filter by recording, unit, indicator or machine",
            self.filter_results,
        )

        last = {}
        for scan in self.overview["scans"]:
            last[scan["storage"]] = max(
                last.get(scan["storage"], ""), scan["scanned_at"]
            )
        names = [m["name"] for m in self.settings.machines]
        for i, name in enumerate(names + sorted(set(last) - set(names))):
            if i:
                imgui.same_line(0, 1.5 * em)
            imgui.text_colored(
                OK if name in last else DIM, f"{fa.ICON_FA_LAPTOP}  {name}"
            )
            imgui.same_line()
            imgui.text_disabled(
                f"scanned {last[name][5:16].replace('T', ' ')}"
                if name in last
                else "not scanned"
            )
        if not names:
            draw_wrapped("Add the computers to catalog in Settings > Machines.", DIM)
        if scanning:
            imgui.text_colored(
                ACCENT,
                f"scanning {self.scanning[0]}... {self.scanning[1]} items listed",
            )
        for line in self.errors_scan:
            draw_wrapped(line, WARN)

        needle = self.filter_results.strip().lower()
        rows = [
            row
            for row in self.overview["rows"]
            if (self.all_units or row["runs"] or row["results"])
            and needle
            in " ".join(
                [
                    row["recording"],
                    row["unit"],
                    row["indicator"],
                    row["lab4"],
                    *row["storages"],
                ]
            ).lower()
        ]
        flags = (
            imgui.TableFlags_.row_bg
            | imgui.TableFlags_.borders_inner_h
            | imgui.TableFlags_.scroll_y
            | imgui.TableFlags_.resizable
        )
        height = max(8 * em, 0.45 * imgui.get_content_region_avail().y)
        # narrower than 40em, the table scrolls sideways rather than squeezing recording to nothing; 0 fits it to the view
        width_inner = 0.0 if imgui.get_content_region_avail().x >= 40 * em else 40 * em
        if imgui.begin_table(
            "##results",
            8,
            flags | imgui.TableFlags_.scroll_x,
            imgui.ImVec2(0, height),
            width_inner,
        ):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column(
                "recording", imgui.TableColumnFlags_.width_stretch, 2.0
            )
            imgui.table_setup_column(
                "unit", imgui.TableColumnFlags_.width_fixed, 6 * em
            )
            imgui.table_setup_column("Hz", imgui.TableColumnFlags_.width_fixed, 3 * em)
            imgui.table_setup_column(
                "frames", imgui.TableColumnFlags_.width_fixed, 3.5 * em
            )
            imgui.table_setup_column(
                "indicator", imgui.TableColumnFlags_.width_fixed, 5 * em
            )
            imgui.table_setup_column(
                "runs", imgui.TableColumnFlags_.width_fixed, 3.5 * em
            )
            imgui.table_setup_column(
                "results", imgui.TableColumnFlags_.width_fixed, 3.5 * em
            )
            imgui.table_setup_column("on", imgui.TableColumnFlags_.width_stretch, 1.0)
            imgui.table_headers_row()
            clipper = imgui.ListClipper()
            clipper.begin(len(rows))
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    row = rows[i]
                    key = (row["recording"], row["unit"])
                    imgui.table_next_row()
                    imgui.table_next_column()
                    if imgui.selectable(
                        f"{row['recording'] or '(unknown recording)'}##result_{i}",
                        key == self.picked_result,
                        imgui.SelectableFlags_.span_all_columns,
                    )[0]:
                        self.picked_result = key
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(
                            f"{row['recording']} {row['unit']}"
                            + (f"\nLab4 recording {row['lab4']}" if row["lab4"] else "")
                        )
                    imgui.table_next_column()
                    # most recordings have one session, so the table leaves MSession_0 out
                    imgui.text(row["unit"].removeprefix("MSession_0/") or "whole file")
                    imgui.table_next_column()
                    imgui.text_disabled(format_rate(row["fs"]))
                    imgui.table_next_column()
                    imgui.text_disabled(str(row["frames"] or ""))
                    imgui.table_next_column()
                    imgui.text_colored(KEY, row["indicator"])
                    imgui.table_next_column()
                    draw_run_counts(row["runs"])
                    imgui.table_next_column()
                    imgui.text_disabled(
                        str(len(row["results"])) if row["results"] else ""
                    )
                    imgui.table_next_column()
                    imgui.text_disabled(", ".join(row["storages"]))
            imgui.end_table()
        shown = f"{len(rows)} of {len(self.overview['rows'])} units"
        draw_wrapped(
            shown
            if self.overview["rows"]
            else "nothing cataloged yet; Scan machines, or Scan a folder in Browse storage",
            DIM,
        )
        picked = next(
            (
                row
                for row in self.overview["rows"]
                if (row["recording"], row["unit"]) == self.picked_result
            ),
            None,
        )
        if picked is not None:
            self.draw_result(row=picked)

    def draw_result(self, row: dict) -> None:
        """The picked unit's data files, runs and result files, each opened in place on its machine."""
        em = imgui.get_font_size()
        facts = [
            row["unit"] or "whole file",
            f"{format_rate(row['fs'])} Hz" if row["fs"] else "",
            f"{row['frames']} frames" if row["frames"] else "",
            row["indicator"],
        ]
        imgui.text_colored(
            ACCENT,
            f"{fa.ICON_FA_CHART_LINE}  {row['recording'] or '(unknown recording)'}",
        )
        imgui.same_line()
        imgui.text_disabled(
            " · ".join(fact for fact in facts if fact)
            + (f" · Lab4 {row['lab4']}" if row["lab4"] else "")
        )
        with imgui_ctx.begin_child("##result"):
            for i, data in enumerate(row["data"]):
                kind = classify_file(posixpath.basename(data["path"]))
                if imgui.small_button(f"{fa.ICON_FA_EYE}  Open##data_{i}"):
                    self.open_on(data["storage"], data["path"], kind)
                if imgui.is_item_hovered():
                    idl.wrapped_tooltip(
                        f"open in {KINDS[kind][0] if kind else 'its default app'}"
                    )
                imgui.same_line()
                imgui.text_disabled(data["storage"])
                imgui.same_line()
                imgui.text_colored(PATH, data["path"])
            if not row["data"]:
                imgui.text_disabled(
                    "no data file found for this unit; runs name it in their input"
                )
            flags = (
                imgui.TableFlags_.row_bg
                | imgui.TableFlags_.borders_inner_h
                | imgui.TableFlags_.sizing_stretch_prop
            )
            if row["runs"] and imgui.begin_table("##result_runs", 6, flags):
                imgui.table_setup_column(
                    "##open", imgui.TableColumnFlags_.width_fixed, 5.5 * em
                )
                imgui.table_setup_column(
                    "status", imgui.TableColumnFlags_.width_fixed, 4.5 * em
                )
                imgui.table_setup_column(
                    "started", imgui.TableColumnFlags_.width_fixed, 7.5 * em
                )
                imgui.table_setup_column(
                    "pipeline", imgui.TableColumnFlags_.width_stretch, 1.5
                )
                imgui.table_setup_column(
                    "on", imgui.TableColumnFlags_.width_fixed, 5 * em
                )
                imgui.table_setup_column(
                    "error", imgui.TableColumnFlags_.width_stretch, 1.5
                )
                imgui.table_headers_row()
                for i, run in enumerate(row["runs"]):
                    imgui.table_next_row()
                    imgui.table_next_column()
                    viewer = "masknmf-run" if run["kind"] == "masknmf-run" else None
                    if imgui.small_button(f"{fa.ICON_FA_EYE}##run_{i}"):
                        self.open_on(run["storage"], run["path"], viewer)
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(
                            "open in masknmf view" if viewer else "open the folder"
                        )
                    if run["log"]:
                        imgui.same_line()
                        if imgui.small_button(f"{fa.ICON_FA_FILE_LINES}##log_{i}"):
                            self.open_on(run["storage"], run["log"], None)
                        if imgui.is_item_hovered():
                            idl.wrapped_tooltip(
                                f"open {posixpath.basename(run['log'])}"
                            )
                    imgui.table_next_column()
                    imgui.text_colored(
                        COLORS_RUN.get(run["status"], DIM), run["status"] or "-"
                    )
                    imgui.table_next_column()
                    imgui.text_disabled(run["started"])
                    imgui.table_next_column()
                    imgui.text(run["pipeline"] or run["kind"])
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(
                            f"{run['path']}\ninput: {run['input'] or 'not recorded'}"
                        )
                    imgui.table_next_column()
                    imgui.text_disabled(run["storage"])
                    imgui.table_next_column()
                    imgui.text_colored(ERROR, run["error"])
                    if run["error"] and imgui.is_item_hovered():
                        idl.wrapped_tooltip(run["error"])
                imgui.end_table()
            if row["results"]:
                imgui.dummy(imgui.ImVec2(0, 0.3 * em))
                imgui.text_disabled(
                    f"{len(row['results'])} result files · double-click to open"
                )
                for i, result in enumerate(row["results"]):
                    where = (
                        posixpath.basename(result["run"])
                        if result["run"]
                        else posixpath.basename(posixpath.dirname(result["path"]))
                    )
                    flags_row = imgui.SelectableFlags_.allow_double_click
                    icon = ICONS_FILE.get(
                        posixpath.splitext(result["path"])[1].lower(), fa.ICON_FA_FILE
                    )
                    clicked, _ = imgui.selectable(
                        f"{icon}  {posixpath.basename(result['path'])}   {where} · {result['storage']}##res_file_{i}",
                        False,
                        flags_row,
                    )
                    if clicked and imgui.is_mouse_double_clicked(
                        imgui.MouseButton_.left
                    ):
                        self.open_on(result["storage"], result["path"], None)
                    if imgui.is_item_hovered():
                        idl.wrapped_tooltip(result["path"])

    def draw_settings(self) -> None:
        """One tab per group of settings; Save writes them all."""
        if imgui.begin_tab_bar("##settings"):
            tabs = (
                ("Connection", self.draw_connection),
                ("Destinations", self.draw_destinations),
                ("Machines", self.draw_machines),
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
                flags = (
                    imgui.TabItemFlags_.set_selected if self.tab_request == table else 0
                )
                selected, _ = imgui.begin_tab_item(
                    f"{table.capitalize()} ({len(self.rows_slices[table])})###tab_{table}",
                    None,
                    flags,
                )
                if selected:
                    if self.tab_slices != table:
                        self.editing = self.cell = None
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
                parent = (
                    rows_picked[0]["slice_id"]
                    if rows_picked
                    else next(
                        (
                            r["slice_id"]
                            for r in self.rows_slices["slices"]
                            if r["id"] in self.picked["slices"]
                        ),
                        "",
                    )
                )
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
                # outline its first cell to fill, so typing starts there
                self.cell = (
                    table,
                    id_,
                    next(
                        name
                        for name, kind, _ in columns
                        if kind not in ("joined", "owner")
                    ),
                )
                self.message, self.failed = (
                    f"added a row to {table}; type to fill the outlined cell, Tab for the next",
                    False,
                )
        if imgui.is_item_hovered():
            hints = {
                "slices": "new slice for the picked slice's patient, or the newest patient",
                "patients": "new patient",
                "assays": "new assay for the picked slice",
            }
            idl.wrapped_tooltip(hints[table])
        text_duplicate = f"{fa.ICON_FA_COPY}  Duplicate"
        same_line_or_wrap(width_button(text_duplicate))
        imgui.begin_disabled(not picked)
        if imgui.button(text_duplicate):
            made = self.slices.duplicate(table, sorted(picked), self.user_slices)
            self.filter_slices[table] = ""
            self.reload_slices()
            picked.clear()
            picked.update(made)
            self.scroll_slices = made[0] if made else None
            self.message, self.failed = f"copied {len(made)} row(s)", False
        text_delete = f"{fa.ICON_FA_TRASH}  Delete"
        same_line_or_wrap(width_button(text_delete))
        # Delete alone empties a cell, as in Excel
        pressed = (
            imgui.get_io().key_ctrl
            and imgui.is_key_pressed(imgui.Key.delete)
            and not imgui.is_any_item_active()
            and not imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id)
        )
        if imgui.button(text_delete) or (picked and pressed):
            key = "patient_id" if table == "patients" else "slice_id"
            names = [str(r[key]) for r in rows_picked]
            lines = [
                f"{len(names)} row(s) from {table}: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}",
                f"in {self.slices.path}",
                "Data files are not touched.",
            ]
            self.confirm = {
                "title": "Delete rows?",
                "lines": lines,
                "button": "Delete",
                "run": self.delete_rows,
                "args": (table, sorted(picked)),
            }
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            idl.wrapped_tooltip(
                "delete picked rows (Ctrl+Delete); for a failed slice, set its outcome instead"
            )
        imgui.end_disabled()
        if table in ("patients", "slices"):
            below, key = (
                ("slices", "patient_id")
                if table == "patients"
                else ("assays", "slice_id")
            )
            text_below = f"{fa.ICON_FA_ARROW_RIGHT}  Show its {below}"
            same_line_or_wrap(width_button(text_below))
            imgui.begin_disabled(len(rows_picked) != 1)
            if imgui.button(text_below):
                self.filter_slices[below] = f"{key}={rows_picked[0][key]}"
                self.order_rows(table=below)
                self.tab_request = below
            imgui.end_disabled()
        text_reset = f"{fa.ICON_FA_ARROWS_ROTATE}  Reset rows"
        width_user = 8 * em
        width_right = (
            imgui.calc_text_size("acting as").x
            + width_user
            + width_button(text_reset)
            + 2 * imgui.get_style().item_spacing.x
        )
        same_line_or_wrap(width_right)
        imgui.set_cursor_pos_x(
            imgui.get_cursor_pos_x()
            + max(0.0, imgui.get_content_region_avail().x - width_right)
        )
        imgui.align_text_to_frame_padding()
        imgui.text_disabled("acting as")
        imgui.same_line()
        imgui.set_next_item_width(width_user)
        changed, index = imgui.combo(
            "##user_slices", PEOPLE.index(self.user_slices), PEOPLE
        )
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
            self.confirm = {
                "title": "Reset rows?",
                "lines": lines,
                "button": "Reset",
                "run": self.reset_slices,
                "args": (),
            }

        imgui.set_next_item_width(-1)
        hints = {
            "slices": "outcome:failed ephys:synaptic day:2",
            "patients": "diagnosis:epilepsy sex:f",
            "assays": "assay:live-dead owner:dana",
        }
        changed, self.filter_slices[table] = imgui.input_text_with_hint(
            "##filter",
            f"{fa.ICON_FA_FILTER}  filter, e.g. {hints[table]}",
            self.filter_slices[table],
        )
        if changed:
            self.order_rows(table=table)
        order = self.order_slices[table]
        line = f"{len(order)} of {len(self.rows_slices[table])} {table}"
        line += f" · {len(picked)} picked" if picked else ""
        if table == "slices" and order:
            line += (
                f" · {summarize(Counter(r['outcome'] for r in order if r['outcome']))}"
            )
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
        if imgui.begin_table(
            f"##db_{table}", len(columns), flags, imgui.ImVec2(0, height)
        ):
            imgui.table_setup_scroll_freeze(1, 1)
            for i, (name, _, _) in enumerate(columns):
                # default sort: collection date, region, patient, day
                flags_column = (
                    imgui.TableColumnFlags_.default_sort
                    if table == "slices" and i < 4
                    else 0
                )
                # the first column, where rows are picked, stays shown and first
                flags_column |= (
                    imgui.TableColumnFlags_.no_hide | imgui.TableColumnFlags_.no_reorder
                    if i == 0
                    else 0
                )
                imgui.table_setup_column(name, flags_column)
            imgui.table_headers_row()
            specs = imgui.table_get_sort_specs()
            if specs is not None and specs.specs_dirty:
                self.sort_slices[table] = [
                    (
                        columns[specs.get_specs(n).column_index][0],
                        specs.get_specs(n).sort_direction
                        == imgui.SortDirection.ascending,
                    )
                    for n in range(specs.specs_count)
                ]
                specs.specs_dirty = False
                self.order_rows(table=table)
                order = self.order_slices[table]
            names = [name for name, _, _ in columns]
            self.shown_slices = columns_shown(len(columns))
            self.keys_cell(table=table)
            if self.cell is not None and (
                self.editing is not None or not imgui.is_any_item_active()
            ):
                # Tab moves the outlined cell, not imgui's focus
                imgui.internal.set_key_owner(imgui.Key.tab, imgui.get_id("##cells"))
            hits = [i for i, row in enumerate(order) if row["id"] == self.scroll_slices]
            # the row and column the outlined cell moved to, drawn and scrolled to even when off screen
            moved = None
            if self.scroll_cell and self.cell is not None:
                moved = next(
                    (
                        (i, names.index(self.cell[2]))
                        for i, row in enumerate(order)
                        if row["id"] == self.cell[1]
                    ),
                    None,
                )
            self.scroll_cell = False
            self.rect_choice = None
            clipper = imgui.ListClipper()
            clipper.begin(len(order))
            if hits:
                clipper.include_item_by_index(hits[0])
            if moved is not None:
                clipper.include_item_by_index(moved[0])
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    row = order[i]
                    imgui.table_next_row()
                    for c, (name, kind, _) in enumerate(columns):
                        if not imgui.table_set_column_index(c):
                            continue
                        value = row[name]
                        if c == 0:
                            if moved is not None and i == moved[0]:
                                scroll_to_cell(moved[1])
                            x = imgui.get_cursor_pos_x()
                            flags_row = (
                                imgui.SelectableFlags_.span_all_columns
                                | imgui.SelectableFlags_.allow_overlap
                                | imgui.SelectableFlags_.allow_double_click
                            )
                            clicked, _ = imgui.selectable(
                                f"##row_{row['id']}", row["id"] in picked, flags_row
                            )
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
                                if 0 <= column < len(columns):
                                    self.cell = (table, row["id"], names[column])
                                    if imgui.is_mouse_double_clicked(
                                        imgui.MouseButton_.left
                                    ):
                                        self.start_edit(
                                            table,
                                            row,
                                            column,
                                            x_click=imgui.get_mouse_pos().x,
                                        )
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
                                self.caret_edit = (
                                    None
                                    if self.x_click is None
                                    else caret_at(
                                        self.text_edit,
                                        self.x_click - imgui.get_cursor_screen_pos().x,
                                    )
                                )
                            self.hold_chars("##edit")
                            # no frame padding: the text stays where it was and the row keeps its height
                            imgui.push_style_var(
                                imgui.StyleVar_.frame_padding, imgui.ImVec2(0, 0)
                            )
                            imgui.set_next_item_width(
                                imgui.get_content_region_avail().x
                            )
                            flags_edit = (
                                imgui.InputTextFlags_.enter_returns_true
                                | imgui.InputTextFlags_.callback_always
                            )
                            entered, self.text_edit = imgui.input_text(
                                "##edit", self.text_edit, flags_edit, self.place_caret
                            )
                            imgui.pop_style_var()
                            back = -1 if imgui.get_io().key_shift else 1
                            step = (back, 0) if entered else None
                            if step is None and imgui.is_item_active():
                                keys = {
                                    imgui.Key.tab: (0, back),
                                    imgui.Key.up_arrow: (-1, 0),
                                    imgui.Key.down_arrow: (1, 0),
                                }
                                step = next(
                                    (
                                        s
                                        for key, s in keys.items()
                                        if imgui.is_key_pressed(key)
                                    ),
                                    None,
                                )
                            if imgui.is_item_deactivated() and imgui.is_key_pressed(
                                imgui.Key.escape
                            ):
                                # Escape keeps what was there
                                self.editing = None
                            elif step is not None:
                                self.commit_edit(text=self.text_edit)
                                self.move_cell(table, *step)
                                if imgui.is_item_active():
                                    imgui.internal.clear_active_id()
                            elif imgui.is_item_deactivated():
                                # clicking away saves, as in Excel
                                self.commit_edit(text=self.text_edit)
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
                        if (
                            kind == "text"
                            and value
                            and not editing
                            and imgui.is_item_hovered()
                        ):
                            idl.wrapped_tooltip(value)
                        if editing and kind in ("choice", "ref"):
                            self.rect_choice = rect_cell(c)
                        if self.cell == (table, row["id"], name):
                            edge = rect_cell(c)
                            color = imgui.color_convert_float4_to_u32(ACCENT)
                            imgui.get_window_draw_list().add_rect(
                                imgui.ImVec2(edge.min.x + 1, edge.min.y + 1),
                                imgui.ImVec2(edge.max.x - 1, edge.max.y - 1),
                                color,
                                thickness=2.0,
                            )
            imgui.end_table()

        if self.open_choice:
            imgui.open_popup("##choice")
            self.open_choice = False
        if self.rect_choice is not None:
            # the list drops from the cell, as Excel's does
            imgui.set_next_window_pos(
                imgui.ImVec2(self.rect_choice.min.x, self.rect_choice.max.y),
                imgui.Cond_.appearing,
            )
        if imgui.begin_popup("##choice"):
            if self.editing is None:
                imgui.close_current_popup()
            else:
                self.draw_choices()
            imgui.end_popup()
        elif self.editing is not None and next(
            column[1]
            for column in COLUMNS[self.editing[0]]
            if column[0] == self.editing[2]
        ) in ("choice", "ref"):
            self.editing = None

        row = rows_picked[0] if len(rows_picked) == 1 else None
        slice_ = None
        if row is not None and table != "patients":
            slice_ = (
                row
                if table == "slices"
                else next(
                    (
                        s
                        for s in self.rows_slices["slices"]
                        if s["slice_id"] == row["slice_id"]
                    ),
                    None,
                )
            )
        files, claimed, heading = self.files_of(table, row, slice_)

        flags_detail = (
            imgui.TableFlags_.sizing_stretch_same | imgui.TableFlags_.borders_inner_v
        )
        # in a short window the table keeps its 8 lines and the page scrolls to these 3
        if not imgui.begin_table(
            "##slice_detail",
            2,
            flags_detail,
            imgui.ImVec2(0, max(3 * em, imgui.get_content_region_avail().y)),
        ):
            return
        imgui.table_next_row()
        imgui.table_next_column()
        with imgui_ctx.begin_child("##slice_record"):
            if row is None:
                draw_wrapped("Pick a row to see its details and files.", DIM)
                draw_wrapped(
                    "Edit cells as in Excel: double-click one, or click it and type, or press F2. Enter, Tab and the arrows "
                    "move on, Escape cancels, Delete empties a cell. Ctrl or shift-click picks several rows; Ctrl+Delete "
                    "deletes them. Shift-click headers to sort by several; right-click one to hide columns. Filter with "
                    "words, column:value or column=value for an exact match. file_match: every word must be in the path, a "
                    "date matches any spelling, and -word excludes.",
                    DIM,
                )
            elif table == "patients":
                theirs = [
                    s
                    for s in self.rows_slices["slices"]
                    if s["patient_id"] == row["patient_id"]
                ]
                draw_wrapped(
                    f"{fa.ICON_FA_USER}  {row['patient_id']}: {row['age'] or '?'} y {row['sex'] or '?'}, {row['diagnosis'] or 'diagnosis not set'}, collected {row['collection_date']}"
                )
                outcomes = summarize(
                    Counter(s["outcome"] for s in theirs if s["outcome"])
                )
                days = summarize(
                    Counter(
                        f"on Day {s['day']}" for s in theirs if s["day"] is not None
                    )
                )
                draw_wrapped(
                    f"{len(theirs)} slices{': ' + outcomes if outcomes else ''}{'; ' + days if days else ''}",
                    DIM,
                )
                if row["notes"]:
                    draw_wrapped(row["notes"], DIM)
            elif slice_ is not None:
                failed = (
                    f" ({slice_['failure_reason']})" if slice_["failure_reason"] else ""
                )
                day = (
                    f"Day {slice_['day']}"
                    if slice_["day"] is not None
                    else "day not set"
                )
                draw_wrapped(
                    f"{fa.ICON_FA_TABLE}  {slice_['slice_id']}: {day}, {slice_['region'] or 'region not set'}, {slice_['outcome']}{failed}",
                    COLORS_SLICE.get(slice_["outcome"], TEXT),
                )
                patient = next(
                    (
                        p
                        for p in self.rows_slices["patients"]
                        if p["patient_id"] == slice_["patient_id"]
                    ),
                    None,
                )
                if patient is not None:
                    draw_wrapped(
                        f"{fa.ICON_FA_USER}  {patient['patient_id']}, collected {patient['collection_date']}{', ' + patient['diagnosis'] if patient['diagnosis'] else ''}",
                        DIM,
                    )
                construct = (
                    f" {slice_['ep_construct']}" if slice_["ep_construct"] else ""
                )
                notes = f" ({slice_['ephys_notes']})" if slice_["ephys_notes"] else ""
                draw_wrapped(
                    f"electroporated {slice_['ep_material'] or '?'}{construct}, dye {slice_['ep_dye'] or '?'} · ephys {slice_['ephys'] or '?'}{notes} · {slice_['incubator'] or 'incubator not set'}",
                    DIM,
                )
                for assay in [
                    a
                    for a in self.rows_slices["assays"]
                    if a["slice_id"] == slice_["slice_id"]
                ]:
                    live = (
                        f"{assay['percent_live']}% live, "
                        if assay["percent_live"] is not None
                        else ""
                    )
                    draw_wrapped(
                        f"{fa.ICON_FA_FLASK}  {assay['assay']} {assay['assay_date'] or ''} by {assay['owner']}: {live}{assay['result'] or ''}",
                        CODE,
                    )
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
                imgui.text_disabled(
                    f"{len(self.files_index)} files indexed {self.indexed['indexed_at'].replace('T', ' ')} ({self.indexed['storage']})"
                )
            if imgui.is_item_hovered():
                idl.wrapped_tooltip(f"all files under {ROOT}; read-only")
            imgui.same_line()
            imgui.begin_disabled(indexing)
            if imgui.small_button(f"{fa.ICON_FA_ARROWS_ROTATE}##index"):
                self.index_files()
            imgui.end_disabled()
            if self.error_index:
                draw_wrapped(
                    f"indexing failed on {self.storage.label}: {self.error_index}", WARN
                )
            if self.fetch is not None:
                total = max(self.fetch["total"], 1)
                imgui.progress_bar(
                    self.fetch["done"] / total,
                    imgui.ImVec2(-6 * em, 0),
                    f"copying {self.fetch['local'].name}",
                )
                imgui.same_line()
                if imgui.small_button("cancel##fetch"):
                    self.fetch["cancelled"] = True
            if heading:
                imgui.text_colored(PATH, f"{fa.ICON_FA_FOLDER_OPEN}  {heading}")
            copies: dict[tuple[str, int], list[str]] = {}
            for f in files:
                copies.setdefault(
                    (posixpath.basename(f["path"]), f["size"]), []
                ).append(f["path"])
            imgui.begin_disabled(self.fetch is not None)
            for (name, size), paths in copies.items():
                kind = classify_file(name)
                icon = ICONS_FILE.get(
                    posixpath.splitext(name)[1].lower(), fa.ICON_FA_FILE
                )
                stray = bool(claimed) and not any(path in claimed for path in paths)
                color = WARN if stray else KEY if kind is not None else None
                if color is not None:
                    imgui.push_style_color(imgui.Col_.text, color)
                clicked, _ = imgui.selectable(
                    f"{icon}  {name}##file_{paths[0]}",
                    False,
                    imgui.SelectableFlags_.allow_double_click,
                )
                if color is not None:
                    imgui.pop_style_color()
                if imgui.is_item_hovered():
                    how = (
                        f"double-click to open in {KINDS[kind][0]}"
                        if kind
                        else "double-click to open in its default app"
                    )
                    where = "\n".join(paths)
                    idl.wrapped_tooltip(
                        f"{how}{'; matches no slice' if stray else ''}\n{where}"
                    )
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    self.open_file(path=paths[0], size=size)
                imgui.same_line()
                imgui.text_disabled(
                    f"{human_bytes(size)}{f' · {len(paths)} copies' if len(paths) > 1 else ''}"
                )
            imgui.end_disabled()
        imgui.end_table()

    def draw_connection(self) -> None:
        """Backend choice and its settings."""
        s = self.settings
        draw_wrapped(
            "No login here: a mounted share uses your mount, ssh uses your keys.", DIM
        )
        for backend, label in zip(BACKENDS, ("Mounted share", "SSH")):
            if imgui.radio_button(label, s.backend == backend):
                s.backend = backend
            imgui.same_line()
        imgui.new_line()
        if self.begin_form("connection"):
            if s.backend == "mounted":
                self.row("mount root")
                _, s.mount_root = imgui.input_text_with_hint(
                    "##root",
                    r"e.g. X:\ or \\lamella.biohpc.swmed.edu\share",
                    s.mount_root,
                )
                self.row("shows")
                _, s.mount_base = imgui.input_text_with_hint(
                    "##base",
                    "BioHPC path of the mount root, e.g. /biohpc",
                    s.mount_base,
                )
            else:
                self.row("host")
                _, s.ssh_host = imgui.input_text_with_hint(
                    "##host", "ssh alias or user@host, e.g. lab-research", s.ssh_host
                )
            imgui.end_table()
        if s.backend == "mounted":
            if imgui.small_button(f"{fa.ICON_FA_MAGNIFYING_GLASS}  Detect"):
                self.detect_mounts()
            imgui.same_line()
            if self.mounts is None:
                imgui.text_disabled(f"looking for {s.mount_base} mounts...")
            elif not self.mounts:
                draw_wrapped(
                    f"no {s.mount_base} mount found; map the share first (e.g. SSHFS-Win)",
                    DIM,
                )
            else:
                imgui.text_disabled(f"{s.mount_base} mounts found:")
                for i, mount in enumerate(self.mounts):
                    if imgui.small_button(f"use##mount_{i}"):
                        self.use_mount(mount)
                    imgui.same_line()
                    imgui.text_colored(
                        OK if mount.root == s.mount_root else TEXT, mount.root
                    )
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
            _, pair[0] = imgui.input_text_with_hint(
                f"##dpath_{i}", "/path/on/biohpc", pair[0]
            )
            imgui.same_line()
            imgui.set_next_item_width(-1)
            _, pair[1] = imgui.input_text_with_hint(
                f"##ddesc_{i}", "what goes here", pair[1]
            )
        if remove is not None:
            s.destinations.pop(remove)
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add destination"):
            s.destinations.append(["", ""])

    def draw_machines(self) -> None:
        """Computers Results catalogs, and how this one reaches each."""
        em = imgui.get_font_size()
        s = self.settings
        draw_wrapped(
            "Computers whose recordings, runs and results Results lists. Roots are that machine's folders to catalog, "
            "one per line, in its own paths. Mount a machine (e.g. sshfs) to open its files here; over ssh they are only listed.",
            DIM,
        )
        remove = None
        for i, machine in enumerate(s.machines):
            imgui.push_id(f"machine_{i}")
            if imgui.small_button(fa.ICON_FA_XMARK):
                remove = i
            imgui.same_line()
            imgui.text_colored(
                ACCENT, f"{fa.ICON_FA_LAPTOP}  {machine['name'] or 'new machine'}"
            )
            if self.begin_form(f"machine_{i}"):
                self.row("name")
                _, machine["name"] = imgui.input_text_with_hint(
                    "##name", "unique, e.g. pml-w1", machine["name"]
                )
                self.row("reached")
                for reach in REACHES:
                    if imgui.radio_button(
                        NAMES_REACH[reach], machine["reach"] == reach
                    ):
                        machine["reach"] = reach
                    imgui.same_line()
                imgui.new_line()
                if machine["reach"] == "ssh":
                    self.row("host")
                    _, machine["host"] = imgui.input_text_with_hint(
                        "##host", "ssh alias or user@host, keys set up", machine["host"]
                    )
                elif machine["reach"] == "mounted":
                    self.row("mounted at")
                    _, machine["mount_root"] = imgui.input_text_with_hint(
                        "##mount_root",
                        r"local folder, e.g. ~/pml-w1, or Z:\ on Windows",
                        machine["mount_root"],
                    )
                    self.row("shows")
                    _, machine["mount_base"] = imgui.input_text_with_hint(
                        "##mount_base",
                        "the machine's path there, e.g. /home/fo",
                        machine["mount_base"],
                    )
                self.row("roots")
                changed, text = imgui.input_text_multiline(
                    "##roots", "\n".join(machine["roots"]), imgui.ImVec2(-1, 3.2 * em)
                )
                if changed:
                    machine["roots"] = text.split("\n")
                imgui.end_table()
            imgui.pop_id()
            imgui.dummy(imgui.ImVec2(0, 0.3 * em))
        if remove is not None:
            s.machines.pop(remove)
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add machine"):
            s.machines.append({**this_computer(), "name": "", "reach": "mounted"})
        names = [m["name"] for m in s.machines]
        if len(set(names)) < len(names) or "" in names:
            draw_wrapped(
                "Each machine needs its own name: the catalog keys its rows by it.",
                WARN,
            )

    def draw_viewers(self) -> None:
        """Viewer commands."""
        s = self.settings
        draw_wrapped("Programs that open datasets: a name on PATH or a full path.", DIM)
        if self.begin_form("viewers"):
            self.row("mbo")
            _, s.command_mbo = imgui.input_text_with_hint("##mbo", "mbo", s.command_mbo)
            self.row("masknmf")
            _, s.command_masknmf = imgui.input_text_with_hint(
                "##masknmf", "masknmf", s.command_masknmf
            )
            imgui.end_table()
        for name, command in (("mbo", s.command_mbo), ("masknmf", s.command_masknmf)):
            found = shutil.which(command)
            imgui.text_colored(OK if found else WARN, f"{name}: {found or 'not found'}")

    def draw_processing_settings(self) -> None:
        """SLURM job settings."""
        s = self.settings
        draw_wrapped(
            "Jobs are submitted over ssh with your keys. To set up BioHPC, see hpc/asap7/README.md in pml_utilities.",
            DIM,
        )
        if self.begin_form("processing"):
            self.row("ssh host")
            _, s.processing_host = imgui.input_text_with_hint(
                "##phost",
                "ssh alias or user@host of a BioHPC login node",
                s.processing_host,
            )
            self.row("partition")
            _, s.partition = imgui.input_text_with_hint(
                "##partition", "a SLURM partition with GPUs", s.partition
            )
            self.row("GPUs")
            _, s.gres = imgui.input_text_with_hint("##gres", "gpu:1", s.gres)
            self.row("email")
            _, s.email = imgui.input_text_with_hint(
                "##email", "emailed when a job ends; empty for none", s.email
            )
            self.row("pipeline folder")
            _, s.dir_asap7 = imgui.input_text_with_hint(
                "##dir_asap7", "~/repos/pml_utilities/hpc/asap7", s.dir_asap7
            )
            self.row(f"{s.mount_base} is")
            _, s.cluster_root = imgui.input_text_with_hint(
                "##cluster_root", "the share's path on BioHPC nodes", s.cluster_root
            )
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
        widths = [
            imgui.calc_text_size(t).x + padding
            for t in [text_out if self.user else None, "Quit", label]
            if t
        ]
        height = 1.7 * em
        if not imgui.begin_table("##footer", 2, imgui.TableFlags_.sizing_stretch_prop):
            return
        imgui.table_setup_column("##status", imgui.TableColumnFlags_.width_stretch)
        imgui.table_setup_column(
            "##buttons",
            imgui.TableColumnFlags_.width_fixed,
            sum(widths) + spacing * (len(widths) - 1),
        )
        imgui.table_next_row()
        imgui.table_next_column()
        if not self.user:
            idl.text_wrapped_colored(DIM, "sign in to continue")
        elif problems and label is not None:
            for problem in problems[:3]:
                idl.text_wrapped_colored(WARN, f"{fa.ICON_FA_CIRCLE_INFO}  {problem}")
        elif self.message and self.failed:
            idl.text_wrapped_colored(
                ERROR, f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.message}"
            )
        elif self.message:
            idl.text_wrapped_colored(OK, f"{fa.ICON_FA_CIRCLE_CHECK}  {self.message}")
        elif self.page == "transfer":
            idl.text_wrapped_colored(
                DIM,
                f"{fa.ICON_FA_CLOUD_ARROW_UP}  sends to {self.destination() or '...'} over {self.settings.backend}",
            )
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
    params.imgui_window_params.tweaked_theme.theme = (
        hello_imgui.ImGuiTheme_.darcula_darker
    )
    params.ini_folder_type = hello_imgui.IniFolderType.absolute_path
    params.ini_filename = str(data_dir() / "window.ini")
    app.dialog.apply_host_theme(params)
    params.callbacks.show_gui = app.dialog.render
    immapp.run(params)
    app.executor.shutdown(wait=False, cancel_futures=True)
    app.executor_long.shutdown(wait=False, cancel_futures=True)
