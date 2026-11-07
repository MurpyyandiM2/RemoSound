"""Bounded-concurrency downloading of resolved MyInstants sounds.

A :class:`BulkDownloader` turns :class:`~remosound.scraper.InstantButton`
records into files inside a :class:`~remosound.storage.StorageManager` bank.
Every download is streamed to a ``.part`` file and renamed into place only
after the transfer finishes, so a broken transfer never leaves a corrupt
``.mp3`` behind, and the manifest is written only after the rename.

Etiquette implemented here:

* at most **five** downloads run at once (the constructor clamps higher
  requests down to five, and one :class:`asyncio.Semaphore` gates every
  transfer);
* HTTP 429 and 5xx responses are retried with exponential backoff
  (``0.5 s``, ``1 s``, ``2 s``) before the sound is abandoned;
* ids already in the manifest are never fetched again.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from remosound.scraper import USER_AGENT, InstantButton
from remosound.storage import StorageManager, sanitize_filename

log = logging.getLogger(__name__)

MAX_CONCURRENCY = 5
MIN_CONCURRENCY = 1

# Windows refuses paths that leave too little room for the directory, so the
# generated name stays comfortably below the classic 260-character limit.
MAX_FILENAME_LENGTH = 150

# Backoff after each failed attempt; the last entry is the third retry.
RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

REQUEST_TIMEOUT = 20.0
CHUNK_SIZE = 64 * 1024


class BulkDownloader:
    """Download buttons into the storage bank, five at a time at most."""

    def __init__(
        self,
        storage: StorageManager,
        max_concurrency: int = MAX_CONCURRENCY,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.storage = storage
        self.max_concurrency = max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, max_concurrency))
        self.semaphore = asyncio.Semaphore(self.max_concurrency)
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        """The HTTP client, created on first use when none was injected."""

        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        """Close the client this downloader created.

        An injected client belongs to its caller and is left open.
        """

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def download_one(self, button: InstantButton) -> Path | None:
        """Download one button and return the written path.

        ``None`` means nothing was written: the id is already recorded, the
        target file already exists, or the transfer kept failing.
        """

        if self.storage.is_downloaded(button.id):
            return None

        target = self._target_path(button)
        if target.exists():
            return None

        async with self.semaphore:
            # Re-check under the semaphore: a queued duplicate may have
            # finished while this task waited for its slot.
            if self.storage.is_downloaded(button.id) or target.exists():
                return None
            try:
                await self._transfer(button, target)
            except (httpx.HTTPError, OSError) as error:
                log.warning("could not download %s: %s", button.id, error)
                return None

            # Mutate and persist without an intervening await, so concurrent
            # record/save pairs cannot lose one another's entries.
            self.storage.record_download(
                instant_id=button.id,
                title=button.title,
                category=button.category,
                relative_path=target.relative_to(self.storage.root).as_posix(),
                tags=[],
            )
            self.storage.save()
            return target

    async def download_batch(
        self,
        buttons: list[InstantButton],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> list[Path]:
        """Download every button and return the paths that were written.

        Buttons are identified by instant id, so duplicates in ``buttons``
        collapse to a single download. Progress is reported once per unique
        button as ``on_progress(completed, total, title)``.
        """

        unique = list({button.id: button for button in buttons}.values())
        total = len(unique)
        results: list[Path | None] = [None] * total
        completed = 0

        async def run(index: int, button: InstantButton) -> None:
            nonlocal completed
            try:
                results[index] = await self.download_one(button)
            except Exception:
                # One bad sound must not abort a bulk run; the traceback keeps
                # the unexpected failure visible to the caller's logs.
                log.exception("unexpected failure downloading %s", button.id)
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, button.title or button.id)

        async with asyncio.TaskGroup() as tasks:
            for index, button in enumerate(unique):
                tasks.create_task(run(index, button))

        return [path for path in results if path is not None]

    def _target_path(self, button: InstantButton) -> Path:
        """Return the file the button downloads to, with a Windows-safe name."""

        suffix = f"-{button.id}.mp3"
        stem = sanitize_filename(button.title, fallback=button.id, max_length=MAX_FILENAME_LENGTH)
        # one character of headroom keeps the whole name strictly under the cap
        stem = stem[: max(MAX_FILENAME_LENGTH - len(suffix) - 1, 1)].rstrip(" .")
        if not stem:
            stem = sanitize_filename(button.id, fallback="sound", max_length=MAX_FILENAME_LENGTH)
        return self.storage.category_dir(button.category) / f"{stem}{suffix}"

    async def _transfer(self, button: InstantButton, target: Path) -> None:
        """Stream ``button.audio_url`` to ``target`` via a ``.part`` file.

        The partial file is removed on every failure path, and the final rename
        happens only after the last chunk is on disk.
        """

        part = target.with_suffix(target.suffix + ".part")
        try:
            for attempt in range(len(RETRY_DELAYS) + 1):
                try:
                    await self._stream_to(button.audio_url, part)
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    if attempt >= len(RETRY_DELAYS) or not _is_retryable(error):
                        raise
                    part.unlink(missing_ok=True)
                    await asyncio.sleep(RETRY_DELAYS[attempt])
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    async def _stream_to(self, url: str, part: Path) -> None:
        """Write the response body at ``url`` to ``part`` chunk by chunk."""

        async with self.client.stream("GET", url) as response:
            response.raise_for_status()
            with part.open("wb") as handle:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    handle.write(chunk)


def _is_retryable(error: httpx.HTTPError) -> bool:
    """429, 5xx, and transport failures are transient; other statuses are final."""

    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code == 429 or 500 <= status_code <= 599
    return isinstance(error, httpx.TransportError)
