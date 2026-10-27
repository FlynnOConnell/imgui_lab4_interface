"""
The human slice table: patients, slices and assays in SQLite.

Data stays where Shobhana keeps it on BioHPC. Each slice finds its files with a match pattern over an index of that
folder, so nothing is moved or renamed. Rows belong to whoever added them; only the owner or the admin edits them.
Failed slices stay, with why.
"""

import csv
import posixpath
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from imgui_lab4_interface.config import data_dir
from imgui_lab4_interface.storage import Storage

# bump when the tables change; an older file is renamed aside and the tables reseeded
VERSION_SCHEMA = 3
ADMIN = "shobhana"
PEOPLE = ["shobhana", "dana", "adrian", "flynn"]
# slice data on BioHPC, until the shared humansliceprep folder exists
ROOT = "/biohpc/data4/Shobhana"

SEXES = ["F", "M", "U"]
HEMISPHERES = ["left", "right"]
REGIONS = [
    "hippocampus",
    "middle temporal gyrus",
    "superior temporal gyrus",
    "frontal cortex",
    "parietal cortex",
]
DIAGNOSES = [
    "temporal lobe epilepsy",
    "mesial temporal sclerosis",
    "focal cortical dysplasia",
    "glioma, access tissue",
    "metastasis, access tissue",
]
SITES = ["site A", "site B", "site C"]
MATERIALS = ["none", "mRNA", "plasmid", "RNP", "dye only"]
DYES = [
    "none",
    "Alexa 488",
    "Alexa 594",
    "Lucifer yellow",
    "biocytin",
    "OGB-1",
    "dextran-TMR",
]
EPHYS = ["none", "intrinsic", "synaptic", "intrinsic + synaptic"]
INCUBATOR = [
    "storage solution",
    "chamber, bubbling",
    "culture",
    "out, recorded",
    "out, imaged",
    "out, fixed",
    "n/a",
]
OUTCOMES = ["ok", "failed"]
ASSAYS = ["live-dead", "2P imaging", "histology", "IHC"]

# columns: (name, type, choices or the table a ref points to); joined columns come from the patient, owner is who
# added the row
COLUMNS = {
    "slices": (
        ("collection_date", "joined", None),
        ("region", "choice", REGIONS),
        ("patient_id", "ref", "patients"),
        ("day", "day", None),
        ("slice_id", "id", None),
        ("ep_material", "choice", MATERIALS),
        ("ep_construct", "text", None),
        ("ep_dye", "choice", DYES),
        ("ephys", "choice", EPHYS),
        ("ephys_notes", "text", None),
        ("incubator", "choice", INCUBATOR),
        ("outcome", "choice", OUTCOMES),
        ("failure_reason", "text", None),
        ("experimenter", "choice", PEOPLE),
        ("file_match", "text", None),
        ("source", "text", None),
        ("owner", "owner", None),
        ("notes", "text", None),
    ),
    "patients": (
        ("patient_id", "id", None),
        ("collection_date", "date", None),
        ("age", "int", None),
        ("sex", "choice", SEXES),
        ("hemisphere", "choice", HEMISPHERES),
        ("diagnosis", "choice", DIAGNOSES),
        ("site", "choice", SITES),
        ("protocol", "text", None),
        ("owner", "owner", None),
        ("notes", "text", None),
    ),
    "assays": (
        ("slice_id", "ref", "slices"),
        ("assay", "choice", ASSAYS),
        ("assay_date", "date", None),
        ("percent_live", "int", None),
        ("result", "text", None),
        ("data_path", "text", None),
        ("owner", "owner", None),
        ("notes", "text", None),
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY, patient_id TEXT UNIQUE NOT NULL, collection_date TEXT, age INTEGER, sex TEXT,
    hemisphere TEXT, diagnosis TEXT, site TEXT, protocol TEXT, owner TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS slices (
    id INTEGER PRIMARY KEY, slice_id TEXT UNIQUE NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients (patient_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    region TEXT, day INTEGER, ep_material TEXT, ep_construct TEXT, ep_dye TEXT, ephys TEXT, ephys_notes TEXT,
    incubator TEXT, outcome TEXT, failure_reason TEXT, experimenter TEXT, file_match TEXT, source TEXT,
    owner TEXT NOT NULL, notes TEXT
);
CREATE INDEX IF NOT EXISTS slices_patient ON slices (patient_id);
CREATE TABLE IF NOT EXISTS assays (
    id INTEGER PRIMARY KEY,
    slice_id TEXT NOT NULL REFERENCES slices (slice_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    assay TEXT, assay_date TEXT, percent_live INTEGER, result TEXT, data_path TEXT, owner TEXT NOT NULL, notes TEXT
);
CREATE INDEX IF NOT EXISTS assays_slice ON assays (slice_id);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL);
CREATE TABLE IF NOT EXISTS indexed (root TEXT PRIMARY KEY, indexed_at TEXT, storage TEXT);
"""

# seed rows from Shobhana's file names; patient ids are placeholders
NOTE_PATIENT = "placeholder id from the file names; the real one is in the PowerPoints"
SOURCE = "file names on BioHPC"
SEED_PATIENTS = [
    ("HS260727", "2026-07-27"),
    ("HS260831", "2026-08-31"),
    ("HS260914", "2026-09-14"),
    ("HS260918", "2026-09-18"),
]
# slice id, patient, day, region, electroporated, construct, dye, incubator, file match, notes
SEED_SLICES = [
    (
        "HS260727.A",
        "HS260727",
        1,
        None,
        None,
        "",
        "Alexa 488",
        None,
        "2026-07-27 -_b_ -_c_ -_d_ -24hrs -cell -260727c2",
        "unlettered files of the day",
    ),
    ("HS260727.B", "HS260727", 1, None, None, "", None, None, "2026-07-27 _b_", ""),
    ("HS260727.C", "HS260727", 1, None, None, "", None, None, "2026-07-27 _c_", ""),
    (
        "HS260727.D",
        "HS260727",
        1,
        None,
        None,
        "",
        "Alexa 488",
        None,
        "2026-07-27 _d_",
        "mean image and movie of D",
    ),
    (
        "HS260727.cells",
        "HS260727",
        1,
        None,
        "dye only",
        "",
        "Alexa 488",
        None,
        "2026-07-27 cell",
        "cells 2 to 7 filled with Alexa 488",
    ),
    (
        "HS260727.24h",
        "HS260727",
        2,
        None,
        None,
        "",
        None,
        "chamber, bubbling",
        "2026-07-27 24hrs",
        "24 hrs in the chamber, bubbling",
    ),
    (
        "HS260727.48h",
        "HS260727",
        3,
        None,
        None,
        "",
        None,
        "chamber, bubbling",
        "2026-07-29 48hrs",
        "48 hrs in the chamber, bubbling",
    ),
    (
        "HS260831.A",
        "HS260831",
        1,
        None,
        None,
        "",
        None,
        None,
        "2026-08-31 -day9 -egfp",
        "ephys in SS_Ephys_HSlice_8-31-2026.pxp",
    ),
    (
        "HS260831.egfp",
        "HS260831",
        None,
        None,
        "mRNA",
        "EGFP mRNA",
        None,
        None,
        "2026-08-31 egfp",
        "",
    ),
    (
        "HS260831.d9-bp",
        "HS260831",
        9,
        None,
        None,
        "",
        None,
        "culture",
        "2026-08-31 day9_bpmedium",
        "day 9 in BP medium",
    ),
    (
        "HS260831.d9-dana",
        "HS260831",
        9,
        None,
        None,
        "",
        None,
        "culture",
        "2026-08-31 day9_danamedium",
        "day 9 in Dana's medium",
    ),
    ("HS260914.d1", "HS260914", 1, None, None, "", None, None, "2026-09-14 day1", ""),
    (
        "HS260914.d2",
        "HS260914",
        2,
        None,
        "mRNA",
        "EGFP mRNA",
        None,
        "storage solution",
        "2026-09-14 day2",
        "",
    ),
    (
        "HS260914.d3",
        "HS260914",
        3,
        None,
        None,
        "",
        None,
        "culture",
        "2026-09-14 day3",
        "culture in OSCM / BPNM",
    ),
    (
        "HS260918.d1",
        "HS260918",
        1,
        "hippocampus",
        None,
        "",
        None,
        "storage solution",
        "2026-09-18 hippocampus",
        "",
    ),
]

MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        1,
    )
}
# the date spellings in the file names: 2026-05-22, 7-27-2026 or 8-31-26, 20260812, 260727, May21st_2026
PATTERNS_DATE = (
    (re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{4}|\d{2})(?!\d)"), "mdy"),
    (re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(2\d)(\d\d)(\d\d)(?!\d)"), "ymd"),
    (
        re.compile(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ _-]*(\d{1,2})(?:st|nd|rd|th)?[ _-]*(20\d\d)"
        ),
        "name",
    ),
)
ISO = re.compile(r"\d{4}-\d\d-\d\d")


def can_edit(owner: str, user: str) -> bool:
    """Users edit their own rows; the admin edits every row."""
    return user == ADMIN or user == owner


def locked(owner: str) -> str:
    """Who can edit a row owner added."""
    return (
        f"only {ADMIN} can edit it"
        if owner == ADMIN
        else f"only {owner} or {ADMIN} can edit it"
    )


def file_dates(path: str) -> set[str]:
    """Every date a path spells, in any of the spellings the lab uses, as YYYY-MM-DD."""
    found = set()
    for pattern, order in PATTERNS_DATE:
        for m in pattern.finditer(path.lower()):
            a, b, c = m.groups()
            year, month, day = (
                (a, b, c)
                if order == "ymd"
                else (c, a, b)
                if order == "mdy"
                else (c, MONTHS[a], b)
            )
            year = int(year) + 2000 if int(year) < 100 else int(year)
            try:
                found.add(date(year, int(month), int(day)).isoformat())
            except ValueError:
                continue
    return found


def match_files(files: list[dict], pattern: str) -> list[dict]:
    """Files whose path has every word of pattern, any case; a YYYY-MM-DD word matches any spelling, -word excludes."""
    words = pattern.lower().split()
    found = []
    for f in files:
        path = f["path"].lower()
        keep = bool(words)
        for word in words:
            negate = word.startswith("-") and len(word) > 1
            word = word[1:] if negate else word
            hit = word in file_dates(path) if ISO.fullmatch(word) else word in path
            if hit == negate:
                keep = False
                break
        if keep:
            found.append(f)
    return found


class SliceDatabase:
    """
    The patients, slices and assays tables, edited as a named user, and the file index under ROOT. Stored in
    slices.sqlite in the data folder by default.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or data_dir() / "slices.sqlite").absolute()
        # an older-schema file, renamed aside rather than dropped
        self.moved_aside: Optional[Path] = None
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                n_tables = db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                ).fetchone()[0]
            if version != VERSION_SCHEMA and n_tables:
                aside = self.path.with_name(f"{self.path.stem}.v{version}.sqlite")
                n = 1
                while aside.exists():
                    aside = self.path.with_name(
                        f"{self.path.stem}.v{version}.{n}.sqlite"
                    )
                    n += 1
                self.path.rename(aside)
                self.moved_aside = aside
        with closing(self.connect()) as db, db:
            db.execute(f"PRAGMA user_version = {VERSION_SCHEMA}")
            db.executescript(SCHEMA)
            if db.execute("SELECT count(*) FROM patients").fetchone()[0] == 0:
                self.seed(db)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def seed(self, db: sqlite3.Connection) -> None:
        """The patients and slices read off the file names, all the admin's."""
        for patient, collected in SEED_PATIENTS:
            db.execute(
                "INSERT INTO patients (patient_id, collection_date, owner, notes) VALUES (?, ?, ?, ?)",
                (patient, collected, ADMIN, NOTE_PATIENT),
            )
        for (
            slice_id,
            patient,
            day,
            region,
            material,
            construct,
            dye,
            incubator,
            match,
            notes,
        ) in SEED_SLICES:
            db.execute(
                "INSERT INTO slices (slice_id, patient_id, day, region, ep_material, ep_construct, ep_dye, incubator, outcome, "
                "experimenter, file_match, source, owner, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    slice_id,
                    patient,
                    day,
                    region,
                    material,
                    construct,
                    dye,
                    incubator,
                    "ok",
                    ADMIN,
                    match,
                    SOURCE,
                    ADMIN,
                    notes,
                ),
            )

    def reset(self) -> None:
        """Delete every row and reseed from the file names; the file index stays."""
        with closing(self.connect()) as db, db:
            for table in ("assays", "slices", "patients"):
                db.execute(f"DELETE FROM {table}")
            self.seed(db)

    def rows(self, table: str) -> list[dict]:
        """Every row of table in the order added; a slice carries its patient's collection date."""
        query = f"SELECT * FROM {table} ORDER BY id"
        if table == "slices":
            query = "SELECT s.*, p.collection_date FROM slices s LEFT JOIN patients p ON p.patient_id = s.patient_id ORDER BY s.id"
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(query)]

    def files(self) -> tuple[list[dict], Optional[dict]]:
        """The saved file index under ROOT, and when it was taken."""
        with closing(self.connect()) as db:
            files = [
                dict(row) for row in db.execute("SELECT * FROM files ORDER BY path")
            ]
            indexed = db.execute(
                "SELECT * FROM indexed WHERE root = ?", (ROOT,)
            ).fetchone()
        return files, None if indexed is None else dict(indexed)

    def index_files(self, storage: Storage) -> tuple[list[dict], Optional[dict]]:
        """Index every file under ROOT in one walk and save it for when storage is offline."""
        files = [
            {"path": posixpath.relpath(r.path, ROOT), "size": r.size, "mtime": r.mtime}
            for r in storage.walk(ROOT)
            if not r.is_dir
        ]
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM files")
            db.executemany("INSERT INTO files VALUES (:path, :size, :mtime)", files)
            db.execute(
                "INSERT OR REPLACE INTO indexed VALUES (?, ?, ?)",
                (ROOT, datetime.now().isoformat(timespec="seconds"), storage.label),
            )
        return self.files()

    def next_id(self, db: sqlite3.Connection, table: str, patient_id: str) -> str:
        """An unused patient id (H26.005), or slice id under patient_id (HS260727.S01)."""
        if table == "patients":
            taken = {row[0] for row in db.execute("SELECT patient_id FROM patients")}
            n = len(taken) + 1
            while f"H{date.today():%y}.{n:03d}" in taken:
                n += 1
            return f"H{date.today():%y}.{n:03d}"
        # slice ids are unique across patients, since a slice can move to another patient
        taken = {row[0] for row in db.execute("SELECT slice_id FROM slices")}
        n = (
            db.execute(
                "SELECT count(*) FROM slices WHERE patient_id = ?", (patient_id,)
            ).fetchone()[0]
            + 1
        )
        while f"{patient_id}.S{n:02d}" in taken:
            n += 1
        return f"{patient_id}.S{n:02d}"

    def check_owner(
        self, db: sqlite3.Connection, table: str, ids: list[int], user: str
    ) -> None:
        """Raise ValueError naming the first row user may not change."""
        for row in db.execute(
            f"SELECT * FROM {table} WHERE id IN ({', '.join('?' * len(ids))})", ids
        ):
            if not can_edit(row["owner"], user):
                name = row["patient_id" if table == "patients" else "slice_id"]
                raise ValueError(f"{name} is {row['owner']}'s; {locked(row['owner'])}")

    def add(self, table: str, user: str, parent: str = "") -> int:
        """
        Add a row for user with a new id and defaults; returns its id. parent is the new slice's patient or the new
        assay's slice, the newest when empty. Raises ValueError when there is none.
        """
        with closing(self.connect()) as db, db:
            if table == "patients":
                values = {
                    "patient_id": self.next_id(db, table, ""),
                    "collection_date": date.today().isoformat(),
                    "sex": "U",
                }
            elif table == "slices":
                found = db.execute(
                    "SELECT patient_id, collection_date FROM patients WHERE patient_id = ? OR ? = '' ORDER BY id DESC LIMIT 1",
                    (parent, parent),
                ).fetchone()
                if found is None:
                    raise ValueError("add a patient first")
                values = {
                    "slice_id": self.next_id(db, table, found["patient_id"]),
                    "patient_id": found["patient_id"],
                    "day": 1,
                    "ep_material": "none",
                    "ep_dye": "none",
                    "ephys": "none",
                    "incubator": "storage solution",
                    "outcome": "ok",
                    "experimenter": user,
                    "file_match": found["collection_date"] or "",
                }
            else:
                found = db.execute(
                    "SELECT slice_id FROM slices WHERE slice_id = ? OR ? = '' ORDER BY id DESC LIMIT 1",
                    (parent, parent),
                ).fetchone()
                if found is None:
                    raise ValueError("add a slice first")
                values = {
                    "slice_id": found["slice_id"],
                    "assay": "live-dead",
                    "assay_date": date.today().isoformat(),
                }
            values["owner"] = user
            names = ", ".join(values)
            return db.execute(
                f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            ).lastrowid

    def duplicate(self, table: str, ids: list[int], user: str) -> list[int]:
        """Copy rows as user's, with new ids; returns them."""
        made = []
        with closing(self.connect()) as db, db:
            for id_ in ids:
                row = db.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (id_,)
                ).fetchone()
                if row is None:
                    continue
                values = {k: row[k] for k in row.keys() if k != "id"}
                if table != "assays":
                    values["patient_id" if table == "patients" else "slice_id"] = (
                        self.next_id(db, table, values.get("patient_id", ""))
                    )
                values["owner"] = user
                names = ", ".join(values)
                made.append(
                    db.execute(
                        f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values()),
                    ).lastrowid
                )
        return made

    def delete(self, table: str, ids: list[int], user: str) -> None:
        """Delete rows; raises ValueError, deleting nothing, when a row is not user's or still has children."""
        try:
            with closing(self.connect()) as db, db:
                self.check_owner(db, table, ids, user)
                db.executemany(f"DELETE FROM {table} WHERE id = ?", [(i,) for i in ids])
        except sqlite3.IntegrityError:
            below = "slices" if table == "patients" else "assays"
            raise ValueError(
                f"these still have {below}; delete or move them first"
            ) from None

    def update(self, table: str, id_: int, column: str, text: str, user: str) -> None:
        """Set one cell from typed text, as user; raises ValueError when the text or the edit is not allowed."""
        kind, choices = next((k, c) for name, k, c in COLUMNS[table] if name == column)
        text = text.strip()
        value: object = text
        if kind == "joined":
            raise ValueError(f"{column} comes from the patient; edit it on Patients")
        if kind == "owner":
            raise ValueError("owner cannot be changed")
        if kind == "id" and not text:
            raise ValueError(f"{column} cannot be empty")
        if kind in ("int", "day"):
            number = text.lower().removeprefix("day").strip() if kind == "day" else text
            try:
                value = int(number) if number else None
            except ValueError:
                raise ValueError(
                    f"{column} must be a whole number{' or Day N' if kind == 'day' else ''}, not {text!r}"
                ) from None
        elif kind == "date" and text:
            try:
                date.fromisoformat(text)
            except ValueError:
                raise ValueError(f"{column} must be YYYY-MM-DD, not {text!r}") from None
        elif kind == "choice":
            if text and text not in choices:
                raise ValueError(f"{column} must be one of {', '.join(choices)}")
            value = text or None
        try:
            with closing(self.connect()) as db, db:
                self.check_owner(db, table, [id_], user)
                db.execute(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, id_)
                )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"no {choices[:-1]} {text}"
                if kind == "ref"
                else f"{column} {text} is already taken"
            ) from None

    def export(self, table: str, path: Path) -> None:
        """Write the table to csv, without the id column."""
        names = [name for name, _, _ in COLUMNS[table]]
        # utf-8 with a BOM so Excel reads it as utf-8
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, names, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.rows(table))
