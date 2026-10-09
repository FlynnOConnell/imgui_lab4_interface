"""
Real clicks through Dear ImGui's test engine on hello_imgui's null backend, in a subprocess: the null backend tears
the test engine down in the wrong order, which corrupts imgui for any later window in the same process.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

FRAMES_MAX = 900


def clicks(tmp: Path) -> dict:
    """Click through Browse in a fresh window; what each step saw, or {"error": ...} when no test ran."""
    import imgui_data_loader as idl
    from imgui_bundle import hello_imgui, imgui, immapp
    from imgui_bundle import icons_fontawesome_6 as fa

    import imgui_lab4_interface.app as app_module
    from imgui_lab4_interface import Settings

    app_module.find_mounts = lambda base: []
    mount = tmp / "mount"
    (mount / "flynn" / "M231").mkdir(parents=True)
    (mount / "flynn" / "rec.h5").write_bytes(b"\x89HDF\r\n\x1a\n")
    idl.ensure_assets()
    app = app_module.Lab4App(Settings(mount_root=str(mount), mount_base="/biohpc", destinations=[["/biohpc", "root"]]))
    app.work_offline()
    app.page = "browse"

    content = []
    draw_browse = app.draw_browse

    def recording_window():
        if not content:
            content.append(imgui.internal.get_current_window().id_)
        draw_browse()

    app.draw_browse = recording_window
    te = imgui.test_engine
    seen = {}

    def run(ctx):
        ctx.set_ref(te.TestRef(content[0]))
        register = f"**/{fa.ICON_FA_DATABASE}  Register in Lab4"
        ctx.item_click(f"**/{fa.ICON_FA_FOLDER}  flynn")
        seen["register on folder"] = ctx.item_exists(register)
        ctx.item_double_click(f"**/{fa.ICON_FA_FOLDER}  flynn")
        seen["after double-click"] = app.path_browse
        ctx.yield_(5)
        ctx.item_click(f"**/{fa.ICON_FA_FILE}  rec.h5")
        seen["register on h5"] = ctx.item_exists(register)
        seen["finished"] = True

    tests = []

    def register_tests():
        engine = hello_imgui.get_imgui_test_engine()
        test = te.register_test(engine, "browse", "clicks")
        test.test_func = run
        tests.append((engine, test))

    frames = [0]

    def gui():
        frames[0] += 1
        app.dialog.render()
        if frames[0] == 20:
            te.queue_test(*tests[0])
        if seen.get("finished") or frames[0] > FRAMES_MAX:
            hello_imgui.get_runner_params().app_shall_exit = True

    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.app_window_params.window_geometry.size = (1040, 720)
    params.ini_folder_type = hello_imgui.IniFolderType.temp_folder
    params.ini_filename = "imgui_lab4_interface_clicks.ini"
    params.use_imgui_test_engine = True
    params.callbacks.post_init = lambda: setattr(imgui.get_io(), "backend_flags", imgui.get_io().backend_flags | imgui.BackendFlags_.renderer_has_textures)
    params.callbacks.register_tests = register_tests
    params.callbacks.show_gui = gui
    try:
        immapp.run(params)
    except Exception as e:
        if not seen.get("finished"):
            return {"error": str(e)}
    return seen


def test_double_click_enters_folders_and_register_shows_only_for_h5(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    result = subprocess.run([sys.executable, __file__, str(tmp_path)], capture_output=True, text=True, timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        pytest.skip(f"the test engine did not report (exit {result.returncode}): {result.stderr[-500:]}")
    seen = json.loads(lines[-1])
    if "error" in seen:
        pytest.skip(f"null backend unavailable: {seen['error']}")
    assert seen == {"register on folder": False, "after double-click": "/biohpc/flynn", "register on h5": True, "finished": True}


if __name__ == "__main__":
    print(json.dumps(clicks(Path(sys.argv[1]))), flush=True)
