"""Example provider plugin: MyInstants.

Not loaded by default. Copy this file into your plugin directory
(``~/.remosound/providers`` or ``$REMOSOUND_PLUGINS``) to add MyInstants as a
source, then pick it with ``--provider myinstants``.

MyInstants hosts community uploads with no licence, and much of it is
copyrighted. Whether you may use a downloaded sound is your responsibility.
This file is also the template for writing a provider of your own: subclass
:class:`remosound.scraper.Scraper`, implement the four methods, expose
``PROVIDER``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, quote_plus, urljoin

from bs4 import BeautifulSoup, Tag

from remosound.providers import Provider
from remosound.scraper import InstantButton, Scraper, path_segment

BASE_URL = "https://www.myinstants.com"
TRENDING_URL = f"{BASE_URL}/en/index/us/"
CATEGORY_URL = f"{BASE_URL}/en/categories/{{category}}/"
SEARCH_URL = f"{BASE_URL}/en/search/?name={{query}}"

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Sound Effects", "sound effects"),
    ("Memes", "memes"),
    ("Music", "music"),
    ("Games", "games"),
    ("Anime & Manga", "anime & manga"),
    ("Movies", "movies"),
    ("Television", "television"),
    ("Pranks", "pranks"),
    ("Reactions", "reactions"),
    ("TikTok Trends", "tiktok trends"),
    ("Viral", "viral"),
    ("Sports", "sports"),
    ("Politics", "politics"),
    ("WhatsApp Audios", "whatsapp audios"),
)

# The inline play handler, e.g. onmousedown="play('/media/sounds/boom.mp3')".
# Test markup and older pages use onmousedown with a bare argument; the current
# site uses onclick and passes extra arguments after the filename:
# onclick="play('/media/sounds/boom.mp3', 'loader-1', 'boom-2')".
_PLAY_HANDLER = re.compile(r"play\('([^']+\.mp3)'(?:,\s*'[^']*')*\)")
_HANDLER_ATTRIBUTES = ("onmousedown", "onclick")


class MyInstantsScraper(Scraper):
    """Fetch and parse MyInstants listing pages."""

    def parse_buttons(self, html: str, category_hint: str = "general") -> list[InstantButton]:
        """Parse the sound cards of one listing page, in document order.

        A card is a ``div.instant``. Its title and detail link come from
        ``a.instant-link`` (any ``a[href*="/instant/"]`` is accepted), the id is
        the last segment of that detail-link path, and the audio file is read
        from a play handler (``onmousedown`` or the site's current ``onclick``),
        then ``button[data-url]``, then ``audio[src]``. Relative audio URLs are
        resolved against :data:`BASE_URL`. Cards with no detail link, no id, or
        no audio reference are skipped.
        """

        soup = BeautifulSoup(html, "html.parser")
        buttons: list[InstantButton] = []
        for card in soup.select("div.instant"):
            detail = card.select_one("a.instant-link[href]") or card.select_one('a[href*="/instant/"]')
            if detail is None:
                continue

            href = str(detail["href"])
            instant_id = path_segment(href)
            audio_url = _audio_url(card)
            if not instant_id or audio_url is None:
                continue

            title_anchor = card.select_one(".instant-link") or detail
            buttons.append(
                InstantButton(
                    id=instant_id,
                    title=title_anchor.get_text(" ", strip=True),
                    audio_url=audio_url,
                    category=category_hint,
                    page_url=urljoin(BASE_URL, href),
                    provider="myinstants",
                )
            )
        return buttons

    async def crawl_category(self, category: str, limit: int = 100) -> list[InstantButton]:
        """Crawl one category listing; ``"movie clips"`` becomes ``/en/categories/movie%20clips/``."""

        return await self.crawl_listing(CATEGORY_URL.format(category=quote(category.strip(), safe="")), category, limit)

    async def crawl_search(self, query: str, limit: int = 100) -> list[InstantButton]:
        """Crawl search results, tagged ``"search"`` so the downloader keeps them together."""

        return await self.crawl_listing(SEARCH_URL.format(query=quote_plus(query.strip())), "search", limit)

    async def crawl_trending(self, limit: int = 100) -> list[InstantButton]:
        """Crawl the trending listing."""

        return await self.crawl_listing(TRENDING_URL, "trending", limit)


def _audio_url(card: Tag) -> str | None:
    """Resolve a card's audio file from a play handler, ``data-url``, or ``<audio>``."""

    for attribute in _HANDLER_ATTRIBUTES:
        trigger = card if card.has_attr(attribute) else card.find(attrs={attribute: True})
        if not isinstance(trigger, Tag):
            continue
        match = _PLAY_HANDLER.search(str(trigger.get(attribute) or ""))
        if match:
            return _absolute(match.group(1))

    data_url = card.select_one("button[data-url]")
    if data_url is not None:
        return _absolute(str(data_url["data-url"]))

    audio = card.select_one("audio[src]")
    if audio is not None:
        return _absolute(str(audio["src"]))

    return None


def _absolute(url: str) -> str:
    return urljoin(BASE_URL, url.strip())


PROVIDER = Provider(
    id="myinstants",
    name="MyInstants",
    badge="Community",
    scraper=MyInstantsScraper,
    categories=CATEGORIES,
)
