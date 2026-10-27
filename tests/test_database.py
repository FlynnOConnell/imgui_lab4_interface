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
CREATE TABLE {s}."#indicator" (indicator varchar(256) PRIMARY KEY);
CREATE TABLE {s}.injection (
    subject_id varchar(64) REFERENCES {s}.subject, indicator varchar(256) REFERENCES {s}."#indicator",
    injection_date date, injection_notes varchar(512) NOT NULL DEFAULT '',
    PRIMARY KEY (subject_id, indicator)
);
INSERT INTO {s}."#experimenter" VALUES ('Eunji Kong');
INSERT INTO {s}."#device" VALUES ('Bruker 1 Imaging', 'imaging', '');
INSERT INTO {s}."#genotype" VALUES ('WT'), ('Grik4-Cre');
INSERT INTO {s}."#brain_location" VALUES ('CA1'), ('DG');
INSERT INTO {s}."#indicator" VALUES ('AAV9-syn-jGCaMP8s');
GRANT USAGE ON SCHEMA {s} TO lab4_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {s} TO lab4_user;
"""


def load(name: str) -> dict:
    path = DEV / name
    if not path.is_file():
        pytest.skip("no local dev database; run python scripts/dev_db.py up")
    values = json.loads(path.read_text())
    assert values["DB_HOST"] == "127.0.0.1", (
        f"{path} must point at the local dev database"
    )
    return values


def admin_connection() -> psycopg.Connection:
    c = load("admin.json")
    return psycopg.connect(
        host=c["DB_HOST"],
        port=c["DB_PORT"],
        user=c["DB_USER"],
        password=c["DB_PASSWORD"],
        dbname=c["DB_DATABASE"],
        autocommit=True,
    )


@pytest.fixture
def lab_credentials() -> Credentials:
    load("secrets.json")
    try:
        with admin_connection():
            pass
    except psycopg.OperationalError:
        pytest.skip(
            "local dev database is not running; run python scripts/dev_db.py up"
        )
    return Credentials.load(DEV / "secrets.json")


@pytest.fixture
def tag(lab_credentials):
    """A unique prefix for the rows a test adds, removed afterwards with the superuser (lab users cannot delete)."""
    tag = f"test-{uuid.uuid4().hex[:8]}"
    yield tag
    with admin_connection() as conn:
        for table in ("recording", "session", "subject"):
            conn.execute(
                f"DELETE FROM lab4.{table} WHERE subject_id LIKE %s", (f"{tag}%",)
            )
        conn.execute(
            "DELETE FROM lab4.expt_group WHERE collection_name LIKE %s", (f"{tag}%",)
        )


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
    assert [(c.name, c.required) for c in schema.subject_fields()] == [
        ("species", True)
    ]
    assert {c.name for c in schema.tables["recording"] if c.key} == {
        "experimenter",
        "collection_name",
        "subject_id",
        "session_datetime",
        "device_name",
    }


def test_register_adds_the_chain_once(lab_credentials, tag):
    db = Lab4Database(lab_credentials)
    schema = db.schema()
    rec = recording(tag)
    assert schema.problems(rec) == ["new subject needs species"]
    rec.subject_fields = {"species": "Mus musculus"}
    assert schema.problems(rec) == []

    added = db.register(schema, rec, "/biohpc/data2/M231/rec.h5")
    assert added == {
        "expt_group": True,
        "subject": True,
        "session": True,
        "recording": True,
    }
    rows = db.recordings(rec.subject_id)
    assert len(rows) == 1
    assert rows[0]["data_path"] == "/biohpc/data2/M231/rec.h5"
    assert rows[0]["session_datetime"] == datetime(2026, 10, 9, 14, 30)
    assert str(rows[0]["weight_g"]) == "24.50"

    with pytest.raises(ValueError, match="already registered"):
        db.register(schema, rec, "/biohpc/data2/M231/rec.h5")
    second = recording(tag, device_name="synthetic")
    assert db.register(db.schema(), second, "/biohpc/data2/M231/behavior.h5") == {
        "expt_group": False,
        "subject": False,
        "session": False,
        "recording": True,
    }
    assert len(db.recordings(rec.subject_id)) == 2


def test_register_refuses_paths_outside_biohpc_and_unknown_lookups(
    lab_credentials, tag
):
    db = Lab4Database(lab_credentials)
    schema = db.schema()
    rec = recording(tag, subject_fields={"species": "Mus musculus"})
    with pytest.raises(ValueError, match="not under /biohpc"):
        db.register(schema, rec, "C:/data/rec.h5")
    assert db.recordings(rec.subject_id) == []
    assert schema.problems(
        recording(tag, device_name="Imaginary Rig", subject_id="fixture-mouse")
    ) == ["device name 'Imaginary Rig' is not in Lab4"]


def test_update_changes_details_in_one_transaction_and_never_a_key(
    lab_credentials, tag
):
    db = Lab4Database(lab_credentials)
    schema = db.schema()
    rec = recording(tag, subject_fields={"species": "Mus musculus"})
    db.register(schema, rec, "/biohpc/data2/M231/rec.h5")
    contents = db.contents(schema)
    assert {"expt_group", "subject", "session", "recording"} == set(contents)
    row = next(r for r in contents["recording"] if r["subject_id"] == rec.subject_id)
    key = schema.key("recording", row)
    assert key == {**rec.key(), "session_datetime": datetime(2026, 10, 9, 14, 30)}

    db.update(
        schema,
        [
            (
                "recording",
                key,
                {
                    "ui_label": " day 1 ",
                    "weight_g": "",
                    "data_path": "/biohpc/data3/M231/rec.h5",
                },
            ),
            (
                "session",
                schema.key("session", row),
                {"session_notes": "moved to data3"},
            ),
            ("subject", schema.key("subject", row), {"species": "Rattus norvegicus"}),
        ],
    )
    after = db.contents(schema)
    row = next(r for r in after["recording"] if r["subject_id"] == rec.subject_id)
    assert (row["ui_label"], row["weight_g"], row["data_path"]) == (
        "day 1",
        None,
        "/biohpc/data3/M231/rec.h5",
    )
    assert [
        s["session_notes"]
        for s in after["session"]
        if s["subject_id"] == rec.subject_id
    ] == ["moved to data3"]
    assert [
        s["species"] for s in after["subject"] if s["subject_id"] == rec.subject_id
    ] == ["Rattus norvegicus"]

    with pytest.raises(
        ValueError, match="only data_path, weight_g, ui_label can change"
    ):
        db.update(schema, [("recording", key, {"device_name": "synthetic"})])
    with pytest.raises(ValueError, match="is not the key"):
        db.update(
            schema, [("recording", {"subject_id": rec.subject_id}, {"ui_label": "x"})]
        )
    with pytest.raises(ValueError, match="not under /biohpc"):
        db.update(schema, [("recording", key, {"data_path": "C:/data/rec.h5"})])
    # a row that is not there undoes the edits before it
    missing = {**schema.key("session", row), "session_datetime": datetime(1999, 1, 1)}
    with pytest.raises(ValueError, match="no session"):
        db.update(
            schema,
            [
                ("session", schema.key("session", row), {"session_notes": "lost"}),
                ("session", missing, {"session_notes": "x"}),
            ],
        )
    assert [
        s["session_notes"]
        for s in db.contents(schema)["session"]
        if s["subject_id"] == rec.subject_id
    ] == ["moved to data3"]

    assert schema.edit_problems("subject", {"species": " "}) == ["species is needed"]
    assert schema.edit_problems(
        "recording", {"data_path": "/home/fo/rec.h5", "ui_label": ""}
    ) == ["data path must be under /biohpc"]


def test_datajoint_shaped_tables(lab_credentials, tag):
    name = f"dj_{tag.replace('-', '_')}"
    with admin_connection() as conn:
        conn.execute(DATAJOINT_SHAPE.format(s=name))
    try:
        db = Lab4Database(Credentials(**{**lab_credentials.__dict__, "schema": name}))
        schema = db.schema()
        assert schema.choices["genotype"] == ["Grik4-Cre", "WT"]
        assert schema.choices["brain_location"] == ["CA1", "DG"]
        assert [(c.name, c.required) for c in schema.subject_fields()] == [
            ("genotype", False),
            ("brain_location", False),
            ("dob", False),
            ("sex", False),
        ]
        rec = recording(tag, subject_fields={"genotype": "Rat", "dob": ""})
        assert schema.problems(rec) == ["genotype 'Rat' is not in Lab4"]
        rec.subject_fields = {
            "genotype": "WT",
            "brain_location": "CA1",
            "dob": "2026-01-02",
            "sex": "",
        }
        db.register(schema, rec, "/biohpc/data3/rec.h5")
        with db.connect() as conn:
            assert conn.execute(
                f"SELECT genotype, dob::text, sex FROM {name}.subject"
            ).fetchall() == [("WT", "2026-01-02", "U")]

        assert schema.choices["indicator"] == ["AAV9-syn-jGCaMP8s"]
        injection = {
            "subject_id": rec.subject_id,
            "indicator": "AAV9-syn-jGCaMP8s",
            "injection_date": "2026-01-20",
            "injection_notes": "",
        }
        db.add(schema, "injection", injection)
        with pytest.raises(ValueError, match="already in Lab4"):
            db.add(schema, "injection", injection)
        assert [
            (r["indicator"], str(r["injection_date"]), r["injection_notes"])
            for r in db.contents(schema)["injection"]
        ] == [("AAV9-syn-jGCaMP8s", "2026-01-20", "")]
        subject = db.contents(schema)["subject"][0]
        db.update(
            schema,
            [
                (
                    "subject",
                    schema.key("subject", subject),
                    {"genotype": "Grik4-Cre", "dob": ""},
                )
            ],
        )
        assert [(r["genotype"], r["dob"]) for r in db.contents(schema)["subject"]] == [
            ("Grik4-Cre", None)
        ]
        assert schema.edit_problems("subject", {"genotype": "Rat"}) == [
            "genotype 'Rat' is not in Lab4"
        ]
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
    assert app.register_problems("/biohpc/data2/rec.tif") == [
        "only .h5 files (from lab4.convert) can be registered"
    ]
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
    app.executor_long.shutdown(wait=True)


def test_window_edits_a_recording_and_rewrites_its_sidecar(
    lab_credentials, tag, tmp_path, monkeypatch
):
    from imgui_lab4_interface import Settings
    from imgui_lab4_interface.app import Lab4App

    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAB4_SECRETS_PATH", str(DEV / "secrets.json"))
    folder = tmp_path / "mount" / "data2" / tag
    folder.mkdir(parents=True)
    (folder / "rec.h5").write_bytes(b"h5")
    path = f"/biohpc/data2/{tag}/rec.h5"
    app = Lab4App(
        Settings(
            mount_root=str(tmp_path / "mount"),
            mount_base="/biohpc",
            destinations=[[f"/biohpc/data2/{tag}", "test"]],
        )
    )
    app.sign_in()
    settle(app)
    app.recording = recording(
        tag, subject_fields={"species": "Mus musculus"}, extra=[["rig", "B"]]
    )
    app.register(path)
    settle(app)
    assert path in app.registered and not app.failed, app.message

    # attach a sidecar to the registered file, then load it back into an empty form
    app.page, app.selected = "browse", "rec.h5"
    app.act()
    app.confirm["run"](*app.confirm["args"])
    app.confirm = None
    settle(app)
    app.recording = Recording()
    app.select(name="rec.h5")
    settle(app)
    app.load_sidecar()
    assert (app.recording.subject_id, app.recording.extra) == (
        f"{tag}-mouse",
        [["rig", "B"]],
    )

    app.show_in_lab4(path)
    settle(app)
    assert (
        app.page == "recordings"
        and app.sidecar_lab4["key"]["subject_id"] == f"{tag}-mouse"
    )
    assert app.problems() == ["nothing changed"]
    app.edits_lab4["recording"]["ui_label"] = "day 1"
    app.edits_lab4["session"]["session_notes"] = "edited"
    app.act()
    lines = " ".join(app.confirm["lines"])
    assert (
        app.confirm["button"] == "Save"
        and "'' -> 'day 1'" in lines
        and f"rewrite {path}.lab4.json" in lines
    )
    sidecar_before = (folder / "rec.h5.lab4.json").read_text()
    assert (
        app.db.recordings(f"{tag}-mouse")[0]["ui_label"] is None
        and "lab4_save" not in app.futures
    )

    app.confirm["run"](*app.confirm["args"])
    app.confirm = None
    settle(app)
    assert app.message == "saved to Lab4 and rewrote the sidecar", app.message
    assert app.db.recordings(f"{tag}-mouse")[0]["ui_label"] == "day 1"
    sidecar = json.loads((folder / "rec.h5.lab4.json").read_text())
    assert sidecar != json.loads(sidecar_before)
    assert (
        sidecar["ui_label"],
        sidecar["notes"],
        sidecar["extra"],
        sidecar["source"],
    ) == ("day 1", "edited", {"rig": "B"}, path)
    assert app.changes_lab4() == []

    # a reload takes what someone else changed, and keeps what is still being edited
    with admin_connection() as conn:
        conn.execute(
            "UPDATE lab4.recording SET ui_label = 'by someone else' WHERE subject_id = %s",
            (f"{tag}-mouse",),
        )
    app.edits_lab4["session"]["session_notes"] = "mine"
    app.load_lab4()
    settle(app)
    assert app.edits_lab4["recording"]["ui_label"] == "by someone else"
    assert [(table, values) for table, _, values in app.changes_lab4()] == [
        ("session", {"session_notes": "mine"})
    ]
    app.executor.shutdown(wait=True)
    app.executor_long.shutdown(wait=True)
