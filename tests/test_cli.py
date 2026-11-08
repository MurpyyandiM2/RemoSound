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
from remosound.providers import providers
from remosound.scraper import InstantButton, MixkitScraper
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

    monkeypatch.setattr(MixkitScraper, method_name, crawl)


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
                    provider=getattr(button, "provider", "myinstants"),
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


def test_limit_is_capped_at_1000_per_run():
    args = build_parser().parse_args(["--limit", "99999"])

    assert args.limit == 1000


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
    assert "1000" in help_text


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


async def test_batch_dry_run_mentions_the_remotion_staging_it_would_do(tmp_path, monkeypatch, capsys):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    public = tmp_path / "public"

    code = await main_async(
        [
            "--dry-run",
            "--trending",
            "--limit",
            "1",
            "--out",
            str(tmp_path / "downloads"),
            "--remotion-public",
            str(public),
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Dry run: would stage into" in output
    assert "public" in output
    assert not public.exists()


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


async def test_rebuild_refuses_to_overwrite_a_populated_registry_from_an_empty_bank(tmp_path, capsys):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare", "sfx")])
    assert await main_async(["--rebuild-index", "--out", str(bank)]) == 0
    registry_path = tmp_path / "sound-bank.json"
    before = registry_path.read_text(encoding="utf-8")

    # A corrupt manifest is quarantined and leaves the bank empty.
    (bank / "manifest.json").write_text("{ not json", encoding="utf-8")

    code = await main_async(["--rebuild-index", "--out", str(bank)])

    assert code == 1
    assert registry_path.read_text(encoding="utf-8") == before
    # rich wraps long paths, so compare whitespace-free text.
    err = "".join(capsys.readouterr().err.split())
    assert "refusingtooverwrite" in err
    assert "manifest.json.corrupt" in err


async def test_staging_refuses_to_replace_a_populated_public_registry(tmp_path):
    bank = seed_bank(tmp_path, [("win-1", "Win Fanfare", "sfx")])
    audio = bank / "sfx" / "Win Fanfare-win-1.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"ID3-fake")
    public = tmp_path / "public"
    argv = ["--rebuild-index", "--out", str(bank), "--remotion-public", str(public)]
    assert await main_async(argv) == 0
    staged = public / "sound-bank.json"
    before = staged.read_text(encoding="utf-8")

    (bank / "manifest.json").write_text("{ not json", encoding="utf-8")

    code = await main_async(argv)

    assert code == 1
    assert staged.read_text(encoding="utf-8") == before


async def test_an_empty_bank_may_still_create_a_first_registry(tmp_path):
    # Refusing is only about overwriting sounds that are already published.
    bank = tmp_path / "downloads"

    code = await main_async(["--rebuild-index", "--out", str(bank)])

    assert code == 0
    assert json.loads((tmp_path / "sound-bank.json").read_text(encoding="utf-8"))["sounds"] == []


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

    monkeypatch.setattr(MixkitScraper, "crawl_search", crawl)

    code = await main_async(["--search", "boom", "--limit", "3", "--out", str(tmp_path / "downloads")])

    assert code == 0
    assert seen == {"query": "boom", "limit": 3}
    assert "No sounds found" in capsys.readouterr().out


# --- failure handling ------------------------------------------------------


async def test_a_scrape_failure_exits_one_without_writing(tmp_path, monkeypatch, capsys):
    async def offline(self, *args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(MixkitScraper, "crawl_trending", offline)
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

    monkeypatch.setattr(MixkitScraper, "crawl_trending", crawl)
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


# --- web studio dispatch ---------------------------------------------------


def test_parser_exposes_the_web_studio_flag():
    args = build_parser().parse_args(["--web"])

    assert args.web is True


def test_web_is_mutually_exclusive_with_a_batch_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--web", "--trending"])


async def test_web_flag_serves_the_studio_and_forwards_the_bank_root(tmp_path, monkeypatch):
    seen = {}

    def fake_serve(out, **kwargs):
        seen["out"] = out
        seen["console"] = kwargs.get("console")

    monkeypatch.setattr(cli_module, "serve_web", fake_serve)
    bank = tmp_path / "bank"

    code = await main_async(["--web", "--out", str(bank)])

    assert code == 0
    assert seen["out"] == bank
    assert seen["console"] is not None


async def test_a_port_that_never_opens_exits_one_with_a_message(tmp_path, monkeypatch, capsys):
    def refuse(out, **kwargs):
        raise OSError("no free port")

    monkeypatch.setattr(cli_module, "serve_web", refuse)

    code = await main_async(["--web", "--out", str(tmp_path / "bank")])

    assert code == 1
    assert "no free port" in capsys.readouterr().err


# --- category menu ---------------------------------------------------------


def test_category_numbers_map_to_the_provider_slugs(monkeypatch):
    monkeypatch.setattr(cli_module.Prompt, "ask", lambda *args, **kwargs: "2")

    slug = cli_module._ask_category(cli_module.Console())

    assert slug == providers()["mixkit"].categories[1][1]


def test_provider_flag_options():
    parser = build_parser()
    assert parser.parse_args(["--trending"]).provider == "mixkit"
    assert parser.parse_args(["--trending", "--provider", "mixkit"]).provider == "mixkit"
    with pytest.raises(SystemExit):
        parser.parse_args(["--trending", "--provider", "unknown"])


async def test_batch_mixkit_provider(tmp_path, monkeypatch):
    seen = {}

    async def crawl(self, query, limit=100):
        seen["called"] = True
        seen["query"] = query
        return [
            InstantButton(
                id="mixkit-1",
                title="Whoosh",
                audio_url="https://assets.mixkit.co/preview.mp3",
                category="search",
                page_url="https://mixkit.co/free-sound-effects/1/",
                provider="mixkit",
            )
        ]

    monkeypatch.setattr(MixkitScraper, "crawl_search", crawl)
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)

    code = await main_async(
        ["--search", "whoosh", "--provider", "mixkit", "--limit", "1", "--out", str(tmp_path / "downloads")]
    )

    assert code == 0
    assert seen.get("called") is True
    assert seen.get("query") == "whoosh"
    storage = StorageManager(tmp_path / "downloads")
    assert storage.is_downloaded("mixkit-1")
    assert storage.manifest["mixkit-1"]["provider"] == "mixkit"


async def test_delete_flag_removes_sound(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes")])
    assert StorageManager(bank).is_downloaded("boom-1")

    code = await main_async(["--delete", "boom-1", "--out", str(bank)])
    assert code == 0
    assert not StorageManager(bank).is_downloaded("boom-1")



def test_the_category_table_shows_each_real_category(capsys, monkeypatch):
    monkeypatch.setattr(cli_module.Prompt, "ask", lambda *args, **kwargs: "1")
    monkeypatch.setenv("COLUMNS", "400")

    cli_module._ask_category(cli_module.Console())

    output = capsys.readouterr().out
    categories = providers()["mixkit"].categories
    assert len(categories) == 14
    for label, _slug in categories:
        assert label in output
    assert {"transition", "whoosh", "cinematic"} <= {slug for _label, slug in categories}


# --- summary ---------------------------------------------------------------


async def test_the_summary_names_the_bank_and_the_next_commands(tmp_path, monkeypatch, capsys):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    # A wide console keeps the path on one line; rich reads COLUMNS when it is
    # not writing to a terminal.
    monkeypatch.setenv("COLUMNS", "400")
    bank = tmp_path / "downloads"

    code = await main_async(["--trending", "--limit", "1", "--out", str(bank)])

    output = capsys.readouterr().out
    assert code == 0
    assert str(bank.resolve()) in output
    assert "Vine Boom" in output
    assert "--match" in output
    assert "--web" in output
    assert "--rebuild-index" in output


async def test_a_custom_bank_root_is_carried_into_the_next_commands(tmp_path, monkeypatch, capsys):
    patch_crawl(monkeypatch, "crawl_trending", [make_button()])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)
    monkeypatch.setenv("COLUMNS", "400")
    bank = tmp_path / "custom-bank"

    await main_async(["--trending", "--limit", "1", "--out", str(bank)])

    output = capsys.readouterr().out
    assert f'--out "{bank}"' in output



# --- --project: the bank lives inside the Remotion project ------------------


def remotion_project(tmp_path):
    project = tmp_path / "my-video"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    return project


async def test_project_flag_downloads_straight_into_the_remotion_public_folder(tmp_path, monkeypatch):
    project = remotion_project(tmp_path)
    patch_crawl(monkeypatch, "crawl_search", [make_button("wind-1", "Strong Wind", "search")])
    monkeypatch.setattr(cli_module, "BulkDownloader", FakeDownloader)

    code = await main_async(["--search", "wind", "--project", str(project), "--limit", "1"])

    assert code == 0
    bank = project / "public" / "remosound"
    assert StorageManager(bank).is_downloaded("wind-1")
    # The registry sits beside the bank, in public/, where staticFile() and the helper expect it.
    registry = json.loads((project / "public" / "sound-bank.json").read_text(encoding="utf-8"))
    assert registry["sounds"][0]["src"].startswith("remosound/")
    assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize(
    "extra",
    [["--out", "elsewhere"], ["--remotion-public", "somewhere"]],
)
async def test_project_flag_refuses_a_second_destination(tmp_path, extra):
    project = remotion_project(tmp_path)

    with pytest.raises(SystemExit) as raised:
        await main_async(["--trending", "--project", str(project), *extra])

    assert raised.value.code == 2


async def test_project_flag_needs_a_real_remotion_project(tmp_path):
    with pytest.raises(SystemExit) as raised:
        await main_async(["--trending", "--project", str(tmp_path / "typo")])

    assert raised.value.code == 2
    assert not (tmp_path / "typo").exists()
