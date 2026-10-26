"""
A SQLite catalog of BioHPC storage and the lab's own machines: datasets mbo or masknmf can open, folder totals,
Lab4 sidecars, and for the Results page each recording's units, the pipeline runs on them and their result files.

A scan reads the listing, the sidecars and run manifests, and the headers of .mesc files it can open in place, so
over ssh it costs about the same as over a mount but finds no units inside a .mesc.
"""

import argparse
import functools
import json
import logging
import posixpath
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

from imgui_lab4_interface import processing
from imgui_lab4_interface.config import Settings, data_dir
from imgui_lab4_interface.storage import SIDECAR_SUFFIX, SUFFIX_VNOISER, Storage, machine_roots, open_machine

SUFFIXES_TIFF = (".tif", ".tiff")
SUFFIXES_HDF5 = (".h5", ".hdf5")
SUFFIX_TRACKING = "_roicat-tracking-manifest.json"
NAME_VOLTAGE = "denoised_trace_scans.pkl"
EVERY_RECORDS = 2000
# files the Results page lists under a run or a recording
SUFFIXES_RESULT = (".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".html", ".csv", ".tsv", ".mp4", ".webm", ".mov", ".avi")
# a unit exported from a .mesc, e.g. GT279-1_spine__MSession_0_MUnit_14.tiff
PATTERN_TIFF_UNIT = re.compile(r"^(.+)__(MSession_\d+)_(MUnit_\d+)\.tiff?$", re.IGNORECASE)
PATTERN_SESSION_UNIT = re.compile(r"(MSession_\d+)[/_](MUnit_\d+)")
PATTERN_UNIT = re.compile(r"MUnit_\d+")
PATTERN_STARTED_MASKNMF = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})")
PATTERN_STARTED_VNOISER = re.compile(r"\.(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-\d{2}" + re.escape(SUFFIX_VNOISER) + "$")
PATTERN_FRAME_RATE = re.compile(r"--frame-rate[ =]([0-9.]+)")
PATTERN_ERROR = re.compile(r"\b\w*(Error|Exception)\b:")
PATTERN_INDICATOR = re.compile(r"(?i)asap\d+|j?gcamp\w*|jrgeco\w*|voltron\w*")

# kind: (viewer, args before the path, description)
KINDS = {
    "masknmf-run": ("masknmf", ["view"], "masknmf run folder: config.json and its results"),
    "masknmf-results": ("masknmf", ["view"], "masknmf results file"),
    "roicat-tracking": ("masknmf", ["view"], "ROICaT tracking run across sessions"),
    "suite2p-run": ("mbo", [], "suite2p output holding plane folders"),
    "suite2p-plane": ("mbo", [], "suite2p plane folder (ops.npy)"),
    "voltage-pf": ("mbo", ["curate"], "vnoiser voltage results (PF folder)"),
    "mesc": ("mbo", [], "Femtonics .mesc recording"),
    "zarr": ("mbo", [], "zarr array"),
    "tiff-series": ("mbo", [], "folder of tiff files"),
    "tiff": ("mbo", [], "tiff stack"),
    "hdf5": ("mbo", [], "hdf5 file"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    storage TEXT, path TEXT, parent TEXT, name TEXT, kind TEXT, detail TEXT,
    n_files INTEGER, size INTEGER, mtime REAL, has_metadata INTEGER,
    PRIMARY KEY (storage, path)
);
CREATE INDEX IF NOT EXISTS datasets_parent ON datasets (storage, parent);
CREATE INDEX IF NOT EXISTS datasets_kind ON datasets (storage, kind);
CREATE TABLE IF NOT EXISTS folders (
    storage TEXT, path TEXT, parent TEXT, name TEXT,
    n_files INTEGER, size INTEGER, mtime REAL, kinds TEXT,
    PRIMARY KEY (storage, path)
);
CREATE INDEX IF NOT EXISTS folders_parent ON folders (storage, parent);
CREATE TABLE IF NOT EXISTS metadata (
    storage TEXT, path TEXT, recording TEXT, subject TEXT, session_date TEXT, experimenter TEXT, body TEXT,
    PRIMARY KEY (storage, path)
);
CREATE TABLE IF NOT EXISTS units (
    storage TEXT, path TEXT, unit TEXT, recording TEXT, role TEXT, method INTEGER, frames INTEGER, fs REAL, mtime REAL,
    PRIMARY KEY (storage, path, unit)
);
CREATE TABLE IF NOT EXISTS runs (
    storage TEXT, path TEXT, kind TEXT, pipeline TEXT, status TEXT, started TEXT, input TEXT, fs REAL, error TEXT,
    log TEXT, recording TEXT, unit TEXT,
    PRIMARY KEY (storage, path)
);
CREATE TABLE IF NOT EXISTS results (
    storage TEXT, path TEXT, size INTEGER, mtime REAL, run TEXT, recording TEXT, unit TEXT,
    PRIMARY KEY (storage, path)
);
CREATE TABLE IF NOT EXISTS scans (
    storage TEXT, root TEXT, scanned_at TEXT, n_records INTEGER, seconds REAL,
    PRIMARY KEY (storage, root)
);
"""


def classify_folder(name: str, files: set[str], subkinds: set[str]) -> tuple[Optional[str], str]:
    """The kind of a folder and a detail line, from its name, its files' names and its subfolders' kinds."""
    if name.endswith(".zarr"):
        return "zarr", ""
    if any(f.endswith(SUFFIX_TRACKING) for f in files):
        return "roicat-tracking", ""
    results = sorted(f for f in files if f.lower().endswith(SUFFIXES_HDF5) and "results" in f.lower())
    if "config.json" in files and results:
        return "masknmf-run", ", ".join(results)
    if NAME_VOLTAGE in files:
        return "voltage-pf", ""
    if "ops.npy" in files:
        return "suite2p-plane", "segmented" if "stat.npy" in files else "registered"
    if "suite2p-plane" in subkinds:
        return "suite2p-run", ""
    tiffs = [f for f in files if f.lower().endswith(SUFFIXES_TIFF)]
    if len(tiffs) > 1:
        return "tiff-series", f"{len(tiffs)} tiffs"
    return None, ""


def classify_file(name: str) -> Optional[str]:
    lower = name.lower()
    if lower.endswith(".mesc"):
        return "mesc"
    if lower.endswith(SUFFIXES_TIFF):
        return "tiff"
    if lower.endswith(SUFFIXES_HDF5):
        return "masknmf-results" if "results" in lower else "hdf5"
    return None


def ancestors(path: str) -> Iterator[str]:
    """The folders above path, nearest first."""
    while path not in ("/", ""):
        path = posixpath.dirname(path)
        yield path


def stem(path: str) -> str:
    """A file or folder name without its extension, from a posix or Windows path."""
    return posixpath.splitext(posixpath.basename(path.replace("\\", "/").rstrip("/")))[0]


def format_started(match: Optional[re.Match]) -> str:
    return "{}-{}-{} {}:{}".format(*match.groups()) if match is not None else ""


def read_runs(storage: Storage, folders: list[str], logs: dict[str, str]) -> list[tuple]:
    """
    Runs rows for the folders whose config.json is a masknmf run manifest, with the last error line of failed runs.

    logs maps a folder to its first .log file.
    """
    texts = storage.read_texts([f"{folder}/config.json" for folder in folders])
    runs = []
    for folder in folders:
        try:
            body = json.loads(texts.get(f"{folder}/config.json", ""))
        except ValueError:
            # a running pipeline can be mid-write; the next scan picks it up
            continue
        if not isinstance(body, dict) or "masknmf_version" not in body:
            continue
        run = body.get("run") if isinstance(body.get("run"), dict) else {}
        data = (body.get("inputs") or {}).get("data") or {}
        rate = PATTERN_FRAME_RATE.search(run.get("command") or "")
        started = format_started(PATTERN_STARTED_MASKNMF.match(run.get("started") or ""))
        row = [folder, "masknmf-run", body.get("pipeline", ""), run.get("status", ""), started, data.get("path", "")]
        runs.append(row + [float(rate.group(1)) if rate else None, "", logs.get(folder, "")])
    failed = {row[-1]: row for row in runs if row[3] == "failed" and row[-1]}
    for log, text in storage.read_texts(list(failed)).items():
        errors = [line.strip() for line in text.splitlines() if PATTERN_ERROR.search(line)]
        failed[log][7] = errors[-1] if errors else ""
    return [tuple(row) for row in runs]


def read_units(storage: Storage, path: str, mtime: float) -> list[tuple]:
    """Units rows for a .mesc from its headers when it is a file here, else one row for the recording."""
    recording = stem(path)
    try:
        local = storage.local_path(path)
        units = [] if local is None else processing.list_units(local)
    except (OSError, KeyError, ValueError):
        units = []
    if not units:
        return [(path, "", recording, "", 0, None, None, mtime)]
    return [(path, u.key, recording, u.role, u.method, u.frames, u.fs, mtime) for u in units]


def match_unit(paths: list[str], recordings: list[str], by_folder: dict[str, str], known: dict[str, set]) -> tuple[str, str]:
    """
    The (recording, unit) paths refer to, "" where unknown.

    The recording is the longest of recordings named in the paths, else the one whose .mesc sits in a folder above
    them (by_folder), else the only one starting with a file name's part before _MUnit. The unit is the last
    MSession_N_MUnit_N named; a bare MUnit_N is the recording's only known unit of that number, else MSession_0's.
    """
    paths = [p.replace("\\", "/") for p in paths if p]
    text = " ".join(paths)
    recording = next((r for r in sorted(recordings, key=len, reverse=True) if r in text), "")
    if not recording:
        recording = next((by_folder[a] for p in paths for a in ancestors(p) if by_folder.get(a)), "")
    if not recording:
        prefixes = {posixpath.basename(p).split("_MUnit")[0] for p in paths if "_MUnit" in posixpath.basename(p)}
        candidates = {r for r in recordings for prefix in prefixes if r.startswith(prefix + "_")}
        recording = candidates.pop() if len(candidates) == 1 else ""
    pairs = PATTERN_SESSION_UNIT.findall(text)
    if pairs:
        return recording, "/".join(pairs[-1])
    bare = PATTERN_UNIT.findall(text)
    if bare:
        candidates = [u for u in known.get(recording, ()) if u.endswith(f"/{bare[-1]}")]
        return recording, candidates[0] if len(candidates) == 1 else f"MSession_0/{bare[-1]}"
    return recording, ""


def indicator(recording: str) -> str:
    """The indicator a recording's name gives, e.g. ASAP7 or jGCaMP8m; "" for none."""
    tokens = [t for t in recording.split("_") if PATTERN_INDICATOR.fullmatch(t)]
    return "" if not tokens else tokens[0].upper() if tokens[0].lower().startswith("asap") else tokens[0]


def is_movie(unit: dict) -> bool:
    """Whether a units row is something a pipeline runs on: an exported unit, or a time series scan."""
    return unit["role"] == "export" or (unit["role"] in (processing.ROLE_MEASUREMENT, "") and unit["method"] in processing.METHODS_MOVIE)


def order_unit(unit: str) -> tuple:
    """Sort MSession_0/MUnit_10 after MSession_0/MUnit_9."""
    return tuple(int(n) for n in re.findall(r"\d+", unit)) or (-1,)


class Catalog:
    """The SQLite catalog; catalog.sqlite in the data folder by default."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or data_dir() / "catalog.sqlite").absolute()
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def scan(self, storage: Storage, root: str, progress: Optional[Callable[[int], None]] = None) -> dict:
        """
        Walk root, classify everything, read sidecars, and replace the catalog rows under root.

        progress gets the item count every EVERY_RECORDS items. Returns root, records, datasets and seconds.
        """
        started = time.perf_counter()
        root = root.rstrip("/") or "/"
        children = defaultdict(list)
        n_records = 0
        for record in storage.walk(root):
            children[posixpath.dirname(record.path)].append(record)
            n_records += 1
            if progress is not None and n_records % EVERY_RECORDS == 0:
                progress(n_records)
        sidecars = {r.path[: -len(SIDECAR_SUFFIX)]: r.path for rs in children.values() for r in rs if r.path.endswith(SIDECAR_SUFFIX)}
        folders = sorted({root, *(r.path for rs in children.values() for r in rs if r.is_dir)}, key=lambda p: p.count("/"), reverse=True)

        datasets, summaries, totals = [], [], {}
        configs, logs, vnoiser, units, results = [], {}, [], [], []
        for folder in folders:
            records = children.get(folder, [])
            files = {posixpath.basename(r.path) for r in records if not r.is_dir}
            if "config.json" in files:
                configs.append(folder)
                logs[folder] = min((r.path for r in records if r.path.endswith(".log")), default="")
            if folder.endswith(SUFFIX_VNOISER):
                vnoiser.append((folder, "vnoiser", "vnoiser", "done", format_started(PATTERN_STARTED_VNOISER.search(folder)), "", None, "", ""))
            for r in records:
                name = posixpath.basename(r.path)
                exported = None if r.is_dir else PATTERN_TIFF_UNIT.match(name)
                if exported is not None:
                    units.append((r.path, "/".join(exported.groups()[1:]), exported.group(1), "export", 0, None, None, r.mtime))
                if not r.is_dir and name.lower().endswith(SUFFIXES_RESULT):
                    results.append((r.path, r.size, r.mtime))
            subkinds = {totals[r.path][4] for r in records if r.is_dir and r.path in totals}
            kind, detail = classify_folder(posixpath.basename(folder), files, subkinds)
            n_files, size, mtime, kinds = 0, 0, 0.0, Counter()
            for r in records:
                mtime = max(mtime, r.mtime)
                if r.is_dir and r.path in totals:
                    sub_files, sub_size, sub_mtime, sub_kinds, _ = totals[r.path]
                    n_files, size, mtime = n_files + sub_files, size + sub_size, max(mtime, sub_mtime)
                    kinds += sub_kinds
                elif not r.is_dir:
                    n_files, size = n_files + 1, size + r.size
                    file_kind = classify_file(posixpath.basename(r.path))
                    if file_kind is not None:
                        datasets.append((r.path, folder, file_kind, "", 1, r.size, r.mtime))
                        kinds[file_kind] += 1
            summaries.append((folder, posixpath.dirname(folder), n_files, size, mtime, json.dumps(dict(kinds))))
            if kind is not None:
                datasets.append((folder, posixpath.dirname(folder), kind, detail, n_files, size, mtime))
                kinds[kind] += 1
            totals[folder] = (n_files, size, mtime, kinds, kind)

        runs = read_runs(storage, configs, logs) + vnoiser
        label = storage.label
        # .mesc headers read before, by path and mtime
        read = defaultdict(list)
        with closing(self.connect()) as db:
            for row in db.execute("SELECT * FROM units WHERE storage = ? AND role != 'export' AND unit != ''", (label,)):
                read[(row["path"], row["mtime"])].append(tuple(row)[1:])
        for path, _, kind, _, _, _, mtime in datasets:
            if kind == "mesc":
                units += read.get((path, mtime)) or read_units(storage, path, mtime)

        metadata = []
        for target, sidecar in sidecars.items():
            try:
                body = json.loads(storage.read_text(sidecar))
            except (OSError, ValueError):
                continue
            if not isinstance(body, dict):
                continue
            key = body.get("key") if isinstance(body.get("key"), dict) else {}
            row = (target, body.get("recording", ""), key.get("subject_id", ""), key.get("session_datetime", ""), key.get("experimenter", ""))
            metadata.append((*row, json.dumps(body)))

        prefix = "/" if root == "/" else f"{root}/"
        seconds = time.perf_counter() - started
        with closing(self.connect()) as db, db:
            for table in ("datasets", "folders", "metadata", "units", "runs", "results"):
                db.execute(f"DELETE FROM {table} WHERE storage = ? AND (path = ? OR substr(path, 1, ?) = ?)", (label, root, len(prefix), prefix))
            db.executemany(
                "INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(label, p, parent, posixpath.basename(p), k, d, n, s, m, p in sidecars) for p, parent, k, d, n, s, m in datasets],
            )
            db.executemany(
                "INSERT INTO folders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(label, p, parent, posixpath.basename(p), n, s, m, k) for p, parent, n, s, m, k in summaries],
            )
            db.executemany("INSERT INTO metadata VALUES (?, ?, ?, ?, ?, ?, ?)", [(label, *row) for row in metadata])
            db.executemany("INSERT OR REPLACE INTO units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [(label, *row) for row in units])
            db.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')", [(label, *row) for row in runs])
            db.executemany("INSERT INTO results VALUES (?, ?, ?, ?, '', '', '')", [(label, *row) for row in results])
            db.execute(
                "INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?)",
                (label, root, datetime.now().isoformat(timespec="seconds"), n_records, seconds),
            )
        return {"root": root, "records": n_records, "datasets": len(datasets), "runs": len(runs), "units": len(units), "seconds": seconds}

    def describe(self, storage_label: str, folder: str) -> dict:
        """The folder's summary, the latest scan covering it, and its items by name."""
        folder = folder.rstrip("/") or "/"
        with closing(self.connect()) as db:
            summary = db.execute("SELECT * FROM folders WHERE storage = ? AND path = ?", (storage_label, folder)).fetchone()
            scan = db.execute(
                "SELECT * FROM scans WHERE storage = ? AND (root = ? OR root = '/' OR substr(?, 1, length(root) + 1) = root || '/') "
                "ORDER BY scanned_at DESC LIMIT 1",
                (storage_label, folder, folder),
            ).fetchone()
            items = {}
            for row in db.execute("SELECT * FROM folders WHERE storage = ? AND parent = ? AND path != ?", (storage_label, folder, folder)):
                items[row["name"]] = {**dict(row), "kinds": json.loads(row["kinds"])}
            for row in db.execute("SELECT * FROM datasets WHERE storage = ? AND parent = ? AND path != ?", (storage_label, folder, folder)):
                items.setdefault(row["name"], {}).update(dict(row))
        return {
            "folder": None if summary is None else {**dict(summary), "kinds": json.loads(summary["kinds"])},
            "scan": None if scan is None else dict(scan),
            "items": items,
        }

    def link(self) -> None:
        """Name the recording and unit of every run and result, from the units and paths of every storage."""
        with closing(self.connect()) as db, db:
            known, by_folder, recordings = defaultdict(set), {}, set()
            for row in db.execute("SELECT path, unit, recording FROM units"):
                recordings.add(row["recording"])
                if row["unit"]:
                    known[row["recording"]].add(row["unit"])
                if row["path"].lower().endswith(".mesc"):
                    folder = posixpath.dirname(row["path"])
                    # a folder holding two recordings names neither
                    by_folder[folder] = row["recording"] if by_folder.get(folder, row["recording"]) == row["recording"] else ""
            recordings = sorted(recordings - {""})
            runs = {}
            for row in db.execute("SELECT storage, path, input FROM runs").fetchall():
                recording, unit = match_unit([row["input"], row["path"]], recordings, by_folder, known)
                runs[(row["storage"], row["path"])] = (recording or (stem(row["input"]) if row["input"] else ""), unit)
            db.executemany("UPDATE runs SET recording = ?, unit = ? WHERE storage = ? AND path = ?", [(*named, *key) for key, named in runs.items()])
            linked = []
            for row in db.execute("SELECT storage, path FROM results").fetchall():
                run = next((folder for folder in ancestors(row["path"]) if (row["storage"], folder) in runs), "")
                named = runs[(row["storage"], run)] if run else match_unit([row["path"]], recordings, by_folder, known)
                linked.append((run, *named, row["storage"], row["path"]))
            db.executemany("UPDATE results SET run = ?, recording = ?, unit = ? WHERE storage = ? AND path = ?", linked)

    def overview(self) -> dict:
        """
        The Results page's rows, one per recording and unit, with the unit's data files, runs and results, and every
        scan. A row with no data file of its own offers its recording's file, else its runs' inputs. Snapshot units
        nothing used are left out, as is a recording's own row once its units are known.
        """
        with closing(self.connect()) as db:
            units = [dict(row) for row in db.execute("SELECT * FROM units ORDER BY storage, path")]
            runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY started DESC, path")]
            results = [dict(row) for row in db.execute("SELECT * FROM results WHERE recording != '' ORDER BY path")]
            labels = {(row["storage"], row["path"]): row["recording"] for row in db.execute("SELECT storage, path, recording FROM metadata")}
            scans = [dict(row) for row in db.execute("SELECT * FROM scans ORDER BY storage, root")]
        rows = {}
        for kind, items in (("data", units), ("runs", runs), ("results", results)):
            for item in items:
                key = (item["recording"], item["unit"])
                if key not in rows:
                    rows[key] = {"recording": key[0], "unit": key[1], "fs": None, "frames": None, "lab4": "", "data": [], "runs": [], "results": []}
                rows[key][kind].append(item)
        for row in rows.values():
            row["fs"] = next((d["fs"] for d in row["data"] + row["runs"] if d["fs"]), None)
            row["frames"] = next((d["frames"] for d in row["data"] if d["frames"]), None)
            row["lab4"] = next((labels[(d["storage"], d["path"])] for d in row["data"] if labels.get((d["storage"], d["path"]))), "")
            row["indicator"] = indicator(row["recording"])
            if not row["data"] and (row["recording"], "") in rows:
                row["data"] = rows[(row["recording"], "")]["data"]
            if not row["data"]:
                inputs = dict.fromkeys((run["storage"], run["input"]) for run in row["runs"] if run["input"])
                row["data"] = [{"storage": storage, "path": path, "unit": row["unit"], "role": "input", "method": 0, "frames": None, "fs": None} for storage, path in inputs]
            row["storages"] = sorted({d["storage"] for d in row["data"] + row["runs"] + row["results"]})
        with_units = {recording for recording, unit in rows if unit}
        kept = [
            row
            for row in rows.values()
            if row["runs"] or row["results"] or (row["unit"] and any(is_movie(d) for d in row["data"])) or (not row["unit"] and row["recording"] not in with_units)
        ]
        return {"rows": sorted(kept, key=lambda row: (row["recording"].lower(), order_unit(row["unit"]))), "scans": scans}


def scan_machines(catalog: Catalog, machines: list[dict], progress: Optional[Callable[[str, int], None]] = None) -> tuple[list[dict], list[str]]:
    """
    Scan every root of machines, then link the whole catalog.

    progress gets "machine root" and the item count as a scan goes. Returns each scan's summary, with its machine,
    and a line per root that could not be scanned; those keep their last scan's rows.
    """
    found, failed = [], []
    for machine in machines:
        storage = open_machine(machine)
        try:
            roots = machine_roots(machine)
        except ValueError as e:
            failed.append(f"{machine['name']}: {e}")
            continue
        for root in roots:
            where = f"{machine['name']} {root}"
            if progress is not None:
                progress(where, 0)
            try:
                found.append({"machine": machine["name"], **catalog.scan(storage, root, None if progress is None else functools.partial(progress, where))})
            except OSError as e:
                failed.append(f"{where}: {e}")
    catalog.link()
    return found, failed


def main() -> None:
    """`lab4-scan`: catalog the machines in Settings, e.g. from a timer, so the Results page is current."""
    parser = argparse.ArgumentParser(prog="lab4-scan", description="Catalog the roots of the machines in Settings > Machines.")
    parser.add_argument("names", nargs="*", help="machines to scan; all of them when none are named")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    unknown = sorted(set(args.names) - {machine["name"] for machine in settings.machines})
    if unknown:
        parser.error(f"no machine named {', '.join(unknown)} in {data_dir() / 'settings.json'}")
    catalog = Catalog()
    found, failed = scan_machines(catalog, [m for m in settings.machines if not args.names or m["name"] in args.names])
    for scan in found:
        logging.info("%s %s: %d items, %d runs, %d units (%.1f s)", scan["machine"], scan["root"], scan["records"], scan["runs"], scan["units"], scan["seconds"])
    for line in failed:
        logging.warning("not scanned, last scan kept: %s", line)
    logging.info("catalog: %s", catalog.path)
