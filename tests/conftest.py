import pytest

from remosound import downloader, safety, scraper


@pytest.fixture(autouse=True)
def _isolated_plugins(tmp_path, monkeypatch):
    """Never let a developer's own ~/.remosound leak into a test run."""

    monkeypatch.setenv("REMOSOUND_PLUGINS", str(tmp_path / "no-plugins"))
    monkeypatch.setenv("REMOSOUND_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _offline_and_fast(monkeypatch):
    """Keep offline tests fast and DNS-free; test_limits.py puts the real floors back."""

    monkeypatch.setattr(scraper, "MIN_DELAY", 0)
    monkeypatch.setattr(downloader, "MIN_START_GAP", 0)

    async def public_address(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(safety, "_resolve", public_address)
