"""
The Lab4 database: the credentials file the Lab4 website hands out, what the tables look like right now, and
registering a recording.

It talks plain SQL and reads the tables' columns and keys from the database instead of assuming them, as the
website does, so a column added on the server shows up here without a release. Every call opens its own
connection, so calls are safe from any worker thread.
"""

import dataclasses
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from imgui_lab4_interface.lab4 import Recording

SECRETS_ENV = "LAB4_SECRETS_PATH"
TIMEOUT_CONNECT = 10
BIOHPC = "/biohpc"
# the tables a registration writes, parents first
TABLES = ("expt_group", "subject", "session", "recording")

QUERY_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable = 'YES', column_default IS NOT NULL
FROM information_schema.columns
WHERE table_schema = %s AND table_name = ANY(%s)
ORDER BY table_name, ordinal_position
"""
# pg_constraint rather than information_schema: the latter hides keys on tables the user does not own
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
    One lab member's database login, from the file the Lab4 website hands out.

    Attributes:
        database (str): The postgres database; the website's files leave it out, meaning "postgres"
        schema (str): The schema the Lab4 tables live in, the file's DB_NAME
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
    The Lab4 tables as the database has them now, and the values the Metadata page offers.

    Attributes:
        tables (dict): table name -> its columns, for the tables a registration writes
        choices (dict): column name -> the values its lookup table holds; a column with choices takes only those
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

    def subject_fields(self) -> list[Column]:
        """What a new subject is described by besides its id."""
        return [c for c in self.tables.get("subject", []) if not c.key]

    def groups_of(self, experimenter: str) -> list[str]:
        return sorted(name for who, name in self.groups if who == experimenter)

    def problems(self, rec: Recording) -> list[str]:
        """What the database would refuse in rec, beyond rec's own problems."""
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
    """The path Lab4 stores for a BioHPC path; raises ValueError for one outside /biohpc."""
    if not path.startswith(f"{BIOHPC}/"):
        raise ValueError(f"{path} is not under {BIOHPC}")
    return path


class Lab4Database:
    """The Lab4 tables reached with one lab member's credentials."""

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
        """Sign in and read the tables, their lookups, and the groups and subjects already there."""
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
        """Insert the values table has a column for; whether a row was added."""
        values = {k: v for k, v in values.items() if k in columns}
        if ignore_existing:
            # NOT NULL is checked before ON CONFLICT, so an existing row given only its key would still fail
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
        Add rec's Recording row pointing at data_path, and its group, subject and session when they are new, in one
        transaction. Returns which rows were added; raises ValueError when the recording is already registered.
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

