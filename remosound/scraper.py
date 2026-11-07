"""Polite MyInstants scraping: page fetching, retry/backoff, and card parsing.

RemoSound only reads listing pages (trending, category, search) at a human
pace, and this module is the only place that knows MyInstants' markup. Pages
are fetched through an injected or self-owned :class:`httpx.AsyncClient`, and
parsed cards become :class:`InstantButton` records that the downloader
consumes.

Etiquette implemented here:

* ``delay`` seconds between every page request, retries included;
* HTTP 429 and 5xx answered with exponential backoff (``delay * 2**attempt``);
* crawlers stop as soon as ``limit`` buttons are collected.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.myinstants.com"
TRENDING_URL = f"{BASE_URL}/en/index/us/"
CATEGORY_URL = f"{BASE_URL}/en/categories/{{category}}/"
SEARCH_URL = f"{BASE_URL}/en/search/?name={{query}}"

USER_AGENT = "RemoSound/0.1 (polite sound-bank downloader; +https://www.myinstants.com)"

REQUEST_TIMEOUT = 20.0

# The inline play handler, e.g. onmousedown="play('/media/sounds/boom.mp3')".
# Test markup and older pages use onmousedown with a bare argument; the current
# site uses onclick and passes extra arguments after the filename:
# onclick="play('/media/sounds/boom.mp3', 'loader-1', 'boom-2')".
_PLAY_HANDLER = re.compile(r"play\('([^']+\.mp3)'(?:,\s*'[^']*')*\)")
_HANDLER_ATTRIBUTES = ("onmousedown", "onclick")


@dataclass(frozen=True)
class InstantButton:
    """One MyInstants sound card whose audio file could be resolved."""

    id: str
    title: str
    audio_url: str
    category: str
    page_url: str


class MyInstantsScraper:
    """Fetch and parse MyInstants listing pages without hammering the site."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        delay: float = 0.5,
        max_retries: int = 3,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        """The HTTP client, created on first request when none was injected."""

        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        """Close the client this scraper created.

        An injected client belongs to its caller and is left open.
        """

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_page(self, url: str) -> str:
        """Return the HTML at ``url``, retrying transient failures politely.

        ``self.delay`` is awaited before every attempt, retries included, and a
        retry additionally backs off ``self.delay * 2**attempt``. A 429, any 5xx
        response, or a transport error is retried up to ``self.max_retries``
        times; once the budget is exhausted the failure is raised — retryable
        statuses as :class:`httpx.HTTPStatusError`, transport errors unchanged.
        """

        attempt = 0
        while True:
            await asyncio.sleep(self.delay)
            try:
                response = await self.client.get(url)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
            else:
                if not _is_retryable(response.status_code) or attempt >= self.max_retries:
                    response.raise_for_status()
                    return response.text
            await asyncio.sleep(self.delay * 2**attempt)
            attempt += 1

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
            instant_id = _path_segment(href)
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
                )
            )
        return buttons

    async def crawl_category(self, category: str, limit: int = 100) -> list[InstantButton]:
        """Crawl one category listing, stopping at ``limit`` buttons."""

        return await self._crawl(CATEGORY_URL.format(category=category.strip()), category, limit)

    async def crawl_search(self, query: str, limit: int = 100) -> list[InstantButton]:
        """Crawl search results for ``query``, stopping at ``limit`` buttons.

        Results are tagged with the ``"search"`` category so the downloader
        keeps them together.
        """

        return await self._crawl(SEARCH_URL.format(query=quote_plus(query.strip())), "search", limit)

    async def crawl_trending(self, limit: int = 100) -> list[InstantButton]:
        """Crawl the trending listing, stopping at ``limit`` buttons."""

        return await self._crawl(TRENDING_URL, "trending", limit)

    async def _crawl(self, listing_url: str, category_hint: str, limit: int) -> list[InstantButton]:
        """Walk the 1-based pages of a listing until ``limit`` or an empty page."""

        buttons: list[InstantButton] = []
        page = 1
        while len(buttons) < limit:
            html = await self.fetch_page(_page_url(listing_url, page))
            page_buttons = self.parse_buttons(html, category_hint)
            if not page_buttons:
                break
            buttons.extend(page_buttons)
            page += 1
        return buttons[:limit]


def _is_retryable(status_code: int) -> bool:
    """429 and 5xx are transient; every other status is final."""

    return status_code == 429 or 500 <= status_code <= 599


def _path_segment(href: str) -> str:
    """Return the last non-empty path segment of ``href`` (the detail slug)."""

    return urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]


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


def _page_url(listing_url: str, page: int) -> str:
    """Return ``listing_url`` carrying the 1-based ``page`` query parameter."""

    if page == 1:
        return listing_url
    separator = "&" if urlparse(listing_url).query else "?"
    return f"{listing_url}{separator}page={page}"
