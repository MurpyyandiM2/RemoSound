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
import hashlib
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx

from remosound.scraper import USER_AGENT, InstantButton
from remosound.storage import StorageManager, sanitize_filename

log = logging.getLogger(__name__)

MAX_CONCURRENCY = 5
MIN_CONCURRENCY = 1

# Tags derived from the title feed the matcher's tag dimension. They stay short
# and deduped so a single manifest entry cannot grow without bound.
MAX_TAGS = 12
MIN_TAG_LENGTH = 2

_TAG_TOKEN = re.compile(r"[a-z0-9]+")

# Windows refuses paths that leave too little room for the directory, so the
# generated name stays comfortably below the classic 260-character limit.
MAX_FILENAME_LENGTH = 150

# Backoff after each failed attempt; the last entry is the third retry.
RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

REQUEST_TIMEOUT = 20.0
CHUNK_SIZE = 64 * 1024

# A remote id is untrusted HTML content; it is sanitized before it becomes part
# of a filename, but kept verbatim as the manifest and dedupe key.
MAX_ID_LENGTH = 80


class EmptyDownloadError(Exception):
    """The server answered 200 but delivered no audio bytes."""


def derive_tags(title: str) -> list[str]:
    """Search tags implied by a sound's title, in title order.

    The matcher scores tags as well as tokens, and the downloader is the only
    producer of manifest records, so the tag dimension is filled here from the
    one field a listing actually gives us. Tokens are lowercased alphanumeric
    runs, single characters are dropped, duplicates collapse, and the list is
    capped at :data:`MAX_TAGS`.
    """

    tags: list[str] = []
    for token in _TAG_TOKEN.findall(title.lower()):
        if len(token) < MIN_TAG_LENGTH or token in tags:
            continue
        tags.append(token)
        if len(tags) == MAX_TAGS:
            break
    return tags


def _id_digest(instant_id: str) -> str:
    """Eight hex characters of the raw id's SHA-1, to keep filenames unique.

    Sanitization is lossy, so the sanitized id alone cannot distinguish two
    remote ids. The digest is deterministic and derived from the raw id, so the
    same sound always maps to the same filename.
    """

    return hashlib.sha1(instant_id.encode("utf-8")).hexdigest()[:8]


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
        self._id_locks: dict[str, asyncio.Lock] = {}
        self._id_lock_users: dict[str, int] = {}
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

        ``None`` means nothing was written: the id is already recorded (perhaps
        by a concurrent caller that held the same id lock) or the transfer kept
        failing. A file that already exists on disk but is missing from the
        manifest is adopted — recorded and returned — so an interrupted run
        never re-downloads it or reports it as a failure. The manifest is saved
        once here, so a standalone call is durable when it returns.
        """

        return await self._download_one(button, persist=True)

    async def _download_one(self, button: InstantButton, persist: bool) -> Path | None:
        """The shared download path; ``persist`` decides who writes the manifest."""

        if self.storage.is_downloaded(button.id):
            return None

        target = self._target_path(button)
        if target.exists():
            return self._adopt(button, target, persist)

        # One lock per instant id. A second caller for the same id waits here
        # instead of racing on the same target and ``.part`` paths, then finds
        # the manifest entry its predecessor wrote and returns None.
        lock = self._id_lock_for(button.id)
        try:
            async with lock, self.semaphore:
                if self.storage.is_downloaded(button.id):
                    return None
                if target.exists():
                    return self._adopt(button, target, persist)
                try:
                    await self._transfer(button, target)
                except (httpx.HTTPError, OSError, EmptyDownloadError) as error:
                    log.warning("could not download %s: %s", button.id, error)
                    return None

                # Mutate and persist without an intervening await, so concurrent
                # record/save pairs cannot lose one another's entries.
                self._record(button, target, persist)
                return target
        finally:
            self._release_id_lock(button.id)

    async def download_batch(
        self,
        buttons: list[InstantButton],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> list[Path]:
        """Download every button and return the paths that were written.

        Buttons are identified by instant id, so duplicates in ``buttons``
        collapse to a single download. Progress is reported once per unique
        button as ``on_progress(completed, total, title)``.

        Records accumulate in memory during the batch and the manifest is
        written exactly once at the end — a 5000-sound run would otherwise
        rewrite the whole file 5000 times. The save sits in a ``finally``, so a
        crash still flushes every record that already completed.
        """

        unique = list({button.id: button for button in buttons}.values())
        total = len(unique)
        results: list[Path | None] = [None] * total
        completed = 0

        async def run(index: int, button: InstantButton) -> None:
            nonlocal completed
            try:
                results[index] = await self._download_one(button, persist=False)
            except Exception:
                # One bad sound must not abort a bulk run; the traceback keeps
                # the unexpected failure visible to the caller's logs.
                log.exception("unexpected failure downloading %s", button.id)
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, button.title or button.id)

        try:
            async with asyncio.TaskGroup() as tasks:
                for index, button in enumerate(unique):
                    tasks.create_task(run(index, button))
        finally:
            self._save_manifest()

        return [path for path in results if path is not None]

    def _target_path(self, button: InstantButton) -> Path:
        """Return the file the button downloads to, with a Windows-safe name.

        The id is remote content, so it is sanitized before it reaches disk;
        ``button.id`` itself stays the manifest and dedupe key. Sanitizing is
        lossy — ``boom:1`` and ``boom1`` collapse to the same text, as do any
        two ids sharing their first :data:`MAX_ID_LENGTH` characters — so a
        short hash of the raw id keeps the name unique per sound. The hash is
        deterministic, so a resumed run derives the same path and adopts the
        file it already wrote.
        """

        safe_id = sanitize_filename(button.id, fallback="sound", max_length=MAX_ID_LENGTH)
        suffix = f"-{safe_id}-{_id_digest(button.id)}.mp3"
        # one character of headroom keeps the whole name strictly under the cap
        budget = max(MAX_FILENAME_LENGTH - len(suffix) - 1, 1)
        stem = sanitize_filename(button.title, fallback=safe_id, max_length=budget)
        stem = stem[:budget].rstrip(" .")
        if not stem:
            stem = safe_id[:budget].rstrip(" .") or "sound"
        return self.storage.category_dir(button.category) / f"{stem}{suffix}"

    def _adopt(self, button: InstantButton, target: Path, persist: bool) -> Path:
        """Record an existing target file whose id is missing from the manifest.

        An interrupted earlier run can leave audio on disk without a manifest
        record. Adopting it makes the next run treat it as owned instead of
        re-fetching it or reporting a phantom failure.
        """

        self._record(button, target, persist)
        return target

    def _record(self, button: InstantButton, target: Path, persist: bool) -> None:
        """Store ``target`` as the sound for ``button`` in the manifest.

        ``persist`` is false inside a batch, where :meth:`download_batch` owns
        the single save.
        """

        try:
            self.storage.record_download(
                instant_id=button.id,
                title=button.title,
                category=button.category,
                relative_path=target.relative_to(self.storage.root).as_posix(),
                tags=derive_tags(button.title),
                provider=getattr(button, "provider", "myinstants"),
            )
            if persist:
                self.storage.save()
        except OSError as error:
            # The audio is on disk; only the dedupe record is missing.
            log.warning("could not record %s in the manifest: %s", button.id, error)

    def _save_manifest(self) -> None:
        """Persist the manifest, logging instead of failing a finished batch."""

        try:
            self.storage.save()
        except OSError as error:
            log.warning("could not save the manifest: %s", error)

    def _id_lock_for(self, instant_id: str) -> asyncio.Lock:
        """Return the lock guarding ``instant_id``, counting a new user."""

        self._id_lock_users[instant_id] = self._id_lock_users.get(instant_id, 0) + 1
        lock = self._id_locks.get(instant_id)
        if lock is None:
            lock = self._id_locks[instant_id] = asyncio.Lock()
        return lock

    def _release_id_lock(self, instant_id: str) -> None:
        """Drop one user of an id lock, removing it when nobody is left."""

        remaining = self._id_lock_users.get(instant_id, 1) - 1
        if remaining > 0:
            self._id_lock_users[instant_id] = remaining
            return
        self._id_locks.pop(instant_id, None)
        self._id_lock_users.pop(instant_id, None)

    async def _transfer(self, button: InstantButton, target: Path) -> None:
        """Stream ``button.audio_url`` to ``target`` via a ``.part`` file.

        The partial file is removed on every failure path, an empty body is
        rejected before the rename, and the final rename happens only after the
        last chunk is on disk.
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
            if part.stat().st_size == 0:
                raise EmptyDownloadError(f"{button.audio_url} returned an empty body")
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
