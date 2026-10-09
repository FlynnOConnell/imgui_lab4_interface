"""
The window `lab4` opens: a sign-in card, then a rail of pages on the left and the page on the right, laid out
like the masknmf launcher on imgui_data_loader's dialog. Storage calls run on worker threads, so a slow mount or
ssh link never stalls a frame.
"""

import json
import posixpath
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import imgui_data_loader as idl
from imgui_bundle import hello_imgui, imgui, imgui_ctx, immapp
from imgui_bundle import icons_fontawesome_6 as fa
from imgui_bundle import portable_file_dialogs as pfd

from imgui_lab4_interface import __version__, auth, lab4, viewers
from imgui_lab4_interface.catalog import KINDS, Catalog
from imgui_lab4_interface.config import BACKENDS, Settings, data_dir
from imgui_lab4_interface.storage import SIDECAR_SUFFIX, Entry, Transfer, open_storage, write_text

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
CUSTOM = "Custom path..."

# the rail: a page key, its icon, its name, and the line under the name on its page
PAGES = (
    ("home", fa.ICON_FA_HOUSE, "Overview", "what this window does and where data lands"),
    ("transfer", fa.ICON_FA_CLOUD_ARROW_UP, "Transfer", "copy files or folders into a BioHPC folder, metadata beside them"),
    ("metadata", fa.ICON_FA_TAGS, "Metadata", "the Lab4 recording identity and notes that travel with the data"),
    ("browse", fa.ICON_FA_SITEMAP, "Browse storage", "look through BioHPC folders, catalog them, open datasets and attach metadata"),
    ("find", fa.ICON_FA_MAGNIFYING_GLASS, "Find data", "search the catalog of scanned folders and open a dataset in mbo or masknmf"),
    ("storage", fa.ICON_FA_SERVER, "Storage", "how this machine reaches BioHPC: a mounted share or ssh"),
)
# the files a transfer writes
FILES = (
    ("<name>", "each file or folder picked, copied as is under the destination folder"),
    (f"<name>{SIDECAR_SUFFIX}", "the recording's Lab4 key, acquisition, notes and extra fields, beside it"),
)


def draw_wrapped(text: str, color: Optional[imgui.ImVec4] = None) -> None:
    """Text wrapped at the current window's right edge, in color when given."""
    if color is None:
        imgui.text_wrapped(text)
    else:
        idl.text_wrapped_colored(color, text)


def draw_hint(text: str) -> None:
    """A dim (?) after the previous item that shows text when hovered."""
    imgui.same_line()
    imgui.text_disabled("(?)")
    if imgui.is_item_hovered():
        idl.wrapped_tooltip(text)


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
    """
    State behind the window: who is signed in, the page shown and every value typed into the pages.

    Args:
        settings (Settings | None): Storage settings; read from the data folder when None
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings.load()
        self.storage = open_storage(self.settings)
        self.user = ""
        self.username = ""
        self.password = ""
        self.error_login = ""
        self.page = "home"
        # None while a check runs or before the first, "" once reached, else why not
        self.status: Optional[str] = None
        # the last action's outcome for the footer, and whether it failed
        self.message = ""
        self.failed = False
        self.sources: list[str] = []
        self.index_dest = 0
        self.custom_dest = ""
        self.subfolder = ""
        self.overwrite = False
        self.attach = True
        self.recording = lab4.Recording()
        self.transfer: Optional[Transfer] = None
        self.path_browse = self.settings.destinations[0][0] if self.settings.destinations else "/"
        self.entries: list[Entry] = []
        self.error_browse = ""
        self.selected = ""
        self.sidecar = ""
        self.catalog = Catalog()
        # what the catalog knows of the browse folder: its summary, the scan covering it, and its items by name
        self.described: dict = {"folder": None, "scan": None, "items": {}}
        self.count_scanned = 0
        self.query = ""
        self.query_kind = ""
        self.results: list[dict] = []
        self.result: Optional[dict] = None
        self.text_sql = "SELECT kind, count(*) AS n, sum(size) AS bytes FROM datasets GROUP BY kind ORDER BY n DESC"
        self.table_sql: tuple[list, list] = ([], [])
        self.error_sql = ""
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

    def sign_in(self) -> None:
        if not auth.sign_in(username=self.username, password=self.password):
            self.error_login = "Invalid credentials."
            self.password = ""
            return
        self.user = self.username.strip()
        self.password = ""
        self.error_login = ""
        if self.user in lab4.EXPERIMENTERS:
            self.recording.experimenter = self.user
        self.connect()

    def sign_out(self) -> None:
        if self.transfer is not None:
            self.transfer.cancelled = True
        self.user = ""
        self.page = "home"
        self.message = ""

    def quit(self) -> None:
        if self.transfer is not None:
            self.transfer.cancelled = True
        hello_imgui.get_runner_params().app_shall_exit = True

    def submit(self, name: str, fn, *args) -> None:
        """Run fn on a worker; poll lands its outcome by name."""
        self.futures[name] = self.executor.submit(fn, *args)

    def connect(self) -> None:
        """Rebuild the storage from the settings, check it, and list the browse folder."""
        self.storage = open_storage(self.settings)
        self.status = None
        self.submit("check", self.storage.check)
        self.browse(path=self.path_browse)
        self.search()

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

    def search(self) -> None:
        self.submit("search", self.catalog.search, self.storage.label, self.query, self.query_kind)

    def open_dataset(self, path: str, kind: str) -> None:
        """Start the viewer kind calls for on path, reporting the outcome in the footer."""
        local = self.storage.local_path(path)
        self.failed = True
        if local is None:
            self.message = "opening needs the share mounted on this machine; ssh only lists it"
            return
        try:
            log = viewers.launch(viewers.command(self.settings, kind, local))
        except OSError as e:
            self.message = str(e)
            return
        self.failed = False
        self.message = f"opened {posixpath.basename(path)} in {KINDS[kind][0]}; its output goes to {log}"

    def select(self, name: str) -> None:
        self.selected = name
        self.sidecar = ""
        names = {e.name for e in self.entries}
        if f"{name}{SIDECAR_SUFFIX}" in names:
            self.submit("sidecar", self.storage.read_text, f"{self.path_browse.rstrip('/')}/{name}{SIDECAR_SUFFIX}")

    def poll(self) -> None:
        """Land every finished worker's outcome, and a finished picker's choice."""
        for name, future in list(self.futures.items()):
            if not future.done():
                continue
            del self.futures[name]
            error = future.exception()
            if name == "check":
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
                self.message = f"scan failed: {error}" if found is None else f"catalogued {found['datasets']} datasets in {found['records']} items under {found['root']} ({found['seconds']:.1f} s)"
                self.failed = found is None
                self.describe()
                self.search()
            elif name == "search":
                self.results = [] if error is not None else future.result()
            elif name == "sql":
                self.table_sql = ([], []) if error is not None else future.result()
                self.error_sql = "" if error is None else str(error)
            elif name == "sidecar":
                self.sidecar = "" if error is not None else future.result()
            elif name == "attach":
                self.message = f"metadata attached to {self.selected}" if error is None else f"attach failed: {error}"
                self.failed = error is not None
                self.submit("list", self.storage.listdir, self.path_browse)
            elif name == "transfer":
                self.message = f"sent {len(self.transfer.sources)} item(s) to {self.transfer.dest}" if error is None else str(error)
                self.failed = error is not None
                if error is None:
                    self.transfer = None
                    self.browse(path=self.path_browse)
        if self.picker is not None and self.picker.ready():
            chosen = self.picker.result()
            chosen = chosen if isinstance(chosen, list) else [chosen]
            chosen = [c for c in chosen if c and c not in self.sources]
            self.sources += chosen
            if chosen:
                self.store.record_selection(idl.DialogResult(paths=chosen))
            self.picker = None

    def open_picker(self, folders: bool) -> None:
        if self.picker is not None:
            return
        start = self.store.default_dir() or str(Path.home())
        if folders:
            self.picker = pfd.select_folder("Add folder", start)
        else:
            self.picker = pfd.open_file("Add files", start, ["All Files", "*"], pfd.opt.multiselect)

    def destination(self) -> str:
        """The BioHPC folder a transfer lands in: the preset or custom folder, then the subfolder."""
        if self.index_dest < len(self.settings.destinations):
            base = self.settings.destinations[self.index_dest][0]
        else:
            base = self.custom_dest.strip()
        if not base.strip("/"):
            return ""
        sub = self.subfolder.strip().strip("/")
        return f"{base.rstrip('/')}/{sub}" if sub else base.rstrip("/")

    def problems(self) -> list[str]:
        """What keeps the page's action from running."""
        found = []
        if self.page in ("transfer", "browse") and self.status:
            found.append(f"storage unreachable: {self.status}")
        if self.page == "transfer":
            if self.transfer is not None and "transfer" in self.futures:
                found.append("a transfer is running")
            if not self.sources:
                found.append("add a file or folder to send")
            found += [f"{s} is not on this machine" for s in self.sources if not Path(s).exists()]
            if not self.destination():
                found.append("choose a destination folder")
            if self.attach:
                found += self.recording.problems()
        elif self.page == "find":
            if self.result is None:
                found.append("select a dataset to open")
            elif self.storage.local_path(self.result["path"]) is None:
                found.append("opening needs the share mounted on this machine; ssh only lists it")
        elif self.page == "browse":
            if not self.selected:
                found.append("select a file or folder to attach metadata to")
            elif self.selected.endswith(SIDECAR_SUFFIX):
                found.append("that is a metadata sidecar already")
            found += self.recording.problems()
        return found

    def label_action(self) -> Optional[str]:
        if self.page == "transfer":
            return f"{fa.ICON_FA_CLOUD_ARROW_UP}  Send {len(self.sources)} item{'' if len(self.sources) == 1 else 's'}"
        if self.page == "browse":
            return f"{fa.ICON_FA_TAGS}  Attach metadata"
        if self.page == "storage":
            return f"{fa.ICON_FA_PLUG}  Save and connect"
        if self.page == "find":
            return f"{fa.ICON_FA_EYE}  Open in {KINDS[self.result['kind']][0] if self.result else 'viewer'}"
        return None

    def act(self) -> None:
        self.message = ""
        if self.page == "transfer":
            metadata = self.recording.sidecar(user=self.user) if self.attach else None
            sources = [Path(s) for s in self.sources]
            self.transfer = Transfer(self.storage, sources, self.destination(), metadata, self.overwrite)
            self.submit("transfer", self.transfer.run)
        elif self.page == "browse":
            path = f"{self.path_browse.rstrip('/')}/{self.selected}{SIDECAR_SUFFIX}"
            text = json.dumps({**self.recording.sidecar(user=self.user), "source": f"{self.path_browse.rstrip('/')}/{self.selected}"}, indent=2)
            self.submit("attach", write_text, self.storage, path, text)
        elif self.page == "storage":
            self.settings.save()
            self.connect()
        elif self.page == "find":
            self.open_dataset(path=self.result["path"], kind=self.result["kind"])

    def draw_header(self) -> None:
        """The name and version, who is signed in and whether storage answers, then what this does."""
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
                idl.wrapped_tooltip(f"{self.storage.label}: {self.status}" if self.status else self.storage.label)
        draw_wrapped("PML data onto BioHPC storage, with its Lab4 metadata beside it", DIM)

    def draw_body(self) -> None:
        """The sign-in card, or the rail and the page side by side; Esc goes back to the Overview."""
        self.poll()
        if not self.user:
            self.draw_login()
            return
        if imgui.is_key_pressed(imgui.Key.escape) and not imgui.is_any_item_active():
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

    def draw_login(self) -> None:
        """A centered card: username, password, Sign In."""
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
            idl.center_text("move data and attach metadata", DIM)
            imgui.dummy(imgui.ImVec2(0, 0.3 * em))
            imgui.separator()
            imgui.dummy(imgui.ImVec2(0, 0.3 * em))
            full = imgui.get_content_region_avail().x
            imgui.text_colored(DIM, f"{fa.ICON_FA_USER}  Username")
            imgui.set_next_item_width(full)
            if imgui.is_window_appearing():
                imgui.set_keyboard_focus_here()
            _, self.username = imgui.input_text_with_hint("##user", "username", self.username)
            imgui.text_colored(DIM, f"{fa.ICON_FA_LOCK}  Password")
            imgui.set_next_item_width(full)
            flags_password = imgui.InputTextFlags_.password | imgui.InputTextFlags_.enter_returns_true
            entered, self.password = imgui.input_text_with_hint("##password", "password", self.password, flags_password)
            if self.error_login:
                draw_wrapped(f"{fa.ICON_FA_CIRCLE_EXCLAMATION}  {self.error_login}", ERROR)
            imgui.dummy(imgui.ImVec2(0, 0.4 * em))
            ready = bool(self.username.strip()) and bool(self.password)
            imgui.begin_disabled(not ready)
            idl.push_button_style(THEME, primary=True)
            clicked = imgui.button("Sign In", imgui.ImVec2(full, 1.9 * em))
            idl.pop_button_style()
            imgui.end_disabled()
            if clicked or (entered and ready):
                self.sign_in()
            imgui.dummy(imgui.ImVec2(0, 0.2 * em))
            idl.center_text(f"demo login:  {auth.DEMO_USER}  /  {auth.DEMO_PASSWORD}", DIM)
        imgui.pop_style_var(2)
        imgui.pop_style_color()

    def draw_rail(self) -> None:
        """One button per page, the current one lit, then recent sources that add themselves to a transfer."""
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
        """The page's title and line, then the page."""
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
        elif key == "metadata":
            self.draw_metadata()
        elif key == "browse":
            self.draw_browse()
        elif key == "find":
            self.draw_find()
        else:
            self.draw_storage()

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
        """Begin a two column table: dim captions in a fixed column, widgets filling the rest; end it with end_table."""
        if not imgui.begin_table(f"##form_{name}", 2, imgui.TableFlags_.sizing_stretch_prop):
            return False
        imgui.table_setup_column("##caption", imgui.TableColumnFlags_.width_fixed, WIDTH_CAPTION_EM * imgui.get_font_size())
        imgui.table_setup_column("##widget", imgui.TableColumnFlags_.width_stretch)
        return True

    def row(self, caption: str) -> None:
        """Start a form row: the dim caption, then the cursor in the widget column at full width."""
        imgui.table_next_row()
        imgui.table_next_column()
        imgui.align_text_to_frame_padding()
        imgui.text_disabled(caption)
        imgui.table_next_column()
        imgui.set_next_item_width(-1)

    def choice(self, label: str, value: str, items: list[str]) -> str:
        """A combo over items holding value, which is added when it is not one of them."""
        items = items if value in items else [value, *items]
        _, index = imgui.combo(label, items.index(value), items)
        return items[index]

    def draw_home(self) -> None:
        """Cards for the three things to do, then where data may go and what lands there."""
        em = imgui.get_font_size()
        w = imgui.get_content_region_avail().x
        gap = 1.0 * em
        cards = [page for page in PAGES if page[0] in ("transfer", "metadata", "browse", "find")]
        width = (w - gap * (len(cards) - 1)) / len(cards)
        for i, (key, icon, name, hint) in enumerate(cards):
            if i:
                imgui.same_line(0, gap)
            if idl.icon_button(icon, name, imgui.ImVec2(width, 3.2 * em), theme=THEME, tooltip=hint):
                self.page = key
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        draw_wrapped(
            "Fill in the recording on Metadata, pick files and a BioHPC folder on Transfer, and send: the data is copied "
            "as is and a sidecar holding its Lab4 key lands beside it. Browse storage attaches the same sidecar to data "
            "already on BioHPC, and its Scan catalogs a folder so Find data can search it and open what it finds in mbo "
            "or masknmf.",
            DIM,
        )
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FOLDER_OPEN}  Destinations")
        draw_hint("edit these on the Storage page")
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
        """Sources, destination, metadata, then the running transfer's progress."""
        em = imgui.get_font_size()
        self.step(1, "Sources", "files and folders on this machine; each is copied under its own name")
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
        self.step(2, "Destination", "a BioHPC folder; a subfolder is made when missing")
        labels = [path for path, _ in self.settings.destinations] + [CUSTOM]
        self.index_dest = min(self.index_dest, len(labels) - 1)
        if self.begin_form("dest"):
            self.row("folder")
            _, self.index_dest = imgui.combo("##dest", self.index_dest, labels)
            if self.index_dest < len(self.settings.destinations):
                imgui.text_disabled(self.settings.destinations[self.index_dest][1])
            else:
                self.row("path")
                _, self.custom_dest = imgui.input_text_with_hint("##custom", "/path/on/biohpc", self.custom_dest)
            self.row("subfolder")
            _, self.subfolder = imgui.input_text_with_hint("##sub", "optional", self.subfolder)
            self.row("lands in")
            draw_wrapped(self.destination() or "choose a folder", PATH if self.destination() else DIM)
            imgui.end_table()

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(3, "Metadata", f"written as <name>{SIDECAR_SUFFIX} beside each source")
        _, self.attach = imgui.checkbox("Attach a metadata sidecar", self.attach)
        if self.attach:
            imgui.same_line()
            imgui.text_colored(KEY, self.recording.label())
            imgui.same_line()
            if imgui.small_button("edit"):
                self.page = "metadata"
        _, self.overwrite = imgui.checkbox("Overwrite what is already at the destination", self.overwrite)

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

    def draw_metadata(self) -> None:
        """The recording's identity, acquisition and notes, extra fields, then the sidecar they make."""
        em = imgui.get_font_size()
        rec = self.recording
        self.step(1, "Recording", "the Lab4 DataJoint key this data registers under")
        if self.begin_form("key"):
            self.row("experimenter")
            rec.experimenter = self.choice("##experimenter", rec.experimenter, lab4.EXPERIMENTERS)
            self.row("experiment group")
            _, rec.expt_group = imgui.input_text_with_hint("##group", "study / cohort", rec.expt_group)
            self.row("subject")
            _, rec.subject = imgui.input_text_with_hint("##subject", "mouse id", rec.subject)
            self.row("session date")
            _, rec.session_date = imgui.input_text_with_hint("##date", "YYYY-MM-DD", rec.session_date)
            self.row("recording #")
            _, rec.recording_idx = imgui.input_int("##rec", rec.recording_idx)
            rec.recording_idx = max(1, rec.recording_idx)
            imgui.end_table()
        for problem in rec.problems():
            draw_wrapped(f"{fa.ICON_FA_CIRCLE_INFO}  {problem}", WARN)

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(2, "Acquisition", "how the data was recorded")
        if self.begin_form("acquisition"):
            self.row("device")
            rec.device = self.choice("##device", rec.device, lab4.DEVICES)
            self.row("indicator")
            rec.indicator = self.choice("##indicator", rec.indicator, lab4.INDICATORS)
            self.row("region")
            rec.region = self.choice("##region", rec.region, lab4.REGIONS)
            self.row("notes")
            _, rec.notes = imgui.input_text_multiline("##notes", rec.notes, imgui.ImVec2(-1, 3.5 * em))
            imgui.end_table()

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(3, "Extra fields", "any other key and value to keep with the data")
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

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        imgui.text_colored(ACCENT, f"{fa.ICON_FA_FILE_CODE}  {rec.label()}{SIDECAR_SUFFIX}")
        draw_hint("what a transfer or an attach writes")
        text = json.dumps(rec.sidecar(user=self.user), indent=2)
        lines = text.count("\n") + 1
        imgui.push_style_color(imgui.Col_.text, CODE)
        imgui.input_text_multiline("##preview", text, imgui.ImVec2(-1, lines * imgui.get_text_line_height() + em), imgui.InputTextFlags_.read_only)
        imgui.pop_style_color()

    def draw_browse(self) -> None:
        """
        A folder's listing with what the catalog knows of each item; double-click opens a dataset in its viewer or
        enters a folder, and the selected item's kind and sidecar show below.
        """
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
            idl.wrapped_tooltip("catalog this folder and everything under it: what each folder holds and every dataset mbo or masknmf opens")
        imgui.same_line()
        imgui.set_next_item_width(-1)
        entered, self.path_browse = imgui.input_text("##path", self.path_browse, imgui.InputTextFlags_.enter_returns_true)
        if entered:
            self.browse(path=self.path_browse)

        summary, scan, items = self.described["folder"], self.described["scan"], self.described["items"]
        if scanning:
            imgui.text_colored(ACCENT, f"scanning {self.path_browse}... {self.count_scanned} items listed")
        elif scan is None:
            imgui.text_disabled("not catalogued; Scan indexes this folder and everything under it")
        else:
            kinds = summary["kinds"] if summary else {}
            below = f"{sum(kinds.values())} datasets below: {summarize(kinds)}" if kinds else "no datasets below"
            size = f" · {human_bytes(summary['size'])} in {summary['n_files']} files" if summary else ""
            imgui.text_disabled(f"catalogued {scan['scanned_at'].replace('T', ' ')} · {below}{size}")
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
                clicked, _ = imgui.selectable(f"{icon}  {entry.name}", entry.name == self.selected, imgui.SelectableFlags_.span_all_columns)
                if sidecar or kind is not None:
                    imgui.pop_style_color()
                if clicked:
                    self.select(name=entry.name)
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    path = f"{self.path_browse.rstrip('/')}/{entry.name}"
                    if kind is not None:
                        self.open_dataset(path=path, kind=kind)
                    elif entry.is_dir:
                        self.browse(path=path)
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
        imgui.text_disabled("double-click a dataset to open it in its viewer, a folder to enter it")

        if self.selected:
            item = items.get(self.selected, {})
            imgui.text_colored(ACCENT, f"{fa.ICON_FA_TAGS}  {self.selected}")
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
                imgui.input_text_multiline("##sidecar", self.sidecar, imgui.ImVec2(-1, -1), imgui.InputTextFlags_.read_only)
                imgui.pop_style_color()
            else:
                draw_wrapped(f"no metadata yet; Attach metadata writes {self.recording.label()} beside it", DIM)

    def draw_find(self) -> None:
        """A search over the catalog, its results table, then raw SQL over the same tables."""
        em = imgui.get_font_size()
        imgui.set_next_item_width(-12 * em)
        changed, self.query = imgui.input_text_with_hint("##query", "words in the path or metadata, e.g. M231 2026-08 kind:mesc", self.query)
        imgui.same_line()
        imgui.set_next_item_width(-1)
        kinds = ["any kind", *KINDS]
        index = kinds.index(self.query_kind) if self.query_kind in kinds else 0
        changed_kind, index = imgui.combo("##kind", index, kinds)
        self.query_kind = "" if index == 0 else kinds[index]
        if changed or changed_kind:
            self.search()
        if not self.results and not self.query and not self.query_kind:
            draw_wrapped("Nothing catalogued for this storage yet: Scan a folder on Browse storage.", DIM)
        else:
            imgui.text_disabled(f"{len(self.results)} datasets{' (first 500)' if len(self.results) == 500 else ''}; double-click one to open it")

        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.scroll_y | imgui.TableFlags_.resizable
        height = max(8 * em, imgui.get_content_region_avail().y - 6 * em)
        if imgui.begin_table("##results", 5, flags, imgui.ImVec2(0, height)):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column("dataset", imgui.TableColumnFlags_.width_stretch, 2.0)
            imgui.table_setup_column("kind", imgui.TableColumnFlags_.width_fixed, 8 * em)
            imgui.table_setup_column("recording", imgui.TableColumnFlags_.width_stretch, 1.0)
            imgui.table_setup_column("size", imgui.TableColumnFlags_.width_fixed, 5 * em)
            imgui.table_setup_column("modified", imgui.TableColumnFlags_.width_fixed, 8 * em)
            imgui.table_headers_row()
            for i, row in enumerate(self.results):
                imgui.table_next_row()
                imgui.table_next_column()
                selected = self.result is not None and self.result["path"] == row["path"]
                clicked, _ = imgui.selectable(f"{row['name']}##result_{i}", selected, imgui.SelectableFlags_.span_all_columns)
                hovered = imgui.is_item_hovered()
                imgui.same_line()
                imgui.text_disabled(posixpath.dirname(row["path"]))
                if hovered:
                    idl.wrapped_tooltip(row["path"])
                if clicked:
                    self.result = row
                if clicked and imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                    self.open_dataset(path=row["path"], kind=row["kind"])
                imgui.table_next_column()
                imgui.text_colored(KEY, row["kind"])
                imgui.table_next_column()
                imgui.text_disabled(row["recording"] or "")
                imgui.table_next_column()
                imgui.text_disabled(human_bytes(row["size"]) if row["size"] else "")
                imgui.table_next_column()
                imgui.text_disabled(format_time(row["mtime"]))
            imgui.end_table()
        if self.result is not None:
            imgui.text_colored(PATH, self.result["path"])
            imgui.same_line()
            if imgui.small_button("show in Browse"):
                self.page = "browse"
                self.browse(path=posixpath.dirname(self.result["path"]))
                self.selected = self.result["name"]

        if imgui.collapsing_header("SQL"):
            draw_wrapped("Read-only SQL over the catalog: datasets, folders, metadata (sidecars) and scans.", DIM)
            _, self.text_sql = imgui.input_text_multiline("##sql", self.text_sql, imgui.ImVec2(-1, 3.5 * em))
            if imgui.button(f"{fa.ICON_FA_PLAY}  Run"):
                self.submit("sql", self.catalog.sql, self.text_sql)
            if self.error_sql:
                draw_wrapped(self.error_sql, ERROR)
            columns, rows = self.table_sql
            if columns and imgui.begin_table("##sql_rows", len(columns), flags, imgui.ImVec2(0, 12 * em)):
                imgui.table_setup_scroll_freeze(0, 1)
                for column in columns:
                    imgui.table_setup_column(column)
                imgui.table_headers_row()
                for row in rows:
                    imgui.table_next_row()
                    for value in row:
                        imgui.table_next_column()
                        imgui.text("" if value is None else str(value))
                imgui.end_table()

    def draw_storage(self) -> None:
        """The backend and how to reach it, then the destinations offered on Transfer."""
        em = imgui.get_font_size()
        s = self.settings
        self.step(1, "Connection", "nothing here signs in: a mounted share uses this machine's mount, ssh its keys")
        for backend, label in zip(BACKENDS, ("Mounted share", "SSH")):
            if imgui.radio_button(label, s.backend == backend):
                s.backend = backend
            imgui.same_line()
        imgui.new_line()
        if self.begin_form("connection"):
            if s.backend == "mounted":
                self.row("mount root")
                _, s.mount_root = imgui.input_text_with_hint("##root", r"e.g. Z:\ or \\lamella.biohpc.swmed.edu\share", s.mount_root)
            else:
                self.row("host")
                _, s.ssh_host = imgui.input_text_with_hint("##host", "ssh alias or user@host", s.ssh_host)
            imgui.end_table()
        hint = (
            "BioHPC paths resolve under this folder: /archive/x is <root>/archive/x"
            if s.backend == "mounted"
            else "ssh runs with BatchMode, so the key must already be set up (ssh-copy-id)"
        )
        draw_wrapped(hint, DIM)

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(2, "Destinations", "the folders Transfer offers")
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

        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        self.step(3, "Viewers", "what opens a dataset on a double-click: a name on PATH or the executable's full path")
        if self.begin_form("viewers"):
            self.row("mbo")
            _, s.command_mbo = imgui.input_text_with_hint("##mbo", "mbo", s.command_mbo)
            self.row("masknmf")
            _, s.command_masknmf = imgui.input_text_with_hint("##masknmf", "masknmf", s.command_masknmf)
            imgui.end_table()
        for name, command in (("mbo", s.command_mbo), ("masknmf", s.command_masknmf)):
            found = shutil.which(command)
            imgui.text_colored(OK if found else WARN, f"{name}: {found or 'not found; datasets it opens will not open'}")
        imgui.dummy(imgui.ImVec2(0, 0.6 * em))
        draw_wrapped(f"saved to {data_dir() / 'settings.json'}", DIM)

    def draw_footer(self, dialog: idl.FileDialog) -> None:
        """What blocks the page's action or the last outcome on the left; Sign out, Quit and the action on the right."""
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
            idl.text_wrapped_colored(DIM, "sign in to reach BioHPC storage")
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
