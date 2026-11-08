import pytest

from remosound import scraper


@pytest.fixture(autouse=True)
def _isolated_plugins(tmp_path, monkeypatch):
    """Never let a developer's own ~/.remosound/providers leak into a test run."""

    monkeypatch.setenv("REMOSOUND_PLUGINS", str(tmp_path / "no-plugins"))


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    """Keep offline tests fast; test_limits.py puts the real floors back."""

    monkeypatch.setattr(scraper, "MIN_DELAY", 0)
