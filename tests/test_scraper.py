"""Tests for the polite MyInstants scraper.

Every test is offline: pages come from ``tests/fixtures`` and HTTP goes through
:class:`httpx.MockTransport`, never the live site.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from remosound.scraper import BASE_URL, MyInstantsScraper

FIXTURE = Path(__file__).parent / "fixtures" / "myinstants_page.html"

EXPECTED_AUDIO_URLS = [
    f"{BASE_URL}/media/sounds/a.mp3",
    f"{BASE_URL}/media/sounds/b.mp3",
    f"{BASE_URL}/media/sounds/c.mp3",
]

EXPECTED_IDS = [
    "boing-sound-effect-101",
    "coin-pickup-202",
    "whoosh-transition-303",
]

EXPECTED_TITLES = ["Boing Sound Effect", "Coin Pickup", "Whoosh Transition"]


def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def parse_fixture(category_hint: str = "general"):
    """Parse the fixture with a scraper that never opens a connection."""

    return MyInstantsScraper(delay=0).parse_buttons(fixture_html(), category_hint)


def scraper_for(handler, **kwargs) -> tuple[MyInstantsScraper, httpx.AsyncClient]:
    """Build a scraper whose client answers through a MockTransport handler."""

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MyInstantsScraper(client=client, delay=0, **kwargs), client


def test_parse_buttons_returns_cards_with_audio_in_document_order():
    buttons = parse_fixture()

    assert len(buttons) == 3
    assert [button.id for button in buttons] == EXPECTED_IDS
    assert [button.title for button in buttons] == EXPECTED_TITLES


def test_parse_buttons_absolutizes_audio_urls():
    buttons = parse_fixture()

    assert [button.audio_url for button in buttons] == EXPECTED_AUDIO_URLS


def test_parse_buttons_extracts_id_from_detail_link_slug():
    buttons = parse_fixture()

    assert [button.id for button in buttons] == EXPECTED_IDS
    assert buttons[0].page_url == f"{BASE_URL}/instant/boing-sound-effect-101/"


def test_parse_buttons_skips_card_without_audio_reference():
    buttons = parse_fixture()

    assert "silent-placeholder-404" not in {button.id for button in buttons}
    assert all(button.audio_url.endswith(".mp3") for button in buttons)


def test_parse_buttons_propagates_category_hint():
    buttons = parse_fixture(category_hint="music")

    assert [button.category for button in buttons] == ["music", "music", "music"]


def test_parse_buttons_defaults_to_general_category():
    buttons = parse_fixture()

    assert [button.category for button in buttons] == ["general", "general", "general"]


def test_parse_buttons_reads_current_onclick_play_handler():
    """The live site calls play() with extra arguments from onclick."""

    html = """
    <div class="instant">
      <button class="small-button"
              onclick="play('/media/sounds/boom.mp3', 'loader-7', 'boom-7')"></button>
      <a class="instant-link link-secondary" href="/en/instant/boom-7/">Boom</a>
    </div>
    """

    buttons = MyInstantsScraper(delay=0).parse_buttons(html)

    assert [button.id for button in buttons] == ["boom-7"]
    assert buttons[0].audio_url == f"{BASE_URL}/media/sounds/boom.mp3"
    assert buttons[0].page_url == f"{BASE_URL}/en/instant/boom-7/"


async def test_crawl_trending_stops_at_limit_without_extra_page_fetches():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text=fixture_html())

    scraper, client = scraper_for(handler)
    async with client:
        buttons = await scraper.crawl_trending(limit=2)

    assert [button.id for button in buttons] == EXPECTED_IDS[:2]
    assert len(requested_urls) <= 1


async def test_crawl_paginates_until_a_page_has_no_buttons():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.params.get("page") in (None, "1"):
            return httpx.Response(200, text=fixture_html())
        return httpx.Response(200, text="<html><body></body></html>")

    scraper, client = scraper_for(handler)
    async with client:
        buttons = await scraper.crawl_trending(limit=10)

    assert len(buttons) == 3
    assert len(requested_urls) == 2


async def test_crawl_category_and_search_build_their_urls():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text="<html><body></body></html>")

    scraper, client = scraper_for(handler)
    async with client:
        await scraper.crawl_category("memes", limit=5)
        await scraper.crawl_search("boom", limit=5)

    assert requested_urls[0] == f"{BASE_URL}/en/categories/memes/"
    assert requested_urls[1] == f"{BASE_URL}/en/search/?name=boom"


async def test_crawl_category_percent_encodes_the_category_name():
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text="<html><body></body></html>")

    scraper, client = scraper_for(handler)
    async with client:
        await scraper.crawl_category("movie clips", limit=5)

    assert requested_urls == [f"{BASE_URL}/en/categories/movie%20clips/"]


async def test_crawl_dedupes_repeated_ids_within_one_page():
    """A duplicated card must not consume one of the ``limit`` slots."""

    html = """
    <div class="instant">
      <a class="instant-link" href="/en/instant/boom-1/">Boom</a>
      <button onmousedown="play('/media/sounds/boom.mp3')"></button>
    </div>
    <div class="instant">
      <a class="instant-link" href="/en/instant/boom-1/">Boom</a>
      <button onmousedown="play('/media/sounds/boom.mp3')"></button>
    </div>
    <div class="instant">
      <a class="instant-link" href="/en/instant/pop-2/">Pop</a>
      <button onmousedown="play('/media/sounds/pop.mp3')"></button>
    </div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    scraper, client = scraper_for(handler)
    async with client:
        buttons = await scraper.crawl_trending(limit=2)

    assert [button.id for button in buttons] == ["boom-1", "pop-2"]


async def test_crawl_stops_when_page_numbers_are_ignored():
    """A server that serves the same page for every ?page must not be looped on."""

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text=fixture_html())

    scraper, client = scraper_for(handler)
    async with client:
        buttons = await scraper.crawl_trending(limit=10)

    assert [button.id for button in buttons] == EXPECTED_IDS
    assert len(requested_urls) == 2


async def test_fetch_page_retries_a_429_then_succeeds():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="<html>ok</html>")

    scraper, client = scraper_for(handler)
    async with client:
        body = await scraper.fetch_page(f"{BASE_URL}/en/index/us/")

    assert body == "<html>ok</html>"
    assert call_count == 2


async def test_fetch_page_gives_up_after_retry_budget():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="unavailable")

    scraper, client = scraper_for(handler, max_retries=3)
    async with client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await scraper.fetch_page(f"{BASE_URL}/en/index/us/")

    assert call_count == 4
    assert error.value.response.status_code == 503


async def test_fetch_page_waits_the_default_delay_before_the_first_request(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper = MyInstantsScraper(client=client, delay=0.5)
    async with client:
        await scraper.fetch_page(f"{BASE_URL}/en/index/us/")

    assert sleeps == [0.5]


async def test_fetch_page_backs_off_exponentially_between_retries(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper = MyInstantsScraper(client=client, delay=0.5, max_retries=3)
    async with client:
        body = await scraper.fetch_page(f"{BASE_URL}/en/index/us/")

    assert body == "<html>ok</html>"
    assert call_count == 4
    # 0.5 s before every attempt, then 0.5 s, 1 s, and 2 s of backoff.
    assert sleeps == [0.5, 0.5, 0.5, 1.0, 0.5, 2.0, 0.5]
