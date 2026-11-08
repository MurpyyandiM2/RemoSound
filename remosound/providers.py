"""Where sounds come from: the built-in Mixkit provider plus optional plugins.

RemoSound is a bulk downloader and sound bank. Which site feeds it is the
user's choice: Mixkit ships built in, and any other source is a plugin file
dropped into the plugin directory. A plugin is a ``*.py`` file that defines a
module-level ``PROVIDER`` (a :class:`Provider`). Plugins are ordinary Python
run on your machine at startup, so install only files you trust.

The plugin directory is ``~/.remosound/providers`` unless ``REMOSOUND_PLUGINS``
names another one. ``contrib/providers`` in the repository holds examples.
"""

from __future__ import annotations

import functools
import importlib.util
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remosound.scraper import MIXKIT_CATEGORIES, MixkitScraper, Scraper

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "mixkit"
PLUGIN_ENV = "REMOSOUND_PLUGINS"


@dataclass(frozen=True)
class Provider:
    """One sound source: how to scrape it and how to label what it yields."""

    id: str
    name: str
    badge: str  # licence label shown on every card from this source
    scraper: Callable[[], Scraper]
    categories: tuple[tuple[str, str], ...] = ()


MIXKIT = Provider(
    id="mixkit",
    name="Mixkit",
    badge="Royalty-Free",
    scraper=MixkitScraper,
    categories=MIXKIT_CATEGORIES,
)


def plugin_dir() -> Path:
    return Path(os.environ.get(PLUGIN_ENV) or Path.home() / ".remosound" / "providers")


def providers() -> dict[str, Provider]:
    """Every provider available now, keyed by id; Mixkit first."""

    return dict(_load(str(plugin_dir())))


@functools.lru_cache(maxsize=None)
def _load(directory: str) -> dict[str, Provider]:
    found = {MIXKIT.id: MIXKIT}
    for path in sorted(Path(directory).glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"remosound_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            plugin = module.PROVIDER
            if not isinstance(plugin, Provider):
                raise TypeError("PROVIDER must be a remosound.providers.Provider")
        except Exception as error:  # noqa: BLE001 - a broken plugin must not stop the tool
            log.warning("skipped provider plugin %s: %s", path, error)
            continue
        if plugin.id in found:
            log.warning("skipped provider plugin %s: id %r is already taken", path, plugin.id)
            continue
        found[plugin.id] = plugin
    return found
