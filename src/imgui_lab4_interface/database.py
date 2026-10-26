"""
The Lab4 database: credentials, the current table layout, registering recordings, and editing their details.

Plain SQL, with columns and keys read from the database, so a new server column shows up without a release. Each
call opens its own connection, so any worker thread can use it. Key columns are never changed: other tables point at
them, and lab members cannot delete a row to add it again.
"""

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from imgui_lab4_interface.lab4 import Recording

SECRETS_ENV = "LAB4_SECRETS_PATH"
TIMEOUT_CONNECT = 10
BIOHPC = "/biohpc"
# the tables the window reads and writes, parents first; a registration writes the first four
TABLES = ("expt_group", "subject", "session", "recording", "injection")

QUERY_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable = 'YES', column_default IS NOT NULL
FROM information_schema.columns
WHERE table_schema = %s AND table_name = ANY(%s)
ORDER BY table_name, ordinal_position
"""
# pg_constraint, since information_schema hides keys on tables the user does not own
QUERY_KEYS = """
SELECT c.contype, t.relname, a.attname, rt.relname
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
JOIN pg_class t ON t.oid = c.conrelid
CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, i)
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
LEFT JOIN pg_class rt ON rt.oid = c.confrelid
WHERE n.nspname = %s AND c.contype IN ('p', 'f') AND t.relname = ANY(%s)
"""


def secrets_path() -> Path:
    """``LAB4_SECRETS_PATH`` when set, else ``~/.lab4/secrets.json``, as the lab4 package reads it."""
    base = os.environ.get(SECRETS_ENV)
    return Path(base).expanduser() if base else Path.home() / ".lab4" / "secrets.json"


@dataclasses.dataclass
class Credentials:
    """
    A lab member's database login, from the Lab4 website's credentials file.

    Attributes:
        database (str): The postgres database; "postgres" when the file leaves it out
        schema (str): The Lab4 schema, the file's DB_NAME
    """

    host: str
    port: int
    user: str
    password: str
    database: str = "postgres"
    schema: str = "lab4"

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        values = json.loads(path.read_text())
        return cls(
            host=values["DB_HOST"],
            port=int(values["DB_PORT"]),
            user=values["DB_USER"],
            password=values["DB_PASSWORD"],
            database=values.get("DB_DATABASE", "postgres"),
            schema=values.get("DB_NAME", "lab4"),
        )

    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@dataclasses.dataclass
class Column:
    name: str
    type: str
    nullable: bool
    has_default: bool
    key: bool

    @property
    def required(self) -> bool:
        return not self.nullable and not self.has_default


@dataclasses.dataclass
class Schema:
    """
    The Lab4 tables as they are now, and the choices the metadata form offers.

    Attributes:
        tables (dict): table -> columns, for those of TABLES the database has
        choices (dict): column -> allowed values from its lookup table
        groups (list): (experimenter, collection_name) of every experiment group
        subjects (list): Every subject_id
    """

    user: str
    tables: dict
    choices: dict
    groups: list
    subjects: list

    def columns(self, table: str) -> dict[str, Column]:
        return {c.name: c for c in self.tables.get(table, [])}

    def key(self, table: str, row: dict) -> dict:
        """The table's primary key, taken from row; row may be a child's, which holds its parents' keys."""
        return {c.name: row[c.name] for c in self.tables[table] if c.key}

    def details(self, table: str) -> list[Column]:
        """The table's columns other than its key, the ones an edit may change."""
        return [c for c in self.tables.get(table, []) if not c.key]

    def edit_problems(self, table: str, values: dict) -> list[str]:
        """What the database would reject in values, column -> text, for the table's columns."""
        found = []
        columns = self.columns(table)
        for name, text in values.items():
            value, caption = text.strip(), name.replace("_", " ")
            if not value and columns[name].required:
                found.append(f"{caption} is needed")
            elif value and name in self.choices and value not in self.choices[name]:
                found.append(f"{caption} {value!r} is not in Lab4")
            elif name == "data_path" and not value.startswith(f"{BIOHPC}/"):
                found.append(f"data path must be under {BIOHPC}")
        return found

    def subject_fields(self) -> list[Column]:
        """Subject columns other than the key."""
        return self.details("subject")

    def groups_of(self, experimenter: str) -> list[str]:
        return sorted(name for who, name in self.groups if who == experimenter)

    def problems(self, rec: Recording) -> list[str]:
        """What the database would reject in rec."""
        found = []
        for column, value in (("experimenter", rec.experimenter), ("device_name", rec.device_name)):
            if column in self.choices and value.strip() and value.strip() not in self.choices[column]:
                found.append(f"{column.replace('_', ' ')} {value.strip()!r} is not in Lab4")
        if rec.subject_id.strip() and rec.subject_id.strip() not in self.subjects:
            for column in self.subject_fields():
                value = rec.subject_fields.get(column.name, "").strip()
                if column.required and not value:
                    found.append(f"new subject needs {column.name.replace('_', ' ')}")
                elif value and column.name in self.choices and value not in self.choices[column.name]:
                    found.append(f"{column.name.replace('_', ' ')} {value!r} is not in Lab4")
        return found


def canonical(path: str) -> str:
    """path, when under /biohpc; raises ValueError otherwise."""
    if not path.startswith(f"{BIOHPC}/"):
        raise ValueError(f"{path} is not under {BIOHPC}")
    return path


def stored(column: Column, text: str) -> Optional[str]:
    """text as column stores it: stripped, and NULL when empty and the column allows it."""
    text = text.strip()
    return None if not text and column.nullable else text


class Lab4Database:
    """Lab4 access with one lab member's credentials."""

    def __init__(self, credentials: Credentials):
        self.credentials = credentials

    def connect(self) -> psycopg.Connection:
        c = self.credentials
        return psycopg.connect(
            host=c.host,
            port=c.port,
            user=c.user,
            password=c.password,
            dbname=c.database,
            connect_timeout=TIMEOUT_CONNECT,
            application_name="imgui_lab4_interface",
        )

    def table(self, name: str) -> sql.Identifier:
        return sql.Identifier(self.credentials.schema, name)

    def schema(self) -> Schema:
        """Sign in and read the tables, lookups, groups and subjects."""
        name = self.credentials.schema
        with self.connect() as conn:
            user = conn.execute("SELECT current_user").fetchone()[0]
            keys, lookups = set(), {}
            for kind, table, column, referenced in conn.execute(QUERY_KEYS, (name, list(TABLES))):
                if kind == "p":
                    keys.add((table, column))
                elif referenced.startswith("#"):
                    lookups[column] = referenced
            tables = {}
            for table, column, kind, nullable, has_default in conn.execute(QUERY_COLUMNS, (name, list(TABLES))):
                tables.setdefault(table, []).append(Column(column, kind, nullable, has_default, (table, column) in keys))
            choices = {}
            for column, lookup in lookups.items():
                query = sql.SQL("SELECT DISTINCT {} FROM {} ORDER BY 1").format(sql.Identifier(column), self.table(lookup))
                choices[column] = [row[0] for row in conn.execute(query)]
            groups = conn.execute(sql.SQL("SELECT experimenter, collection_name FROM {}").format(self.table("expt_group"))).fetchall()
            subjects = [row[0] for row in conn.execute(sql.SQL("SELECT subject_id FROM {} ORDER BY 1").format(self.table("subject")))]
        return Schema(user=user, tables=tables, choices=choices, groups=[tuple(g) for g in groups], subjects=subjects)

    def insert(self, conn: psycopg.Connection, table: str, values: dict, columns: dict, ignore_existing: bool) -> bool:
        """Insert the values table has columns for; returns whether a row was added."""
        values = {k: v for k, v in values.items() if k in columns}
        if ignore_existing:
            # NOT NULL fires before ON CONFLICT, so look for the key first
            key = {k: v for k, v in values.items() if columns[k].key}
            where = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key)
            if conn.execute(sql.SQL("SELECT 1 FROM {} WHERE {}").format(self.table(table), where), list(key.values())).fetchone():
                return False
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}){}").format(
            self.table(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() * len(values)),
            sql.SQL(" ON CONFLICT DO NOTHING") if ignore_existing else sql.SQL(""),
        )
        return conn.execute(query, list(values.values())).rowcount == 1

    def register(self, schema: Schema, rec: Recording, data_path: str) -> dict:
        """
        Add rec's Recording row for data_path, plus its group, subject and session when new, in one transaction.
        Returns which rows were added; raises ValueError when already registered.
        """
        key = rec.key()
        session_key = {k: key[k] for k in ("experimenter", "collection_name", "subject_id", "session_datetime")}
        subject = {"subject_id": key["subject_id"], **{k: v.strip() for k, v in rec.subject_fields.items() if v.strip()}}
        recording = {**key, "data_path": canonical(data_path), "ui_label": rec.ui_label.strip() or None, "weight_g": rec.weight_g.strip() or None}
        with self.connect() as conn, conn.transaction():
            added = {
                "expt_group": self.insert(conn, "expt_group", {"experimenter": key["experimenter"], "collection_name": key["collection_name"]}, schema.columns("expt_group"), True),
                "subject": self.insert(conn, "subject", subject, schema.columns("subject"), True),
                "session": self.insert(conn, "session", {**session_key, "session_notes": rec.notes.strip()}, schema.columns("session"), True),
            }
            try:
                with conn.transaction():
                    added["recording"] = self.insert(conn, "recording", recording, schema.columns("recording"), False)
            except psycopg.errors.UniqueViolation as e:
                raise ValueError(f"{rec.label()} is already registered in Lab4") from e
        return added

    def recordings(self, subject_id: str) -> list[dict]:
        """The subject's recordings, newest session first."""
        query = sql.SQL("SELECT * FROM {} WHERE subject_id = %s ORDER BY session_datetime DESC").format(self.table("recording"))
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, (subject_id,)).fetchall()

    def contents(self, schema: Schema) -> dict[str, list[dict]]:
        """Every row of the tables in schema, by table; recordings newest session first."""
        found = {}
        with self.connect() as conn:
            # NOTE: one snapshot for every table, so no recording is read without the session it points at
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with conn.cursor(row_factory=dict_row) as cur:
                for table in TABLES:
                    if table in schema.tables:
                        order = sql.SQL(" ORDER BY session_datetime DESC, subject_id, device_name") if table == "recording" else sql.SQL("")
                        found[table] = cur.execute(sql.SQL("SELECT * FROM {}{}").format(self.table(table), order)).fetchall()
        return found

    def update(self, schema: Schema, edits: list[tuple[str, dict, dict]]) -> None:
        """
        Change existing rows, all in one transaction. Each edit is (table, primary key, column -> text), and only
        columns outside the key change; empty text is NULL where the column allows it. Raises ValueError for a key
        column or a row that is not there.
        """
        with self.connect() as conn, conn.transaction():
            for table, key, values in edits:
                columns = schema.columns(table)
                if set(key) != {c.name for c in columns.values() if c.key}:
                    raise ValueError(f"{sorted(key)} is not the key of {table}")
                if any(name not in columns or columns[name].key for name in values):
                    raise ValueError(f"only {', '.join(c.name for c in schema.details(table))} can change in {table}")
                values = {name: stored(columns[name], text) for name, text in values.items()}
                if "data_path" in values:
                    values["data_path"] = canonical(values["data_path"])
                query = sql.SQL("UPDATE {} SET {} WHERE {}").format(
                    self.table(table),
                    sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values),
                    sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key),
                )
                if conn.execute(query, [*values.values(), *key.values()]).rowcount != 1:
                    raise ValueError(f"no {table.replace('_', ' ')} {key} in Lab4")

    def add(self, schema: Schema, table: str, values: dict) -> None:
        """Insert one row from column -> text, leaving empty text to the column's default. Raises ValueError when the key is taken."""
        row = {k: v.strip() for k, v in values.items() if v.strip()}
        with self.connect() as conn:
            try:
                with conn.transaction():
                    self.insert(conn, table, row, schema.columns(table), False)
            except psycopg.errors.UniqueViolation as e:
                raise ValueError(f"that {table.replace('_', ' ')} is already in Lab4") from e
