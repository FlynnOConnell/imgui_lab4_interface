"""
Draw the window for a few frames per page on hello_imgui's null backend: no window or GPU. Skips when the
backend cannot start here.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import imgui_data_loader as idl
import pytest
from imgui_bundle import hello_imgui, imgui, immapp
from test_transfer_page import settle

from imgui_lab4_interface import Settings
from imgui_lab4_interface.app import PAGES, Lab4App
from imgui_lab4_interface.database import Column, Schema

FRAMES_PER_PAGE = 3


def enable_textures() -> None:
    # the null renderer uploads no font atlas; imgui 1.92 asserts in NewFrame unless the backend claims textures
    imgui.get_io().backend_flags |= imgui.BackendFlags_.renderer_has_textures


class Driver:
    """Draws the signed-out card, then signs in and visits every page."""

    def __init__(self, app: Lab4App):
        self.app = app
        self.frame = 0
        self.pages_seen = []

    def __call__(self) -> None:
        self.frame += 1
        page = self.frame // FRAMES_PER_PAGE - 1
        if page == 0 and not self.app.user:
            self.app.work_offline()
        if 0 <= page < len(PAGES):
            self.app.page = PAGES[page][0]
            if self.app.page not in self.pages_seen:
                self.pages_seen.append(self.app.page)
        self.app.dialog.render()
        if page >= len(PAGES):
            hello_imgui.get_runner_params().app_shall_exit = True


def test_every_page_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    (tmp_path / "mount" / "archive" / "x").mkdir(parents=True)
    (tmp_path / "data.tif").write_bytes(b"tif")
    idl.ensure_assets()
    app = Lab4App(
        Settings(
            mount_root=str(tmp_path / "mount"), destinations=[["/archive/x", "test"]]
        )
    )
    app.sources = [str(tmp_path / "data.tif")]
    app.recording.subject_id = "M231"
    driver = Driver(app)

    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.app_window_params.window_geometry.size = (1040, 720)
    params.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    params.ini_filename = "imgui_lab4_interface_test.ini"
    params.callbacks.post_init = enable_textures
    params.callbacks.show_gui = driver
    try:
        immapp.run(params)
    except Exception as e:
        pytest.skip(f"null backend unavailable: {e}")
    app.executor.shutdown(wait=True)
    app.executor_long.shutdown(wait=True)

    assert app.user == "offline"
    assert driver.pages_seen == [key for key, *_ in PAGES]
    assert app.status == ""


def lab4_schema() -> Schema:
    """The Lab4 tables as lab4.tables declares them, without a database."""
    keys = {
        "experimenter",
        "collection_name",
        "subject_id",
        "session_datetime",
        "device_name",
    }
    shapes = {
        "expt_group": [("experimenter", "varchar"), ("collection_name", "varchar")],
        "subject": [
            ("subject_id", "varchar"),
            ("genotype", "varchar"),
            ("dob", "date"),
            ("sex", "varchar"),
        ],
        "session": [
            ("experimenter", "varchar"),
            ("collection_name", "varchar"),
            ("subject_id", "varchar"),
            ("session_datetime", "timestamp"),
            ("session_notes", "varchar"),
        ],
        "recording": [
            *[(k, "varchar") for k in sorted(keys)],
            ("data_path", "varchar"),
            ("ui_label", "varchar"),
            ("weight_g", "numeric"),
        ],
        "injection": [
            ("subject_id", "varchar"),
            ("indicator", "varchar"),
            ("injection_date", "date"),
            ("injection_notes", "varchar"),
        ],
    }
    key_of = {
        "expt_group": {"experimenter", "collection_name"},
        "subject": {"subject_id"},
        "session": keys - {"device_name"},
        "recording": keys,
        "injection": {"subject_id", "indicator"},
    }
    tables = {
        table: [
            Column(name, kind, name not in key_of[table], False, name in key_of[table])
            for name, kind in columns
        ]
        for table, columns in shapes.items()
    }
    return Schema(
        user="loson",
        tables=tables,
        choices={"genotype": ["WT"], "indicator": ["AAV9-syn-jGCaMP8s"]},
        groups=[("Eunji Kong", "place cells")],
        subjects=["M231"],
    )


def lab4_app(tmp: Path) -> Lab4App:
    """A window signed in to a made-up Lab4 with one recording, /biohpc/data2/rec.h5, which has a sidecar."""
    (tmp / "mount" / "data2").mkdir(parents=True)
    (tmp / "mount" / "data2" / "rec.h5").write_bytes(b"h5")
    (tmp / "mount" / "data2" / "rec.h5.lab4.json").write_text('{"extra": {"rig": "B"}}')
    app = Lab4App(
        Settings(
            mount_root=str(tmp / "mount"), destinations=[["/biohpc/data2", "test"]]
        )
    )
    app.user, app.schema = "loson", lab4_schema()
    key = {
        "experimenter": "Eunji Kong",
        "collection_name": "place cells",
        "subject_id": "M231",
        "session_datetime": datetime(2026, 8, 21, 14, 30),
    }
    recording = {
        **key,
        "device_name": "Bruker 1 Imaging",
        "data_path": "/biohpc/data2/rec.h5",
        "ui_label": None,
        "weight_g": Decimal("24.50"),
    }
    app.lab4 = {
        "subject": [{"subject_id": "M231", "genotype": "WT", "dob": None, "sex": "U"}],
        "session": [{**key, "session_notes": ""}],
        "recording": [recording],
        "injection": [
            {
                "subject_id": "M231",
                "indicator": "AAV9-syn-jGCaMP8s",
                "injection_date": None,
                "injection_notes": "",
            }
        ],
    }
    app.registered = {recording["data_path"]: recording}
    app.connect()
    return app


def test_lab4_pages_render_a_picked_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    idl.ensure_assets()
    app = lab4_app(tmp_path)
    settle(app)
    app.select(name="rec.h5")
    app.show_in_lab4("/biohpc/data2/rec.h5")
    settle(app)
    app.edits_lab4["recording"]["ui_label"] = "day 1"
    frames = []

    def draw() -> None:
        frames.append(app.page)
        if len(frames) == FRAMES_PER_PAGE:
            app.page = "browse"
        app.dialog.render()
        if len(frames) == 3 * FRAMES_PER_PAGE:
            hello_imgui.get_runner_params().app_shall_exit = True

    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.app_window_params.window_geometry.size = (1040, 720)
    params.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    params.ini_filename = "imgui_lab4_interface_test.ini"
    params.callbacks.post_init = enable_textures
    params.callbacks.show_gui = draw
    try:
        immapp.run(params)
    except Exception as e:
        pytest.skip(f"null backend unavailable: {e}")
    app.executor.shutdown(wait=True)
    app.executor_long.shutdown(wait=True)

    assert frames[0] == "recordings" and frames[-1] == "browse"
    assert app.sidecar_lab4 == {"extra": {"rig": "B"}} and app.sidecar.startswith("{")
    app.page = "recordings"
    assert app.problems() == []
    assert [(table, values) for table, _, values in app.changes_lab4()] == [
        ("recording", {"ui_label": "day 1"})
    ]
    assert app.sidecar_rewrite()[0] == "/biohpc/data2/rec.h5.lab4.json"
    assert json.loads(app.sidecar_rewrite()[1])["extra"] == {"rig": "B"}


def test_sign_in_without_credentials_stays_signed_out(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path))
    app = Lab4App(Settings())
    app.sign_in()
    assert (
        app.user == ""
        and "cannot read" in app.error_login
        and "sign_in" not in app.futures
    )


def test_one_detected_mount_fills_an_unset_root(tmp_path, monkeypatch):
    from concurrent.futures import wait

    from imgui_lab4_interface.storage import Mount

    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path))
    found = [Mount("Y:\\", r"\\sshfs.kr\me@host!2216\biohpc")]
    monkeypatch.setattr("imgui_lab4_interface.app.find_mounts", lambda base: found)
    app = Lab4App(Settings())
    wait(list(app.futures.values()))
    app.poll()
    assert app.settings.mount_root == "Y:\\"

    kept = Lab4App(Settings(mount_root="Q:\\"))
    wait(list(kept.futures.values()))
    kept.poll()
    assert kept.settings.mount_root == "Q:\\" and kept.mounts == found

    found.append(Mount("Z:\\", "me@other:/biohpc"))
    ambiguous = Lab4App(Settings())
    wait(list(ambiguous.futures.values()))
    ambiguous.poll()
    assert ambiguous.settings.mount_root == ""


def test_no_mount_found_at_startup_means_ssh(tmp_path, monkeypatch):
    from concurrent.futures import wait

    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path))
    app = Lab4App(Settings())
    wait(list(app.futures.values()))
    app.poll()
    assert (app.settings.backend, app.settings.ssh_host) == ("ssh", "lab-research")
    app.settings.backend = "mounted"
    app.detect_mounts()
    wait(list(app.futures.values()))
    app.poll()
    assert app.settings.backend == "mounted"
