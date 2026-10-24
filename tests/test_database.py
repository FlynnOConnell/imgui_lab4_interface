"""
Against the local development database only (python scripts/dev_db.py up); skips when it is not running.
"""

import json
import uuid
from concurrent.futures import wait
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from imgui_lab4_interface.database import Credentials, Lab4Database
from imgui_lab4_interface.lab4 import Recording

DEV = Path(__file__).resolve().parents[1] / ".dev"

# the tables as lab4.tables declares them, which the server's may follow more closely than the dev fixtures do
DATAJOINT_SHAPE = """
CREATE SCHEMA {s};
CREATE TABLE {s}."#experimenter" (experimenter varchar(128) PRIMARY KEY);
CREATE TABLE {s}."#device" (device_name varchar(64) PRIMARY KEY, device_type varchar(16) NOT NULL, description varchar(512) NOT NULL DEFAULT '');
CREATE TABLE {s}."#genotype" (genotype varchar(128) PRIMARY KEY);
CREATE TABLE {s}."#brain_location" (brain_location varchar(64) PRIMARY KEY);
CREATE TABLE {s}.expt_group (experimenter varchar(128) REFERENCES {s}."#experimenter", collection_name varchar(64), PRIMARY KEY (experimenter, collection_name));
CREATE TABLE {s}.subject (
    subject_id varchar(64) PRIMARY KEY,
    genotype varchar(128) REFERENCES {s}."#genotype",
    brain_location varchar(64) REFERENCES {s}."#brain_location",
    dob date,
    sex varchar(8) NOT NULL DEFAULT 'U'
);
CREATE TABLE {s}.session (
    experimenter varchar(128), collection_name varchar(64), subject_id varchar(64) REFERENCES {s}.subject,
    session_datetime timestamp, session_notes varchar(512) NOT NULL DEFAULT '',
    PRIMARY KEY (experimenter, collection_name, subject_id, session_datetime),
    FOREIGN KEY (experimenter, collection_name) REFERENCES {s}.expt_group
);
CREATE TABLE {s}.recording (
    experimenter varchar(128), collection_name varchar(64), subject_id varchar(64), session_datetime timestamp,
    device_name varchar(64) REFERENCES {s}."#device",
    data_path varchar(512) NOT NULL, ui_label varchar(255), weight_g decimal(6,2),
    PRIMARY KEY (experimenter, collection_name, subject_id, session_datetime, device_name),
    FOREIGN KEY (experimenter, collection_name, subject_id, session_datetime) REFERENCES {s}.session
);
INSERT INTO {s}."#experimenter" VALUES ('Eunji Kong');
INSERT INTO {s}."#device" VALUES ('Bruker 1 Imaging', 'imaging', '');
INSERT INTO {s}."#genotype" VALUES ('WT'), ('Grik4-Cre');
INSERT INTO {s}."#brain_location" VALUES ('CA1'), ('DG');
GRANT USAGE ON SCHEMA {s} TO lab4_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {s} TO lab4_user;
"""


def load(name: str) -> dict:
    path = DEV / name
    if not path.is_file():
        pytest.skip("no local dev database; run python scripts/dev_db.py up")
    values = json.loads(path.read_text())
    assert values["DB_HOST"] == "127.0.0.1", f"{path} must point at the local dev database"
    return values


def admin_connection() -> psycopg.Connection:
    c = load("admin.json")
    return psycopg.connect(host=c["DB_HOST"], port=c["DB_PORT"], user=c["DB_USER"], password=c["DB_PASSWORD"], dbname=c["DB_DATABASE"], autocommit=True)


@pytest.fixture
def lab_credentials() -> Credentials:
    load("secrets.json")
    try:
        with admin_connection():
            pass
    except psycopg.OperationalError:
        pytest.skip("local dev database is not running; run python scripts/dev_db.py up")
    return Credentials.load(DEV / "secrets.json")


@pytest.fixture
def tag(lab_credentials):
    """A unique prefix for the rows a test adds, removed afterwards with the superuser (lab users cannot delete)."""
    tag = f"test-{uuid.uuid4().hex[:8]}"
    yield tag
    with admin_connection() as conn:
        for table in ("recording", "session", "subject"):
            conn.execute(f"DELETE FROM lab4.{table} WHERE subject_id LIKE %s", (f"{tag}%",))
        conn.execute("DELETE FROM lab4.expt_group WHERE collection_name LIKE %s", (f"{tag}%",))


def recording(tag: str, **values) -> Recording:
    fields = {
        "experimenter": "Eunji Kong",
        "collection_name": f"{tag}-group",
        "subject_id": f"{tag}-mouse",
        "session_datetime": "2026-10-09 14:30",
        "device_name": "Bruker 1 Imaging",
        "weight_g": "24.5",
        "notes": "first session",
    }
    return Recording(**{**fields, **values})


def test_schema_reads_the_tables_as_the_lab_user(lab_credentials):
    schema = Lab4Database(lab_credentials).schema()
    assert schema.user == "lab4_u_1"
    assert {"fixture-user", "Eunji Kong"} <= set(schema.choices["experimenter"])
    assert "synthetic" in schema.choices["device_name"]
    assert ("fixture-user", "development") in schema.groups
    assert "fixture-mouse" in schema.subjects
    assert [(c.name, c.required) for c in schema.subject_fields()] == [("species", True)]
    assert {c.name for c in schema.tables["recording"] if c.key} == {"experimenter", "collection_name", "subject_id", "session_datetime", "device_name"}


def test_register_adds_the_chain_once(lab_credentials, tag):
    db = Lab4Database(lab_credentials)
    schema = db.schema()
    rec = recording(tag)
    assert schema.problems(rec) == ["new subject needs species"]
    rec.subject_fields = {"species": "Mus musculus"}
    assert schema.problems(rec) == []

    added = db.register(schema, rec, "/biohpc/data2/M231/rec.h5")
    assert added == {"expt_group": True, "subject": True, "session": True, "recording": True}
    rows = db.recordings(rec.subject_id)
    assert len(rows) == 1
    assert rows[0]["data_path"] == "/biohpc/data2/M231/rec.h5"
    assert rows[0]["session_datetime"] == datetime(2026, 10, 9, 14, 30)
    assert str(rows[0]["weight_g"]) == "24.50"

    with pytest.raises(ValueError, match="already registered"):
        db.register(schema, rec, "/biohpc/data2/M231/rec.h5")
    second = recording(tag, device_name="synthetic")
    assert db.register(db.schema(), second, "/biohpc/data2/M231/behavior.h5") == {"expt_group": False, "subject": False, "session": False, "recording": True}
    assert len(db.recordings(rec.subject_id)) == 2


def test_register_refuses_paths_outside_biohpc_and_unknown_lookups(lab_credentials, tag):
    db = Lab4Database(lab_credentials)
    schema = db.schema()
    rec = recording(tag, subject_fields={"species": "Mus musculus"})
    with pytest.raises(ValueError, match="not under /biohpc"):
        db.register(schema, rec, "C:/data/rec.h5")
    assert db.recordings(rec.subject_id) == []
    assert schema.problems(recording(tag, device_name="Imaginary Rig", subject_id="fixture-mouse")) == ["device name 'Imaginary Rig' is not in Lab4"]


def test_datajoint_shaped_tables(lab_credentials, tag):
    name = f"dj_{tag.replace('-', '_')}"
    with admin_connection() as conn:
        conn.execute(DATAJOINT_SHAPE.format(s=name))
    try:
        db = Lab4Database(Credentials(**{**lab_credentials.__dict__, "schema": name}))
        schema = db.schema()
        assert schema.choices["genotype"] == ["Grik4-Cre", "WT"]
        assert schema.choices["brain_location"] == ["CA1", "DG"]
        assert [(c.name, c.required) for c in schema.subject_fields()] == [("genotype", False), ("brain_location", False), ("dob", False), ("sex", False)]
        rec = recording(tag, subject_fields={"genotype": "Rat", "dob": ""})
        assert schema.problems(rec) == ["genotype 'Rat' is not in Lab4"]
        rec.subject_fields = {"genotype": "WT", "brain_location": "CA1", "dob": "2026-01-02", "sex": ""}
        db.register(schema, rec, "/biohpc/data3/rec.h5")
        with db.connect() as conn:
            assert conn.execute(f"SELECT genotype, dob::text, sex FROM {name}.subject").fetchall() == [("WT", "2026-01-02", "U")]
    finally:
        with admin_connection() as conn:
            conn.execute(f"DROP SCHEMA {name} CASCADE")


def settle(app) -> None:
    """Wait for the app's workers and land their outcomes, as frames would."""
    while app.futures:
        wait(list(app.futures.values()))
        app.poll()


def test_window_signs_in_and_registers(lab_credentials, tag, tmp_path, monkeypatch):
    from imgui_lab4_interface import Settings
    from imgui_lab4_interface.app import Lab4App

    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAB4_SECRETS_PATH", str(DEV / "secrets.json"))
    (tmp_path / "mount" / "data2").mkdir(parents=True)
    app = Lab4App(Settings(mount_root=str(tmp_path / "mount"), mount_base="/biohpc"))
    app.sign_in()
    settle(app)
    assert (app.user, app.error_login, app.status) == ("lab4_u_1", "", "")

    app.recording = recording(tag, subject_fields={"species": "Mus musculus"})
    path = "/biohpc/data2/rec.h5"
    assert app.register_problems("/biohpc/data2/rec.tif") == ["only .h5 files (from lab4.convert) can be registered"]
    assert app.register_problems(path) == []
    app.register(path)
    settle(app)
    assert not app.failed, app.message
    assert "with a new expt group, subject, session" in app.message
    assert app.recording.subject_id in app.schema.subjects
    assert [r["data_path"] for r in app.subject_recordings] == [path]

    app.register(path)
    settle(app)
    assert app.failed and "already registered" in app.message
    app.executor.shutdown(wait=True)
