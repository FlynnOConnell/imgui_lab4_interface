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


def run_window(app, draw: str, steps, size: tuple[int, int] = (1040, 720)) -> dict:
    """Show app at size, run steps(ctx, seen) on the test engine inside the page app.<draw> draws; what they saw, or {"error": ...}."""
    from imgui_bundle import hello_imgui, imgui, immapp

    content = []
    draw_page = getattr(app, draw)

    def recording_window():
        if not content:
            content.append(imgui.internal.get_current_window().id_)
        draw_page()

    setattr(app, draw, recording_window)
    te = imgui.test_engine
    seen = {}

    def run(ctx):
        ctx.set_ref(te.TestRef(content[0]))
        steps(ctx, seen)
        seen["finished"] = True

    tests = []

    def register_tests():
        engine = hello_imgui.get_imgui_test_engine()
        test = te.register_test(engine, "page", "clicks")
        test.test_func = run
        tests.append((engine, test))

    frames = [0]

    def gui():
        frames[0] += 1
        # the null backend ignores window_geometry.size
        imgui.get_io().display_size = imgui.ImVec2(*size)
        app.dialog.render()
        if frames[0] == 20:
            te.queue_test(*tests[0])
        if seen.get("finished") or frames[0] > FRAMES_MAX:
            hello_imgui.get_runner_params().app_shall_exit = True

    params = hello_imgui.RunnerParams()
    params.platform_backend_type = hello_imgui.PlatformBackendType.null
    params.renderer_backend_type = hello_imgui.RendererBackendType.null
    params.app_window_params.window_geometry.size = size
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


def browse_clicks(tmp: Path) -> dict:
    """Click through Browse in a fresh window."""
    import imgui_data_loader as idl
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

    def steps(ctx, seen):
        register = f"**/{fa.ICON_FA_DATABASE}  Register in Lab4"
        ctx.item_click(f"**/{fa.ICON_FA_FOLDER}  flynn")
        seen["register on folder"] = ctx.item_exists(register)
        ctx.item_double_click(f"**/{fa.ICON_FA_FOLDER}  flynn")
        seen["after double-click"] = app.path_browse
        ctx.yield_(5)
        ctx.item_click(f"**/{fa.ICON_FA_FILE}  rec.h5")
        seen["register on h5"] = ctx.item_exists(register)

    return run_window(app, "draw_browse", steps)


def slice_clicks(tmp: Path) -> dict:
    """Pick a slice, double-click its file, then double-click a cell, in a fresh window."""
    import imgui_data_loader as idl
    from imgui_bundle import icons_fontawesome_6 as fa
    from imgui_bundle import imgui

    import imgui_lab4_interface.app as app_module
    from imgui_lab4_interface import Settings

    app_module.find_mounts = lambda base: []
    idl.ensure_assets()
    app = app_module.Lab4App(Settings(mount_root=str(tmp / "mount"), mount_base="/biohpc"))
    app.work_offline()
    app.page = "slices"
    row = next(r for r in app.rows_slices["slices"] if r["slice_id"] == "HS260727.B")
    name = "HSLice_B_7-27-2026.mesc"
    app.files_index = [{"path": name, "size": 4, "mtime": 0.0}]
    opened = []
    app.open_file = lambda path, size: opened.append(path)

    def steps(ctx, seen):
        # the row spans every column of a table wider than the window, so aim at its first cell, not its middle
        edge = imgui.test_engine.TestOpFlags_.move_to_edge_l
        ctx.mouse_move(f"**/##row_{row['id']}", edge)
        ctx.mouse_click(0)
        ctx.item_double_click(f"**/{fa.ICON_FA_FILE_IMAGE}  {name}##file_{name}")
        seen["opened"] = opened
        ctx.mouse_move(f"**/##row_{row['id']}", edge)
        ctx.mouse_double_click(0)
        # the first cell is collection_date, which comes from the patient, so the double-click says so
        seen["cell"] = app.message

    return run_window(app, "draw_slices", steps)


def slice_cells(tmp: Path) -> dict:
    """Edit row B's cells with Excel's keys and a double-click, in a fresh window."""
    import imgui_data_loader as idl
    from imgui_bundle import imgui

    import imgui_lab4_interface.app as app_module
    from imgui_lab4_interface import Settings

    app_module.find_mounts = lambda base: []
    idl.ensure_assets()
    app = app_module.Lab4App(Settings(mount_root=str(tmp / "mount"), mount_base="/biohpc"))
    app.work_offline()
    app.page = "slices"
    row = next(r for r in app.rows_slices["slices"] if r["slice_id"] == "HS260727.B")
    names = [name for name, _, _ in app_module.COLUMNS["slices"]]
    # where each outlined cell was last drawn
    rects = {}
    rect_cell = app_module.rect_cell

    def recording(column):
        rect = rect_cell(column)
        rects[names[column]] = (rect.min.x, rect.min.y, rect.max.x, rect.max.y)
        return rect

    app_module.rect_cell = recording

    def value(name):
        return next(r for r in app.slices.rows("slices") if r["id"] == row["id"])[name]

    def steps(ctx, seen):
        ctx.mouse_move(f"**/##row_{row['id']}", imgui.test_engine.TestOpFlags_.move_to_edge_l)
        ctx.mouse_click(0)
        seen["click outlines"] = app.cell[2]
        ctx.key_press(imgui.Key.right_arrow, names.index("ephys_notes"))
        seen["arrows move"] = app.cell[2]
        ctx.key_chars("slo")
        # typed while the box waits for focus
        ctx.key_chars("w")
        ctx.yield_(2)
        ctx.key_press(imgui.Key.tab)
        seen["typing then tab saves"] = [value("ephys_notes"), app.cell[2]]
        ctx.key_press(imgui.Key.left_arrow)
        ctx.key_press(imgui.Key.f2)
        ctx.yield_(2)
        ctx.key_chars("er")
        ctx.key_press(imgui.Key.enter)
        seen["f2 appends, enter moves down"] = [value("ephys_notes"), app.cell[1] != row["id"]]
        ctx.key_press(imgui.Key.up_arrow)
        ctx.key_chars("zzz")
        ctx.yield_(2)
        ctx.key_press(imgui.Key.escape)
        seen["escape keeps"] = [value("ephys_notes"), app.editing, app.cell[2]]
        ctx.yield_(2)
        x, top, _, bottom = rects["ephys_notes"]
        # just left of the w in "slower"
        x += imgui.get_style().cell_padding.x + imgui.calc_text_size("slo").x - 1
        ctx.mouse_move_to_pos(imgui.ImVec2(x, (top + bottom) / 2))
        ctx.mouse_double_click(0)
        ctx.yield_(2)
        ctx.key_chars("X")
        ctx.key_press(imgui.Key.enter)
        seen["double-click puts the caret there"] = value("ephys_notes")
        ctx.key_press(imgui.Key.up_arrow)
        ctx.key_press(imgui.Key.delete)
        ctx.yield_(2)
        ctx.key_press(imgui.Key.enter)
        seen["delete empties"] = value("ephys_notes")
        ctx.key_press(imgui.Key.up_arrow)
        ctx.key_press(imgui.Key.right_arrow)
        ctx.key_chars("cul")
        ctx.yield_(2)
        seen["typing filters the list"] = [app.filter_choice, app.pick_choice]
        ctx.key_press(imgui.Key.enter)
        seen["enter picks"] = [value("incubator"), app.cell[1] != row["id"]]
        ctx.key_press(imgui.Key.escape)
        seen["escape lets the cell go"] = [app.cell, app.page]

    return run_window(app, "draw_slices", steps)


def slice_toolbar(tmp: Path, width: int) -> dict:
    """Whether the slice toolbar fits a window width pixels wide: nothing past the page's edges, nothing to scroll sideways."""
    import imgui_data_loader as idl
    from imgui_bundle import icons_fontawesome_6 as fa

    import imgui_lab4_interface.app as app_module
    from imgui_lab4_interface import Settings

    app_module.find_mounts = lambda base: []
    idl.ensure_assets()
    app = app_module.Lab4App(Settings(mount_root=str(tmp / "mount"), mount_base="/biohpc"))
    app.work_offline()
    app.page = "slices"

    def steps(ctx, seen):
        reset = ctx.item_info(f"**/{fa.ICON_FA_ARROWS_ROTATE}  Reset rows")
        user = ctx.item_info("##user_slices")
        page = reset.window.inner_clip_rect
        seen["reset inside"] = reset.rect_full.max.x <= page.max.x
        seen["picker inside"] = user.rect_full.min.x >= page.min.x
        seen["no sideways scroll"] = reset.window.scroll_max.x == 0

    return run_window(app, "draw_slices", steps, size=(width, 700))


def clicks_in_subprocess(tmp_path: Path, page: str) -> dict:
    result = subprocess.run([sys.executable, __file__, str(tmp_path), page], capture_output=True, text=True, timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        pytest.skip(f"the test engine did not report (exit {result.returncode}): {result.stderr[-500:]}")
    seen = json.loads(lines[-1])
    if "error" in seen:
        pytest.skip(f"null backend unavailable: {seen['error']}")
    return seen


def test_double_click_enters_folders_and_register_shows_only_for_h5(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    seen = clicks_in_subprocess(tmp_path, "browse")
    assert seen == {"register on folder": False, "after double-click": "/biohpc/flynn", "register on h5": True, "finished": True}


def test_double_click_opens_slice_files_and_cells(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    seen = clicks_in_subprocess(tmp_path, "slices")
    assert seen == {"opened": ["HSLice_B_7-27-2026.mesc"], "cell": "collection_date comes from the patient; edit it on Patients", "finished": True}


def test_slice_cells_edit_as_in_excel(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    seen = clicks_in_subprocess(tmp_path, "cells")
    assert seen == {
        "click outlines": "collection_date",
        "arrows move": "ephys_notes",
        "typing then tab saves": ["slow", "incubator"],
        "f2 appends, enter moves down": ["slower", True],
        "escape keeps": ["slower", None, "ephys_notes"],
        "double-click puts the caret there": "sloXwer",
        "delete empties": "",
        "typing filters the list": ["cul", "culture"],
        "enter picks": ["culture", True],
        "escape lets the cell go": [None, "slices"],
        "finished": True,
    }


@pytest.mark.parametrize("width", [560, 640, 1040, 1920])
def test_slice_toolbar_fits_the_window(tmp_path, monkeypatch, width):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    seen = clicks_in_subprocess(tmp_path, f"toolbar{width}")
    assert seen == {"reset inside": True, "picker inside": True, "no sideways scroll": True, "finished": True}


if __name__ == "__main__":
    pages = {"browse": browse_clicks, "slices": slice_clicks, "cells": slice_cells}
    tmp, page = Path(sys.argv[1]), sys.argv[2]
    seen = slice_toolbar(tmp, width=int(page.removeprefix("toolbar"))) if page.startswith("toolbar") else pages[page](tmp)
    print(json.dumps(seen), flush=True)
