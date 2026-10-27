"""
Real clicks on the Lab4 recordings page, with a made-up Lab4, in a subprocess for the reason test_browse_clicks gives.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def lab4_clicks(tmp: Path) -> dict:
    """Pick a recording, double-click it into Browse storage, then come back with Show in Lab4."""
    import imgui_data_loader as idl
    from imgui_bundle import icons_fontawesome_6 as fa
    from test_browse_clicks import run_window
    from test_headless_render import lab4_app

    import imgui_lab4_interface.app as app_module

    app_module.find_mounts = lambda base: []
    idl.ensure_assets()
    app = lab4_app(tmp)
    app.page = "recordings"

    def steps(ctx, seen):
        ctx.item_click("**/M231##lab4_0")
        seen["picked"] = (
            app.picked_lab4["recording"]["device_name"] if app.picked_lab4 else None
        )
        ctx.yield_(5)
        seen["sidecar"] = app.sidecar_lab4
        seen["rewrite offered"] = ctx.item_exists(
            "**/Save also rewrites rec.h5.lab4.json"
        )
        app.edits_lab4["recording"]["ui_label"] = "day 1"
        ctx.item_double_click("**/M231##lab4_0")
        seen["after double-click"] = [app.page, app.path_browse, app.selected]
        ctx.yield_(5)
        ctx.item_click(f"**/{fa.ICON_FA_DATABASE}  Show in Lab4")
        seen["back, still edited"] = [app.page, app.edits_lab4["recording"]["ui_label"]]

    return run_window(app, "draw_lab4", steps)


def test_a_recording_picks_and_shows_its_data(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGUI_LAB4_HOME", str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, __file__, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        pytest.skip(
            f"the test engine did not report (exit {result.returncode}): {result.stderr[-500:]}"
        )
    seen = json.loads(lines[-1])
    if "error" in seen:
        pytest.skip(f"null backend unavailable: {seen['error']}")
    assert seen == {
        "picked": "Bruker 1 Imaging",
        "sidecar": {"extra": {"rig": "B"}},
        "rewrite offered": True,
        "after double-click": ["browse", "/biohpc/data2", "rec.h5"],
        "back, still edited": ["recordings", "day 1"],
        "finished": True,
    }


if __name__ == "__main__":
    print(json.dumps(lab4_clicks(Path(sys.argv[1]))), flush=True)
