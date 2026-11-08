"""Polite scraping core and the built-in Mixkit provider.

RemoSound only reads listing pages (trending, category, search) at a human
pace. :class:`Scraper` carries everything a source shares: an injected or
self-owned :class:`httpx.AsyncClient`, retry/backoff and the page walk. A
source subclasses it and supplies its URLs and its card parsing; parsed cards
become :class:`InstantButton` records that the downloader consumes.

Etiquette implemented here:

* ``delay`` seconds between every page request, retries included;
* HTTP 429 and 5xx answered with exponential backoff (``delay * 2**attempt``);
* crawlers stop as soon as ``limit`` buttons are collected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote, quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

MIXKIT_BASE_URL = "https://mixkit.co"
MIXKIT_SFX_URL = f"{MIXKIT_BASE_URL}/free-sound-effects/"
MIXKIT_CATEGORY_URL = f"{MIXKIT_BASE_URL}/free-sound-effects/{{category}}/"
MIXKIT_SEARCH_URL = f"{MIXKIT_BASE_URL}/free-sound-effects/discover/{{query}}/"

# The categories the site publishes, as (display name, listing slug).
MIXKIT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Transition", "transition"),
    ("Nature", "nature"),
    ("Technology", "technology"),
    ("Funny", "funny"),
    ("Notification", "notification"),
    ("Animals", "animals"),
    ("Transport", "transport"),
    ("Game", "game"),
    ("Whoosh", "whoosh"),
    ("Impact", "impact"),
    ("Cinematic", "cinematic"),
    ("Ambience", "ambience"),
    ("Alarm", "alarm"),
    ("Cartoon", "cartoon"),
)

USER_AGENT = "RemoSound/0.1 (polite sound-bank downloader)"

REQUEST_TIMEOUT = 20.0


@dataclass(frozen=True)
class InstantButton:
    """One sound card whose audio file could be resolved."""

    id: str
    title: str
    audio_url: str
    category: str
    page_url: str
    provider: str = "mixkit"


class Scraper:
    """Fetch listing pages without hammering the site.

    Subclasses implement :meth:`parse_buttons` and the three ``crawl_*``
    methods, usually as one-line calls to :meth:`crawl_listing`.
    """

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
                if not is_retryable(response.status_code) or attempt >= self.max_retries:
                    response.raise_for_status()
                    return response.text
            await asyncio.sleep(self.delay * 2**attempt)
            attempt += 1

    def parse_buttons(self, html: str, category_hint: str = "general") -> list[InstantButton]:
        raise NotImplementedError

    async def crawl_trending(self, limit: int = 100) -> list[InstantButton]:
        raise NotImplementedError

    async def crawl_category(self, category: str, limit: int = 100) -> list[InstantButton]:
        raise NotImplementedError

    async def crawl_search(self, query: str, limit: int = 100) -> list[InstantButton]:
        raise NotImplementedError

    async def crawl_listing(self, listing_url: str, category_hint: str, limit: int) -> list[InstantButton]:
        """Walk the 1-based pages of a listing until ``limit`` or an empty page.

        Ids already collected are skipped, and the walk stops as soon as a page
        contributes no new id. That bounds the cost both for a listing that runs
        out of pages and for a server that ignores ``?page=n`` and keeps serving
        the same page. A 404 past the first page ends the listing.
        """

        buttons: list[InstantButton] = []
        seen: set[str] = set()
        page = 1
        while len(buttons) < limit:
            try:
                html = await self.fetch_page(page_url(listing_url, page))
            except httpx.HTTPStatusError as err:
                if err.response.status_code == 404 and page > 1:
                    break
                raise

            # One card can appear twice on a page; keep the first occurrence so
            # a duplicate neither lands in the bank twice nor eats a limit slot.
            by_id = {button.id: button for button in self.parse_buttons(html, category_hint)}
            fresh = [button for item_id, button in by_id.items() if item_id not in seen]
            if not fresh:
                break
            seen.update(button.id for button in fresh)
            buttons.extend(fresh)
            page += 1
        return buttons[:limit]


class MixkitScraper(Scraper):
    """Fetch and parse Mixkit royalty-free sound effect pages."""

    def parse_buttons(self, html: str, category_hint: str = "general") -> list[InstantButton]:
        """Parse Mixkit sound cards from HTML, in document order."""

        soup = BeautifulSoup(html, "html.parser")
        buttons: list[InstantButton] = []
        for card in soup.select("div.item-grid-card"):
            player = (
                card
                if card.has_attr("data-audio-player-preview-url-value")
                else card.find(attrs={"data-audio-player-preview-url-value": True})
            )
            if not isinstance(player, Tag):
                continue
            audio_url = str(player.get("data-audio-player-preview-url-value") or "").strip()
            if not audio_url:
                continue

            raw_id = str(player.get("data-audio-player-item-id-value") or "").strip()
            if not raw_id:
                raw_id = path_segment(audio_url).replace(".mp3", "").replace("-preview", "")
            if not raw_id:
                continue

            item_id = f"mixkit-{raw_id}" if not raw_id.startswith("mixkit-") else raw_id
            title_node = card.select_one("h2.item-grid-card__title") or card.find("h2")
            title = title_node.get_text(" ", strip=True) if title_node else f"Mixkit {raw_id}"
            page_link = urljoin(MIXKIT_BASE_URL, f"/free-sound-effects/{raw_id}/")

            buttons.append(
                InstantButton(
                    id=item_id,
                    title=title,
                    audio_url=audio_url,
                    category=category_hint,
                    page_url=page_link,
                    provider="mixkit",
                )
            )
        return buttons

    async def crawl_category(self, category: str, limit: int = 100) -> list[InstantButton]:
        """Crawl one Mixkit category listing, stopping at ``limit`` buttons."""

        slug = quote(category.strip().lower(), safe="")
        return await self.crawl_listing(MIXKIT_CATEGORY_URL.format(category=slug), category, limit)

    async def crawl_search(self, query: str, limit: int = 100) -> list[InstantButton]:
        """Crawl Mixkit search results for ``query``, stopping at ``limit`` buttons."""

        slug = quote_plus(query.strip().lower())
        return await self.crawl_listing(MIXKIT_SEARCH_URL.format(query=slug), "search", limit)

    async def crawl_trending(self, limit: int = 100) -> list[InstantButton]:
        """Crawl the Mixkit main listing, stopping at ``limit`` buttons."""

        return await self.crawl_listing(MIXKIT_SFX_URL, "trending", limit)


def is_retryable(status_code: int) -> bool:
    """429 and 5xx are transient; every other status is final."""

    return status_code == 429 or 500 <= status_code <= 599


def path_segment(href: str) -> str:
    """Return the last non-empty path segment of ``href`` (the detail slug)."""

    return urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]


def page_url(listing_url: str, page: int) -> str:
    """Return ``listing_url`` carrying the 1-based ``page`` query parameter."""

    if page == 1:
        return listing_url
    separator = "&" if urlparse(listing_url).query else "?"
    return f"{listing_url}{separator}page={page}"
