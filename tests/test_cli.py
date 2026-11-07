"""Tests for the interactive and flag-driven CLI.

Every test is offline: scraper methods are monkeypatched, the downloader is
replaced by an in-memory stand-in, and all output lands in a pytest
``tmp_path``. Nothing here reads stdin.
"""

import io
import json
import sys

import httpx
import pytest

from remosound import cli as cli_module
from remosound.cli import build_parser, main, main_async
from remosound.scraper import InstantButton, MyInstantsScraper
from remosound.storage import StorageManager

BASE_URL = "https://www.myinstants.com"


def make_button(instant_id="boom-1", title="Vine Boom", category="memes"):
    return InstantButton(
        id=instant_id,
        title=title,
        audio_url=f"{BASE_URL}/media/sounds/{instant_id}.mp3",
        category=category,
        page_url=f"{BASE_URL}/instant/{instant_id}/",
    )


def patch_crawl(monkeypatch, method_name, buttons):
    """Replace one scraper crawl method with a fixed, offline answer."""

    async def crawl(self, *args, **kwargs):
        return list(buttons)

    monkeypatch.setattr(MyInstantsScraper, method_name, crawl)


def seed_bank(tmp_path, records):
    """Write a manifest with ``(id, title, category)`` records."""

    bank = tmp_path / "downloads"
    storage = StorageManager(bank)
    for instant_id, title, category in records:
        storage.record_download(
            instant_id=instant_id,
            title=title,
            category=category,
            relative_path=f"{category}/{title}-{instant_id}.mp3",
            tags=[],
        )
    storage.save()
    return bank


class FakeDownloader:
    """Stands in for :class:`BulkDownloader` without touching the network.

    Buttons whose id is in ``fail_ids`` are left out of the manifest, which is
    how the CLI is required to detect a failed download.
    """

    fail_ids: set[str] = set()
    instances: list["FakeDownloader"] = []

    def __init__(self, storage, max_concurrency=5, client=None):
        self.storage = storage
        self.max_concurrency = max_concurrency
        self.closed = False
        self.received: list[InstantButton] = []
        FakeDownloader.instances.append(self)

    async def download_batch(self, buttons, on_progress=None):
        self.received = list(buttons)
        written = []
        for index, button in enumerate(buttons):
            if button.id not in FakeDownloader.fail_ids:
                target = self.storage.category_dir(button.category) / f"{button.title}-{button.id}.mp3"
                target.write_bytes(b"ID3-fake")
                self.storage.record_download(
                    instant_id=button.id,
                    title=button.title,
                    category=button.category,
                    relative_path=target.relative_to(self.storage.root).as_posix(),
                    tags=[],
                )
                written.append(target)
            if on_progress is not None:
                on_progress(index + 1, len(buttons), button.title)
        self.storage.save()
        return written

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_downloader():
    FakeDownloader.fail_ids = set()
    FakeDownloader.instances = []
    yield


# --- argument parsing ------------------------------------------------------


def test_parser_reads_a_category_and_limit():
    args = build_parser().parse_args(["--category", "music", "--limit", "5"])

    assert args.category == "music"
    assert args.limit == 5
    assert args.trending is False
    assert args.search is None


def test_parser_defaults_match_the_documented_values():
    args = build_parser().parse_args([])

    assert args.limit == 100
    assert args.out.name == "downloads"
    assert args.max_concurrency == 5
    assert args.dry_run is False
    assert args.remotion_public is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--trending", "--category", "music"],
        ["--trending", "--search", "boom"],
        ["--category", "music", "--search", "boom"],
        ["--match", "win", "--rebuild-index"],
    ],
)
def test_mode_flags_are_mutually_exclusive(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize("value", ["0", "-3"])
def test_limit_must_be_positive(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--limit", value])


@pytest.mark.parametrize("value", ["", "   "])
def test_match_rejects_blank_scene_text(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--match", value])


def test_limit_is_capped_at_5000_per_run():
    args = build_parser().parse_args(["--limit", "99999"])

    assert args.limit == 5000


def test_max_concurrency_rejects_zero():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--max-concurrency", "0"])


def test_max_concurrency_above_five_parses_and_is_left_to_the_downloader():
    # The downloader clamps to 5 (Task 4); the parser must not reject the value
    # and must not silently rewrite it, so the clamp has one owner.
    args = build_parser().parse_args(["--max-concurrency", "99"])

    assert args.max_concurrency == 99


def test_help_lists_every_flag_and_the_limit_cap():
    help_text = build_parser().format_help()

    for flag in (
        "--trending",
        "--category",
        "--search",
        "--match",
        "--limit",
        "--out",
        "--rebuild-index",
        "--remotion-public",
        "--max-concurrency",
        "--dry-run",
    ):
        assert flag in help_text
    assert "5000" in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["--remotion-public", "public"],
        ["--dry-run", "--remotion-public", "public"],
        ["--match", "win", "--remotion-public", "public"],
    ],
)
def test_remotion_public_needs_a_bank_writing_mode(argv):
    with pytest.raises(SystemExit):
        main(argv)


# --- dry run ---------------------------------------------------------------


async def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    buttons = [make_button("boom-1", "Vine Boom"), make_button("pop-1", "Pop Whoosh", "sfx")]
    patch_crawl(monkeypatch, "crawl_category", buttons)
    bank = tmp_path / "downloads"

    code = await main_async(["--dry-run", "--category", "music", "--limit", "2", "--out", str(bank)])

    output = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in output
    assert "Vine Boom" in output
    assert "Pop Whoosh" in output
    assert not bank.exists()


async def test_dry_run_does_not_touch_the_downloader(tmp_path, monkeypatch):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])

    def exploding(*args, **kwargs):
        raise AssertionError("a dry run must not download")

    monkeypatch.setattr(cli_module, "BulkDownloader", exploding)

    code = await main_async(["--dry-run", "--trending", "--out", str(tmp_path / "downloads")])

    assert code == 0


# --- match -----------------------------------------------------------------


async def test_match_prints_machine_readable_json(tmp_path, capsys):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare", "sfx"), ("boom-1", "Vine Boom", "memes")])

    code = await main_async(["--match", "win", "--out", str(bank)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["context"] == "win"
    assert payload["matches"][0]["id"] == "win-1"
    assert payload["matches"][0]["path"] == "sfx/Win Fanfare-win-1.mp3"


class Cp1252Stream:
    """A stdout double that cannot encode emoji and has no ``reconfigure``."""

    encoding = "cp1252"

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        self.buffer.write(text.encode("cp1252", errors="strict"))
        return len(text)

    def flush(self):
        pass


async def test_match_json_survives_a_cp1252_stdout(tmp_path, monkeypatch):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare 🎉", "sfx")])
    stream = Cp1252Stream()
    monkeypatch.setattr(sys, "stdout", stream)

    code = await main_async(["--match", "win", "--out", str(bank)])

    payload = json.loads(stream.buffer.getvalue().decode("cp1252"))
    assert code == 0
    assert payload["matches"][0]["title"] == "Win Fanfare 🎉"


async def test_match_against_a_missing_bank_returns_an_empty_list(tmp_path, capsys):
    code = await main_async(["--match", "win", "--out", str(tmp_path / "nothing")])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["matches"] == []


# --- rebuild index ---------------------------------------------------------


async def test_rebuild_index_writes_registry_and_typescript_helper(tmp_path):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare", "sfx")])

    code = await main_async(["--rebuild-index", "--out", str(bank)])

    registry_path = tmp_path / "sound-bank.json"
    helper_path = tmp_path / "sound-bank.ts"
    assert code == 0
    assert registry_path.exists()
    assert helper_path.exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["version"] == 1
    assert registry["sounds"][0]["id"] == "win-1"
    assert "SOUND_BANK" in helper_path.read_text(encoding="utf-8")


async def test_rebuild_index_dry_run_writes_nothing(tmp_path):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare", "sfx")])

    code = await main_async(["--rebuild-index", "--dry-run", "--out", str(bank)])

    assert code == 0
    assert not (tmp_path / "sound-bank.json").exists()
    assert not (tmp_path / "sound-bank.ts").exists()


# --- batch downloads -------------------------------------------------------


async def test_batch_downloads_through_the_shared_downloader(tmp_path, monkeypatch, capsys):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    bank = tmp_path / "downloads"

    code = await main_async(["--trending", "--limit", "1", "--out", str(bank)])

    output = capsys.readouterr().out
    assert code == 0
    assert "Downloaded" in output
    storage = StorageManager(bank)
    assert storage.is_downloaded("boom-1")
    assert FakeDownloader.instances[0].closed is True
    # A plain batch does not write the registry; that is --rebuild-index's job.
    assert not (tmp_path / "sound-bank.json").exists()


async def test_batch_reports_failures_from_the_manifest(tmp_path, monkeypatch, capsys):
    buttons = [make_button("good-1", "Good One", "sfx"), make_button("bad-1", "Bad One", "sfx")]
    patch_crawl(monkeypatch, "crawl_trending", buttons)
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    FakeDownloader.fail_ids = {"bad-1"}
    bank = tmp_path / "downloads"

    code = await main_async(["--trending", "--limit", "2", "--out", str(bank)])

    output = capsys.readouterr().out
    assert code == 1
    assert "bad-1" in output
    storage = StorageManager(bank)
    assert storage.is_downloaded("good-1")
    assert not storage.is_downloaded("bad-1")


async def test_remotion_public_stages_a_successful_batch(tmp_path, monkeypatch):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    bank = tmp_path / "downloads"
    public = tmp_path / "remotion" / "public"

    code = await main_async(
        ["--trending", "--limit", "1", "--out", str(bank), "--remotion-public", str(public)]
    )

    assert code == 0
    assert (tmp_path / "sound-bank.json").exists()
    staged_registry = json.loads((public / "sound-bank.json").read_text(encoding="utf-8"))
    assert staged_registry["sounds"][0]["id"] == "boom-1"
    assert (public / "remosound" / "memes" / "Vine Boom-boom-1.mp3").exists()


async def test_max_concurrency_is_forwarded_to_the_downloader(tmp_path, monkeypatch):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)

    code = await main_async(
        ["--trending", "--limit", "1", "--max-concurrency", "2", "--out", str(tmp_path / "downloads")]
    )

    assert code == 0
    assert FakeDownloader.instances[0].max_concurrency == 2


async def test_staging_is_skipped_when_a_download_fails(tmp_path, monkeypatch, capsys):
    buttons = [make_button("good-1", "Good One", "sfx"), make_button("bad-1", "Bad One", "sfx")]
    patch_crawl(monkeypatch, "crawl_trending", buttons)
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    FakeDownloader.fail_ids = {"bad-1"}
    bank = tmp_path / "downloads"
    public = tmp_path / "public"

    code = await main_async(
        ["--trending", "--limit", "2", "--out", str(bank), "--remotion-public", str(public)]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "Skipped staging" in output
    assert not (tmp_path / "sound-bank.json").exists()
    assert not public.exists()


async def test_search_flag_crawls_the_query(tmp_path, monkeypatch, capsys):
    seen = {}

    async def crawl(self, query, limit=100):
        seen["query"] = query
        seen["limit"] = limit
        return []

    monkeypatch.setattr(MyInstantsScraper, "crawl_search", crawl)

    code = await main_async(["--search", "boom", "--limit", "3", "--out", str(tmp_path / "downloads")])

    assert code == 0
    assert seen == {"query": "boom", "limit": 3}
    assert "No sounds found" in capsys.readouterr().out


# --- failure handling ------------------------------------------------------


async def test_a_scrape_failure_exits_one_without_writing(tmp_path, monkeypatch, capsys):
    async def offline(self, *args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(MyInstantsScraper, "crawl_trending", offline)
    bank = tmp_path / "downloads"

    code = await main_async(["--trending", "--out", str(bank)])

    assert code == 1
    assert "offline" in capsys.readouterr().err
    assert not bank.exists()


# --- entrypoints -----------------------------------------------------------


def test_main_is_a_synchronous_wrapper(tmp_path):
    assert main(["--rebuild-index", "--out", str(tmp_path / "downloads")]) == 0


async def test_no_mode_flag_opens_the_interactive_menu(tmp_path, monkeypatch):
    seen = {}

    async def fake_menu(args, console):
        seen["called"] = True
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli_module, "interactive_menu", fake_menu)

    code = await main_async(["--limit", "7", "--out", str(tmp_path / "downloads")])

    assert code == 0
    assert seen["called"] is True
    assert seen["args"].limit == 7


async def test_dry_run_survives_interactive_menu_dispatch(tmp_path, monkeypatch, capsys):
    seen = {}

    async def crawl(self, limit=100):
        seen["limit"] = limit
        return [make_button()]

    monkeypatch.setattr(MyInstantsScraper, "crawl_trending", crawl)
    # Answer the menu without touching stdin: choice "1" is Trending.
    monkeypatch.setattr(cli_module.Prompt, "ask", lambda *args, **kwargs: "1")

    def exploding(*args, **kwargs):
        raise AssertionError("a dry run must not download")

    monkeypatch.setattr(cli_module, "BulkDownloader", exploding)
    bank = tmp_path / "downloads"

    code = await main_async(["--dry-run", "--limit", "7", "--out", str(bank)])

    output = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in output
    assert seen["limit"] == 7
    assert not bank.exists()
