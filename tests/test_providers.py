"""The provider registry: Mixkit built in, anything else a plugin file."""

from pathlib import Path

from remosound.providers import DEFAULT_PROVIDER, providers

PLUGIN = """
from remosound.providers import Provider
from remosound.scraper import MixkitScraper

PROVIDER = Provider(id="{id}", name="Demo", badge="Demo licence", scraper=MixkitScraper)
"""


def test_only_mixkit_is_built_in():
    assert list(providers()) == [DEFAULT_PROVIDER] == ["mixkit"]


def test_a_plugin_file_adds_a_provider(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "demo.py").write_text(PLUGIN.format(id="demo"), encoding="utf-8")
    monkeypatch.setenv("REMOSOUND_PLUGINS", str(plugins))

    found = providers()

    assert list(found) == ["mixkit", "demo"]
    assert found["demo"].badge == "Demo licence"


def test_a_broken_or_clashing_plugin_is_skipped(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "broken.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    (plugins / "wrong.py").write_text("PROVIDER = 42", encoding="utf-8")
    (plugins / "clash.py").write_text(PLUGIN.format(id="mixkit"), encoding="utf-8")
    monkeypatch.setenv("REMOSOUND_PLUGINS", str(plugins))

    assert list(providers()) == ["mixkit"]


def test_the_myinstants_example_plugin_loads_from_contrib(monkeypatch):
    monkeypatch.setenv("REMOSOUND_PLUGINS", str(Path(__file__).parent.parent / "contrib" / "providers"))

    assert "myinstants" in providers()
    assert providers()["myinstants"].badge == "Community"
