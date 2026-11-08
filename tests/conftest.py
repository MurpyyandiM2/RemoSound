import pytest


@pytest.fixture(autouse=True)
def _isolated_plugins(tmp_path, monkeypatch):
    """Never let a developer's own ~/.remosound/providers leak into a test run."""

    monkeypatch.setenv("REMOSOUND_PLUGINS", str(tmp_path / "no-plugins"))
