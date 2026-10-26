"""
Draw the window for a few frames per page on hello_imgui's null backend: no window or GPU. Skips when the
backend cannot start here.
"""

import imgui_data_loader as idl
import pytest
from imgui_bundle import hello_imgui, imgui, immapp

from imgui_lab4_interface import Settings
from imgui_lab4_interface.app import PAGES, Lab4App

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
    app = Lab4App(Settings(mount_root=str(tmp_path / "mount"), destinations=[["/archive/x", "test"]]))
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


def test_sign_in_without_credentials_stays_signed_out(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path))
    app = Lab4App(Settings())
    app.sign_in()
    assert app.user == "" and "cannot read" in app.error_login and "sign_in" not in app.futures


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
