"""The hard limits that keep RemoSound from hammering a site.

They live in the shared layers, so a plugin source cannot opt out of them.
"""

from remosound import scraper as scraper_module
from remosound.downloader import MAX_CONCURRENCY, BulkDownloader
from remosound.scraper import InstantButton, MixkitScraper, Scraper
from remosound.storage import StorageManager
from remosound.web import DownloadJob, JobRegistry, clamp_limit


def test_a_provider_cannot_fetch_faster_than_the_minimum_delay(monkeypatch):
    monkeypatch.setattr(scraper_module, "MIN_DELAY", 0.5)

    assert MixkitScraper(delay=0).delay == 0.5
    assert MixkitScraper(delay=0.01).delay == 0.5
    assert MixkitScraper(delay=2).delay == 2


async def test_one_listing_is_never_walked_past_the_page_cap(monkeypatch):
    monkeypatch.setattr(scraper_module, "MAX_PAGES", 3)
    fetched = []

    class Endless(Scraper):
        async def fetch_page(self, url):
            fetched.append(url)
            return str(len(fetched))

        def parse_buttons(self, html, category_hint="general"):
            # Every page offers a brand-new sound, so only the page cap can stop the walk.
            return [InstantButton(id=f"s{html}", title="t", audio_url="u", category="c", page_url="p")]

    buttons = await Endless().crawl_listing("https://example.test/list", "c", limit=5000)

    assert len(fetched) == 3
    assert len(buttons) == 3


def test_an_absurd_limit_is_clamped_to_5000():
    assert clamp_limit(999_999_999) == 5000
    assert clamp_limit("999999999") == 5000
    assert clamp_limit(-5) == 1


def test_the_downloader_never_runs_more_than_five_at_once(tmp_path):
    assert BulkDownloader(StorageManager(tmp_path), max_concurrency=10_000).max_concurrency == MAX_CONCURRENCY == 5


def test_only_one_download_job_runs_at_a_time():
    registry = JobRegistry()
    first, second = DownloadJob("trending", "", 10), DownloadJob("trending", "", 10)

    assert registry.add_if_idle(first) is first
    assert registry.add_if_idle(second) is None

    first.update(state="done")

    assert registry.add_if_idle(second) is second
