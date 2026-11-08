"""Local web studio: a soundboard, a visual downloader, and a Remotion matcher.

The server is Python's standard :mod:`http.server` on ``127.0.0.1`` only, so
``run.py --web`` needs no Node, no build step, and no extra dependency. It
reuses the CLI's own layers — :class:`~remosound.scraper.MyInstantsScraper`,
:class:`~remosound.downloader.BulkDownloader`,
:class:`~remosound.storage.StorageManager`, and
:class:`~remosound.matcher.ContextualSoundMatcher` — so the ethics limits,
manifest dedupe, and scoring rules are the same on both surfaces.

Endpoints:

============================  ===============================================
``GET /``                     the single-page studio
``GET /api/bank``             manifest sounds, category counts, categories
``GET /api/audio?id=<id>``    stream one bank file (HTTP Range supported)
``GET /api/jobs/<id>``        progress snapshot for a download job
``POST /api/download``        start a batch download job
``POST /api/match``           rank the bank against scene text
============================  ===============================================
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlparse

from rich.console import Console

from remosound import __version__
from remosound.downloader import MAX_CONCURRENCY, BulkDownloader
from remosound.matcher import ContextualSoundMatcher
from remosound.remotion_adapter import REMOTION_PREFIX
from remosound.scraper import CATEGORIES, MIXKIT_CATEGORIES, InstantButton, MixkitScraper, MyInstantsScraper
from remosound.storage import StorageManager, category_slug

HOST = "127.0.0.1"
DEFAULT_PORT = 5000
PORT_ATTEMPTS = 10

MAX_LIMIT = 5000
MAX_MATCH_RESULTS = 8
MAX_BODY_BYTES = 1_000_000
JOB_HISTORY = 20
CHUNK_SIZE = 64 * 1024

WEB_DIR = Path(__file__).with_name("web")
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
INDEX_NAME = "index.html"

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


# --- jobs ------------------------------------------------------------------


class DownloadJob:
    """Thread-safe progress record for one background download."""

    def __init__(self, mode: str, target: str, limit: int, provider: str = "myinstants") -> None:
        self.id = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._data: dict = {
            "id": self.id,
            "mode": mode,
            "target": target,
            "limit": limit,
            "provider": provider,
            "state": "queued",
            "found": 0,
            "total": 0,
            "completed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "current": "",
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def update(self, **fields: object) -> None:
        with self._lock:
            self._data.update(fields)

    def on_progress(self, completed: int, total: int, title: str) -> None:
        self.update(completed=completed, total=total, current=title or "")

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


class JobRegistry:
    """Keep recent jobs addressable by id while bounding memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, DownloadJob] = {}

    def add(self, job: DownloadJob) -> DownloadJob:
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > JOB_HISTORY:
                self._jobs.pop(next(iter(self._jobs)))
        return job

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)


def start_download(
    registry: JobRegistry,
    mode: str,
    target: str,
    limit: int,
    out: Path,
    provider: str = "myinstants",
) -> DownloadJob:
    """Register a job and run it on a daemon thread.

    The caller gets its snapshot immediately; the browser polls
    ``/api/jobs/<id>`` for progress.
    """

    job = registry.add(DownloadJob(mode=mode, target=target, limit=limit, provider=provider))
    thread = threading.Thread(
        target=_run_job,
        args=(job, mode, target, limit, Path(out), provider),
        name=f"remosound-{job.id}",
        daemon=True,
    )
    thread.start()
    return job


def _run_job(job: DownloadJob, mode: str, target: str, limit: int, out: Path, provider: str = "myinstants") -> None:
    """Own the job's event loop and turn every failure into job state."""

    job.update(state="running", started_at=time.time())
    try:
        asyncio.run(_download_async(job, mode, target, limit, out, provider))
    except Exception as error:  # noqa: BLE001 - surfaced to the browser
        job.update(state="error", error=str(error), finished_at=time.time())


async def _download_async(
    job: DownloadJob, mode: str, target: str, limit: int, out: Path, provider: str = "myinstants"
) -> None:
    """Scrape one listing, then download what the manifest does not already own."""

    storage = StorageManager(out)
    scraper = MixkitScraper() if provider == "mixkit" else MyInstantsScraper()
    try:
        buttons = await _collect(scraper, mode, target, limit)
    finally:
        await scraper.close()

    unique = list({button.id: button for button in buttons}.values())
    pending = [button for button in unique if not storage.is_downloaded(button.id)]
    job.update(found=len(unique), total=len(pending), skipped=len(unique) - len(pending))

    if not pending:
        job.update(state="done", finished_at=time.time())
        return

    downloader = BulkDownloader(storage, max_concurrency=MAX_CONCURRENCY)
    try:
        await downloader.download_batch(pending, on_progress=job.on_progress)
    finally:
        await downloader.close()

    failed = [button for button in pending if not storage.is_downloaded(button.id)]
    job.update(
        downloaded=len(pending) - len(failed),
        failed=len(failed),
        current="",
        state="done",
        finished_at=time.time(),
    )


async def _collect(
    scraper: MyInstantsScraper | MixkitScraper, mode: str, target: str, limit: int
) -> list[InstantButton]:
    """Crawl the listing named by ``mode`` and ``target``."""

    if mode == "trending":
        return await scraper.crawl_trending(limit)
    if mode == "category":
        return await scraper.crawl_category(target.strip(), limit)
    if mode == "search":
        return await scraper.crawl_search(target.strip(), limit)
    raise ValueError(f"unknown download mode: {mode!r}")


# --- payloads --------------------------------------------------------------


def bank_payload(storage: StorageManager) -> dict:
    """The whole bank in the shape the studio's soundboard consumes."""

    sounds = []
    counts: dict[str, int] = {}
    for entry in storage.entries():
        category = str(entry.get("category") or "uncategorized")
        sound = _sound_payload(entry)
        sounds.append(sound)
        counts[category] = counts.get(category, 0) + 1

    sounds.sort(key=lambda sound: (sound["category"].casefold(), sound["title"].casefold(), sound["id"]))
    categories = [
        {"name": name, "count": counts[name]} for name in sorted(counts, key=str.casefold)
    ]
    return {
        "total": len(sounds),
        "sounds": sounds,
        "categories": categories,
        "available": [{"name": label, "slug": slug} for label, slug in CATEGORIES],
        "providers": [
            {
                "id": "myinstants",
                "name": "MyInstants",
                "badge": "Community",
                "categories": [{"name": label, "slug": slug} for label, slug in CATEGORIES],
            },
            {
                "id": "mixkit",
                "name": "Mixkit",
                "badge": "Royalty-Free",
                "categories": [{"name": label, "slug": slug} for label, slug in MIXKIT_CATEGORIES],
            },
        ],
    }


def match_payload(storage: StorageManager, text: str, limit: int = MAX_MATCH_RESULTS) -> dict:
    """Rank the bank against ``text`` and add browser play + Remotion fields."""

    entries = {str(entry.get("id")): entry for entry in storage.entries()}
    matches = ContextualSoundMatcher(storage.entries()).match(text, top_k=limit)
    for match in matches:
        match.update(_sound_payload(entries.get(str(match["id"]), match)))
    return {"context": text, "matches": matches}


def _sound_payload(entry: dict) -> dict:
    """One sound as the studio needs it: identity, bank path, play URL, Remotion src."""

    instant_id = str(entry.get("id") or "")
    title = str(entry.get("title") or instant_id)
    category = str(entry.get("category") or "uncategorized")
    provider = str(entry.get("provider") or "myinstants")
    path = str(entry.get("path") or "").replace("\\", "/")
    file_name = PurePosixPath(path).name
    return {
        "id": instant_id,
        "title": title,
        "category": category,
        "provider": provider,
        "path": path,
        "src": f"{REMOTION_PREFIX}/{category_slug(category)}/{file_name}",
        "url": "/api/audio?id=" + quote(instant_id, safe=""),
    }


def audio_path(storage: StorageManager, instant_id: str) -> Path | None:
    """Resolve a manifest id to a file inside the bank, or ``None``.

    The browser only ever names an id; the path comes from the manifest and is
    checked against the bank root, so a crafted id or manifest entry cannot
    read a file outside ``downloads/``.
    """

    entry = storage.manifest.get(instant_id)
    if entry is None:
        return None

    root = storage.root.resolve()
    candidate = (root / str(entry.get("path") or "")).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Return inclusive ``(start, end)`` for one ``bytes=`` range, or ``None``.

    Only the single-range form browsers send is supported. An absent or
    malformed header, an unsatisfiable start, or a zero-size file returns
    ``None`` so the caller can answer 200 or 416.
    """

    match = _RANGE.match((header or "").strip())
    if match is None or size <= 0:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None

    if not start_text:
        length = int(end_text)
        if length <= 0:
            return None
        start = max(size - length, 0)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1

    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def clamp_limit(value: object) -> int:
    """Coerce a browser-supplied limit into ``1..5000``."""

    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 100
    return max(1, min(MAX_LIMIT, number))


# --- HTTP ------------------------------------------------------------------


class RemoSoundServer(ThreadingHTTPServer):
    """Threaded server carrying the bank root and the job registry."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path) -> None:
        super().__init__(address, _Handler)
        self.root = Path(root)
        self.jobs = JobRegistry()

    def storage(self) -> StorageManager:
        return StorageManager(self.root)


class _Handler(BaseHTTPRequestHandler):
    server: RemoSoundServer
    protocol_version = "HTTP/1.1"

    # --- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path)
        try:
            if route.path in ("/", "/index.html"):
                self._serve_index()
            elif route.path == "/api/bank":
                self._send_json(bank_payload(self.server.storage()))
            elif route.path == "/api/health":
                self._send_json({"ok": True, "version": __version__})
            elif route.path == "/api/audio":
                self._serve_audio(parse_qs(route.query).get("id", [""])[0])
            elif route.path.startswith("/api/jobs/"):
                self._serve_job(route.path.rsplit("/", 1)[-1])
            elif route.path.startswith("/assets/"):
                self._serve_asset(route.path[len("/assets/") :])
            elif route.path in ("/favicon.ico", "/favicon.svg"):
                self._send_empty(HTTPStatus.NO_CONTENT)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            # The browser aborted a stream (skipped playback, closed tab).
            return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path)
        try:
            payload = self._read_json()
            if payload is None:
                return
            if route.path == "/api/download":
                self._start_download(payload)
            elif route.path == "/api/delete":
                self._delete_sound(payload)
            elif route.path == "/api/match":
                self._match(payload)
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: object) -> None:
        """Keep the CLI clean; the studio reports its own errors in the UI."""

    # --- responses ---------------------------------------------------------

    def _serve_index(self) -> None:
        index = WEB_DIR / INDEX_NAME
        try:
            body = index.read_bytes()
        except OSError as error:
            self._send_json(
                {"error": f"studio page is missing: {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_bytes(body, "text/html; charset=utf-8")

    def _serve_audio(self, instant_id: str) -> None:
        path = audio_path(self.server.storage(), instant_id)
        if path is None:
            self._send_json({"error": "sound not found in the bank"}, HTTPStatus.NOT_FOUND)
            return

        size = path.stat().st_size
        header = self.headers.get("Range", "")
        span = parse_range(header, size)
        if header and span is None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = span if span is not None else (0, size - 1)
        length = 0 if size == 0 else end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if span is not None else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(length))
        if span is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_job(self, job_id: str) -> None:
        job = self.server.jobs.get(job_id)
        if job is None:
            self._send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"job": job.snapshot()})

    def _start_download(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "trending").strip()
        target = str(payload.get("target") or "").strip()
        if mode not in ("trending", "category", "search"):
            self._send_json({"error": f"unknown mode: {mode}"}, HTTPStatus.BAD_REQUEST)
            return
        if mode != "trending" and not target:
            self._send_json({"error": "a category or search query is required"}, HTTPStatus.BAD_REQUEST)
            return

        provider = str(payload.get("provider") or "myinstants").strip()
        if provider not in ("myinstants", "mixkit"):
            provider = "myinstants"

        job = start_download(
            self.server.jobs,
            mode=mode,
            target=target,
            limit=clamp_limit(payload.get("limit")),
            out=self.server.root,
            provider=provider,
        )
        self._send_json({"job": job.snapshot()}, HTTPStatus.ACCEPTED)

    def _delete_sound(self, payload: dict) -> None:
        raw_ids = payload.get("ids")
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                self._send_json({"error": "ids must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        else:
            single = str(payload.get("id") or "").strip()
            ids = [single] if single else []

        if not ids:
            self._send_json({"error": "sound id or ids required"}, HTTPStatus.BAD_REQUEST)
            return

        storage = self.server.storage()
        deleted = storage.delete_sounds(ids)
        if not deleted:
            self._send_json({"error": "sound not found in the bank"}, HTTPStatus.NOT_FOUND)
            return

        self._send_json({"ok": True, "deleted": deleted, "id": deleted[0] if len(deleted) == 1 else None})

    def _serve_asset(self, raw_path: str) -> None:
        rel_path = unquote(raw_path).lstrip("/\\")
        candidate = (ASSETS_DIR / rel_path).resolve()
        if candidate != ASSETS_DIR and ASSETS_DIR not in candidate.parents:
            self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self._send_json({"error": "asset not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            body = candidate.read_bytes()
        except OSError as error:
            self._send_json({"error": f"could not read asset: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send_bytes(body, content_type)

    def _match(self, payload: dict) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            self._send_json({"error": "scene text is required"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(match_payload(self.server.storage(), text))

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json({"error": "bad content length"}, HTTPStatus.BAD_REQUEST)
            return None
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "request body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json({"error": "body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return payload

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


# --- entry point -----------------------------------------------------------


def serve(
    out: Path,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    console: Console | None = None,
) -> None:
    """Serve the studio until Ctrl+C, choosing the first free port."""

    server = _bind(Path(out), port)
    url = f"http://{HOST}:{server.server_address[1]}/"
    message = f"RemoSound studio running at [bold green]{url}[/bold green] (Ctrl+C to stop)"
    (console or Console()).print(message)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _bind(root: Path, port: int) -> RemoSoundServer:
    """Bind the first free port in ``port..port+9``, or report the last error."""

    last_error: OSError | None = None
    for candidate in range(port, port + PORT_ATTEMPTS):
        try:
            return RemoSoundServer((HOST, candidate), root)
        except OSError as error:
            last_error = error
    raise OSError(
        f"no free port in {port}-{port + PORT_ATTEMPTS - 1} for the web studio ({last_error})"
    )
