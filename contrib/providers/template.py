"""Template for a RemoSound provider plugin.

Copy this file, rename it, and change the numbered spots below to describe the
site you want to download from. Put the copy in ``~/.remosound/providers`` (or
the folder named by ``$REMOSOUND_PLUGINS``), then pick it with
``--provider <id>``. RemoSound ships no audio and no site-specific code; you
choose the source and you are responsible for the rights to what you download.

RemoSound's limits (page delay, per-run and daily caps, public addresses only,
50 MB per file) apply to every plugin and cannot be lowered here.

The site below, ``sounds.example.com``, does not exist: this file loads, but it
downloads nothing until you point it at a real site.
"""

from __future__ import annotations

from urllib.parse import quote, quote_plus, urljoin

from bs4 import BeautifulSoup

from remosound.providers import Provider
from remosound.scraper import InstantButton, Scraper, path_segment

# 1. The site and its listing pages. Page 2 is fetched by adding ``?page=2``
#    (or ``&page=2``), so each URL must be a page that lists many sounds.
BASE_URL = "https://sounds.example.com"
TRENDING_URL = f"{BASE_URL}/popular/"
CATEGORY_URL = f"{BASE_URL}/category/{{category}}/"
SEARCH_URL = f"{BASE_URL}/search/?q={{query}}"

# 2. Categories shown in the web studio, as (label, text used in CATEGORY_URL).
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Nature", "nature"),
    ("Impact", "impact"),
)


class TemplateScraper(Scraper):
    """Read the sound cards out of one listing page."""

    def parse_buttons(self, html: str, category_hint: str = "general") -> list[InstantButton]:
        soup = BeautifulSoup(html, "html.parser")
        buttons: list[InstantButton] = []

        # 3. One CSS selector that matches every sound card on a listing page.
        #    Find it with your browser: right-click a sound, "Inspect", and look
        #    for the element that wraps one sound (its class, such as "sound-card").
        for card in soup.select("div.sound-card"):
            # 4. Inside a card: the link to the sound's own page (gives the id
            #    and title) and the address of the audio file. Adjust each
            #    selector or attribute to match what "Inspect" shows.
            link = card.select_one("a[href]")
            audio = card.select_one("audio[src]")
            if link is None or audio is None:
                continue  # a card without an audio file is skipped

            href = str(link["href"])
            sound_id = path_segment(href)
            if not sound_id:
                continue

            buttons.append(
                InstantButton(
                    id=sound_id,
                    title=link.get_text(" ", strip=True) or sound_id,
                    audio_url=urljoin(BASE_URL, str(audio["src"]).strip()),
                    category=category_hint,
                    page_url=urljoin(BASE_URL, href),
                    provider="mysite",  # must equal PROVIDER.id below
                )
            )
        return buttons

    # These three rarely need changing: they only choose which listing to walk.
    async def crawl_category(self, category: str, limit: int = 100) -> list[InstantButton]:
        url = CATEGORY_URL.format(category=quote(category.strip(), safe=""))
        return await self.crawl_listing(url, category, limit)

    async def crawl_search(self, query: str, limit: int = 100) -> list[InstantButton]:
        return await self.crawl_listing(SEARCH_URL.format(query=quote_plus(query.strip())), "search", limit)

    async def crawl_trending(self, limit: int = 100) -> list[InstantButton]:
        return await self.crawl_listing(TRENDING_URL, "trending", limit)


# 5. Name the source. ``id`` is what you type after ``--provider``; ``badge`` is
#    the licence label shown on each card (for example "Royalty-Free" or "Check licence").
PROVIDER = Provider(
    id="mysite",
    name="My Site",
    badge="Check licence",
    scraper=TemplateScraper,
    categories=CATEGORIES,
)
