import pytest


@pytest.fixture(autouse=True)
def no_real_lab4(tmp_path, monkeypatch):
    # ~/.lab4/secrets.json is a real lab member's production login and X: may be real BioHPC; no test may reach either
    monkeypatch.setenv("LAB4_SECRETS_PATH", str(tmp_path / "no-lab4-secrets.json"))
    monkeypatch.setattr("imgui_lab4_interface.app.find_mounts", lambda base: [])
