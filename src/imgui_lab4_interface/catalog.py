"""
A catalog of what lives on BioHPC storage: every dataset mbo_utilities or masknmf can open, what each folder holds
beneath it, and the Lab4 sidecars beside the data, kept in SQLite so the window queries it instead of walking again.

A scan reads the listing (names, sizes, times) and the small sidecars only, so it costs the same over a mounted
share as over one ssh `find`. The tables are plain SQL and keyed by storage, ready to move to the lab's database
server when one file is no longer enough.
"""

import json
import posixpath
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from imgui_lab4_interface.config import data_dir
from imgui_lab4_interface.storage import SIDECAR_SUFFIX, Storage

SUFFIXES_TIFF = (".tif", ".tiff")
SUFFIXES_HDF5 = (".h5", ".hdf5")
SUFFIX_TRACKING = "_roicat-tracking-manifest.json"
NAME_VOLTAGE = "denoised_trace_scans.pkl"
EVERY_RECORDS = 2000

# a dataset kind: the viewer that opens it, the arguments before its path, and what it is
KINDS = {
    "masknmf-run": ("masknmf", ["view"], "masknmf run folder: config.json and its results"),
    "masknmf-results": ("masknmf", ["view"], "masknmf results file, at whatever stage it reached"),
    "roicat-tracking": ("masknmf", ["view"], "ROICaT tracking run across sessions"),
    "suite2p-run": ("mbo", [], "suite2p output holding plane folders"),
    "suite2p-plane": ("mbo", [], "suite2p plane folder (ops.npy)"),
    "voltage-pf": ("mbo", ["curate"], "vnoiser voltage results (PF folder), opened in the curation window"),
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


class Catalog:
    """
    The SQLite index of scanned storage.

    Args:
        path (Path | None): The database file; catalog.sqlite in the data folder when None
    """

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
        Walk root, classify every folder and file, read the sidecars, and replace what the catalog held under root.

        Args:
            storage (Storage): Where root is
            root (str): The BioHPC folder to scan
            progress (Callable | None): Called with the number of items listed so far, every EVERY_RECORDS
        Returns:
            dict: root, records listed, datasets found, seconds taken
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
        for folder in folders:
            records = children.get(folder, [])
            files = {posixpath.basename(r.path) for r in records if not r.is_dir}
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

        metadata = []
        for target, sidecar in sidecars.items():
            try:
                body = json.loads(storage.read_text(sidecar))
            except (OSError, ValueError):
                continue
            if not isinstance(body, dict):
                continue
            key = body.get("key") if isinstance(body.get("key"), dict) else {}
            row = (target, body.get("recording", ""), key.get("subject_id", ""), key.get("session_date", ""), key.get("experimenter", ""))
            metadata.append((*row, json.dumps(body)))

        label = storage.label
        prefix = "/" if root == "/" else f"{root}/"
        seconds = time.perf_counter() - started
        with closing(self.connect()) as db, db:
            for table in ("datasets", "folders", "metadata"):
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
            db.execute(
                "INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?)",
                (label, root, datetime.now().isoformat(timespec="seconds"), n_records, seconds),
            )
        return {"root": root, "records": n_records, "datasets": len(datasets), "seconds": seconds}

    def describe(self, storage_label: str, folder: str) -> dict:
        """
        What the catalog knows of a folder: its summary, the latest scan covering it, and each item directly in it.

        Returns:
            dict: "folder" (row or None), "scan" (row or None), "items" (name -> merged folder summary and dataset row)
        """
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

    def search(self, storage_label: str, text: str = "", kind: str = "", limit: int = 500) -> list[dict]:
        """
        Datasets whose path or sidecar matches every word of text, newest first.

        A word kind:<kind> narrows to one kind, as kind does.
        """
        clauses, params = ["d.storage = ?"], [storage_label]
        for term in text.split():
            if term.startswith("kind:"):
                kind = term[len("kind:") :]
                continue
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(d.path LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\')")
            params += [like, like]
        if kind:
            clauses.append("d.kind = ?")
            params.append(kind)
        query = (
            "SELECT d.*, m.recording, m.subject, m.session_date FROM datasets d "
            "LEFT JOIN metadata m ON m.storage = d.storage AND m.path = d.path "
            f"WHERE {' AND '.join(clauses)} ORDER BY d.mtime DESC LIMIT ?"
        )
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(query, (*params, limit))]

    def sql(self, query: str, limit: int = 1000) -> tuple[list[str], list[tuple]]:
        """Run a query on a read-only connection: the column names and up to limit rows."""
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as db:
            cursor = db.execute(query)
            return [c[0] for c in cursor.description or []], [tuple(row) for row in cursor.fetchmany(limit)]
