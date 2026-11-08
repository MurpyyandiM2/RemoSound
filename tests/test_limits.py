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


def test_an_absurd_limit_is_clamped_to_1000():
    assert clamp_limit(999_999_999) == 1000
    assert clamp_limit("999999999") == 1000
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


# --- what a remote page may make the downloader fetch -----------------------

import asyncio
import json

import httpx
import pytest

from remosound import downloader as downloader_module
from remosound import safety
from remosound.safety import DailyBudget, UnsafeDownloadError, require_public_http_url


def button(url="https://cdn.example.test/a.mp3", instant_id="a-1"):
    return InstantButton(id=instant_id, title="A", audio_url=url, category="search", page_url="p")


def downloader_for(tmp_path, handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BulkDownloader(StorageManager(tmp_path / "bank"), client=client, **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5000/api/delete",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin",
        "http://[::1]/",
        "file:///C:/Windows/win.ini",
        "ftp://example.test/a.mp3",
    ],
)
async def test_links_to_private_addresses_and_other_schemes_are_refused(url, monkeypatch):
    async def real_resolve(host, port):
        return [host.strip("[]")]  # an address literal resolves to itself, no DNS

    monkeypatch.setattr(safety, "_resolve", real_resolve)

    with pytest.raises(UnsafeDownloadError):
        await require_public_http_url(url)


async def test_a_public_link_is_allowed():
    await require_public_http_url("https://cdn.example.test/a.mp3")


async def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    async def rebinding(host, port):
        return ["10.0.0.7"]

    monkeypatch.setattr(safety, "_resolve", rebinding)

    with pytest.raises(UnsafeDownloadError):
        await require_public_http_url("https://innocent.example.test/a.mp3")


async def test_a_redirect_to_a_private_address_is_not_followed(tmp_path, monkeypatch):
    async def resolve(host, port):
        return ["127.0.0.1"] if host == "internal.test" else ["93.184.216.34"]

    monkeypatch.setattr(safety, "_resolve", resolve)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://internal.test/secret.mp3"})

    downloader = downloader_for(tmp_path, handler)
    assert await downloader.download_one(button()) is None

    assert seen == ["https://cdn.example.test/a.mp3"]  # the private hop was never requested
    assert not StorageManager(tmp_path / "bank").is_downloaded("a-1")


async def test_a_public_redirect_is_followed(tmp_path):
    def handler(request):
        if request.url.host == "cdn.example.test":
            return httpx.Response(302, headers={"location": "https://files.example.test/a.mp3"})
        return httpx.Response(200, content=b"ID3-audio")

    assert await downloader_for(tmp_path, handler).download_one(button()) is not None


async def test_an_oversized_body_is_cut_off_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "MAX_FILE_BYTES", 1024)
    downloader = downloader_for(tmp_path, lambda request: httpx.Response(200, content=b"x" * 5000))

    assert await downloader.download_one(button()) is None

    bank = tmp_path / "bank"
    assert not StorageManager(bank).is_downloaded("a-1")
    assert not list(bank.rglob("*.part"))


async def test_a_declared_oversized_body_is_refused_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "MAX_FILE_BYTES", 1024)
    downloader = downloader_for(
        tmp_path, lambda request: httpx.Response(200, headers={"content-length": "999999"}, content=b"ID3")
    )

    assert await downloader.download_one(button()) is None


# --- pacing and the daily budget --------------------------------------------


async def test_download_starts_are_spaced_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "MIN_START_GAP", 0.05)
    starts = []

    def handler(request):
        starts.append(asyncio.get_running_loop().time())
        return httpx.Response(200, content=b"ID3-audio")

    downloader = downloader_for(tmp_path, handler)
    await downloader.download_batch([button(instant_id=f"s{i}", url=f"https://cdn.example.test/{i}.mp3") for i in range(4)])

    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert len(starts) == 4
    assert min(gaps) >= 0.04


def test_the_daily_budget_counts_and_resets(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=2)
    assert not budget.exhausted()

    budget.spend()
    budget.spend()
    assert budget.exhausted()

    stale = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    stale["date"] = "2000-01-01"
    (tmp_path / "budget.json").write_text(json.dumps(stale), encoding="utf-8")
    assert not budget.exhausted()


def test_a_broken_budget_file_counts_as_unused(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("not json", encoding="utf-8")

    assert DailyBudget(path, limit=1).used() == 0


async def test_downloads_stop_when_the_daily_budget_is_spent(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=2)
    downloader = downloader_for(tmp_path, lambda request: httpx.Response(200, content=b"ID3-audio"), budget=budget)

    written = await downloader.download_batch(
        [button(instant_id=f"s{i}", url=f"https://cdn.example.test/{i}.mp3") for i in range(5)]
    )

    assert len(written) == 2
    assert budget.exhausted()
