"""Tests for the bounded-concurrency downloader.

Every test is offline: audio bytes come from :class:`httpx.MockTransport` and
every file lands in a pytest ``tmp_path``.
"""

import asyncio
import json

import httpx

from remosound import downloader as downloader_module
from remosound.downloader import BulkDownloader
from remosound.scraper import InstantButton
from remosound.storage import StorageManager, sanitize_filename

AUDIO = b"ID3fake"

BASE_URL = "https://www.myinstants.com"


def make_button(
    instant_id: str = "boom-1",
    title: str = "Boom",
    category: str = "memes",
    audio_url: str | None = None,
) -> InstantButton:
    return InstantButton(
        id=instant_id,
        title=title,
        audio_url=audio_url or f"{BASE_URL}/media/sounds/{instant_id}.mp3",
        category=category,
        page_url=f"{BASE_URL}/instant/{instant_id}/",
    )


def make_downloader(tmp_path, handler, max_concurrency: int = 5):
    """Build a downloader whose client answers through a MockTransport handler."""

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = StorageManager(tmp_path)
    return BulkDownloader(storage, max_concurrency=max_concurrency, client=client), storage, client


def audio_handler(calls: list | None = None):
    """A handler that serves the fake MP3 and records every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, content=AUDIO)

    return handler


async def test_download_one_writes_audio_into_the_category_folder(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())

    async with client:
        path = await downloader.download_one(make_button())

    assert path is not None
    assert path.parent == storage.category_dir("memes")
    assert path.name == "Boom-boom-1.mp3"
    assert path.read_bytes() == AUDIO
    assert not path.with_suffix(".mp3.part").exists()


async def test_download_one_records_and_saves_the_manifest_entry(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())

    async with client:
        path = await downloader.download_one(make_button())

    assert path is not None
    assert storage.is_downloaded("boom-1")
    entry = storage.manifest["boom-1"]
    assert entry["title"] == "Boom"
    assert entry["category"] == "memes"
    assert entry["path"] == path.relative_to(tmp_path).as_posix()

    on_disk = json.loads(storage.manifest_path.read_text(encoding="utf-8"))
    assert on_disk["boom-1"]["path"] == entry["path"]


async def test_download_one_skips_a_manifest_entry_without_an_http_request(tmp_path):
    calls: list = []
    downloader, storage, client = make_downloader(tmp_path, audio_handler(calls))
    button = make_button()

    async with client:
        first = await downloader.download_one(button)
        second = await downloader.download_one(button)

    assert first is not None
    assert second is None
    assert len(calls) == 1


async def test_download_one_skips_an_existing_file_even_without_a_manifest_entry(tmp_path):
    calls: list = []
    downloader, storage, client = make_downloader(tmp_path, audio_handler(calls))
    target = storage.category_dir("memes") / f"{sanitize_filename('Boom', fallback='boom-1')}-boom-1.mp3"
    target.write_bytes(AUDIO)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is None
    assert calls == []


async def test_download_one_returns_none_and_leaves_no_files_after_http_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "RETRY_DELAYS", ())
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="boom")

    downloader, storage, client = make_downloader(tmp_path, handler)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is None
    assert len(calls) == 1
    assert list(storage.category_dir("memes").iterdir()) == []


async def test_download_one_retries_a_retryable_status(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "RETRY_DELAYS", (0.0,))
    calls: list = []
    responses = iter([httpx.Response(500, text="boom"), httpx.Response(200, content=AUDIO)])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    downloader, storage, client = make_downloader(tmp_path, handler)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is not None
    assert len(calls) == 2
    assert path.read_bytes() == AUDIO


async def test_download_one_gives_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, text="slow down")

    downloader, storage, client = make_downloader(tmp_path, handler)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is None
    # one attempt plus three retries
    assert len(calls) == 4
    assert list(storage.category_dir("memes").iterdir()) == []


async def test_download_one_deletes_the_part_file_when_the_transfer_breaks(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "RETRY_DELAYS", ())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped")

    downloader, storage, client = make_downloader(tmp_path, handler)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is None
    assert list(storage.category_dir("memes").iterdir()) == []


async def test_download_one_sanitizes_forbidden_characters_in_the_title(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())

    async with client:
        path = await downloader.download_one(make_button(title='Boom: "Big"? <Now>'))

    assert path is not None
    assert path.name == "Boom Big Now-boom-1.mp3"


async def test_download_one_keeps_the_filename_under_150_characters(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())

    async with client:
        path = await downloader.download_one(make_button(title="very long title " * 20))

    assert path is not None
    assert len(path.name) < 150
    assert path.name.endswith("-boom-1.mp3")


def assert_contained(path, storage) -> None:
    """The written file must be the single, resolved file inside its folder."""

    files = list(path.parent.iterdir())
    assert files == [path]
    assert path.resolve().parent == path.parent.resolve()
    assert path.resolve().is_relative_to(storage.root.resolve())
    assert path.name.endswith(".mp3")


async def test_download_one_contains_a_traversal_id_inside_the_category_folder(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())
    button = make_button(instant_id=r"..\..\..\..\planted", title="Evil", audio_url=f"{BASE_URL}/media/sounds/x.mp3")

    async with client:
        path = await downloader.download_one(button)

    assert path is not None
    assert_contained(path, storage)
    assert path.parent == storage.category_dir("memes")
    assert storage.is_downloaded(r"..\..\..\..\planted")
    assert not (tmp_path / "planted.mp3").exists()


async def test_download_one_sanitizes_a_path_separator_in_the_id(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())
    button = make_button(instant_id="evil/planted", title="Evil", audio_url=f"{BASE_URL}/media/sounds/x.mp3")

    async with client:
        path = await downloader.download_one(button)

    assert path is not None
    assert_contained(path, storage)
    assert path.parent == storage.category_dir("memes")


async def test_download_one_sanitizes_a_colon_in_the_id_without_residue(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())
    button = make_button(instant_id="boom:1", title="Boom", audio_url=f"{BASE_URL}/media/sounds/x.mp3")

    async with client:
        path = await downloader.download_one(button)

    assert path is not None
    assert ":" not in path.name
    assert path.name == "Boom-boom1.mp3"
    assert_contained(path, storage)
    assert list(path.parent.glob("*.part")) == []


async def test_download_one_keeps_a_300_character_id_under_150_characters(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())
    button = make_button(instant_id="x" * 300, title="Boom", audio_url=f"{BASE_URL}/media/sounds/x.mp3")

    async with client:
        path = await downloader.download_one(button)

    assert path is not None
    assert len(path.name) < 150
    assert_contained(path, storage)
    assert storage.is_downloaded("x" * 300)


async def test_concurrent_downloads_of_one_button_fetch_once(tmp_path):
    calls: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        # yield so a second task can pass its pre-check before this one records
        await asyncio.sleep(0)
        calls.append(request)
        return httpx.Response(200, content=AUDIO)

    downloader, storage, client = make_downloader(tmp_path, handler)
    button = make_button()

    async with client:
        results = await asyncio.gather(downloader.download_one(button), downloader.download_one(button))

    assert len(calls) == 1
    paths = [path for path in results if path is not None]
    assert len(paths) == 1
    assert paths[0].read_bytes() == AUDIO


async def test_download_one_rejects_an_empty_body_without_leaving_files(tmp_path):
    downloader, storage, client = make_downloader(tmp_path, lambda request: httpx.Response(200, content=b""))

    async with client:
        path = await downloader.download_one(make_button())

    assert path is None
    assert list(storage.category_dir("memes").iterdir()) == []
    assert not storage.is_downloaded("boom-1")


async def test_download_one_still_returns_the_path_when_saving_the_manifest_fails(tmp_path, monkeypatch):
    downloader, storage, client = make_downloader(tmp_path, audio_handler())

    def broken_save() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save", broken_save)

    async with client:
        path = await downloader.download_one(make_button())

    assert path is not None
    assert path.read_bytes() == AUDIO
    assert storage.is_downloaded("boom-1")


def test_constructor_clamps_max_concurrency_to_five(tmp_path):
    downloader = BulkDownloader(StorageManager(tmp_path), max_concurrency=99)

    assert downloader.max_concurrency == 5


def test_constructor_clamps_max_concurrency_up_to_one(tmp_path):
    assert BulkDownloader(StorageManager(tmp_path), max_concurrency=0).max_concurrency == 1
    assert BulkDownloader(StorageManager(tmp_path), max_concurrency=-3).max_concurrency == 1


async def test_download_batch_downloads_every_button_and_reports_progress(tmp_path):
    calls: list = []
    downloader, storage, client = make_downloader(tmp_path, audio_handler(calls))
    buttons = [make_button(f"boom-{index}", title=f"Boom {index}") for index in range(1, 4)]
    progress: list[tuple[int, int, str]] = []

    async with client:
        paths = await downloader.download_batch(
            buttons, on_progress=lambda done, total, label: progress.append((done, total, label))
        )

    assert len(paths) == 3
    assert all(path.exists() for path in paths)
    assert len(calls) == 3
    assert [done for done, _, _ in progress] == [1, 2, 3]
    assert {total for _, total, _ in progress} == {3}
    assert {label for _, _, label in progress} == {"Boom 1", "Boom 2", "Boom 3"}


async def test_download_batch_skips_failures_but_still_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "RETRY_DELAYS", ())

    def handler(request: httpx.Request) -> httpx.Response:
        if "missing" in str(request.url):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, content=AUDIO)

    downloader, storage, client = make_downloader(tmp_path, handler)
    buttons = [make_button("boom-1"), make_button("missing-2"), make_button("boom-3")]
    progress: list[tuple[int, int, str]] = []

    async with client:
        paths = await downloader.download_batch(
            buttons, on_progress=lambda done, total, label: progress.append((done, total, label))
        )

    assert len(paths) == 2
    assert [done for done, _, _ in progress] == [1, 2, 3]


async def test_download_batch_downloads_a_duplicate_id_only_once(tmp_path):
    calls: list = []
    downloader, storage, client = make_downloader(tmp_path, audio_handler(calls))
    button = make_button()
    progress: list[tuple[int, int, str]] = []

    async with client:
        paths = await downloader.download_batch(
            [button, button], on_progress=lambda done, total, label: progress.append((done, total, label))
        )

    assert len(paths) == 1
    assert len(calls) == 1
    assert [done for done, _, _ in progress] == [1]


async def test_download_batch_never_exceeds_its_concurrency_limit(tmp_path):
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=AUDIO)

    downloader, storage, client = make_downloader(tmp_path, handler, max_concurrency=2)
    buttons = [make_button(f"boom-{index}") for index in range(1, 6)]

    async with client:
        paths = await downloader.download_batch(buttons)

    assert len(paths) == 5
    assert peak <= 2


async def test_close_leaves_an_injected_client_open(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(audio_handler()))
    downloader = BulkDownloader(StorageManager(tmp_path), client=client)

    await downloader.close()

    assert not client.is_closed
    await client.aclose()


async def test_close_closes_a_client_the_downloader_created(tmp_path):
    downloader = BulkDownloader(StorageManager(tmp_path))
    client = downloader.client

    await downloader.close()

    assert client.is_closed
