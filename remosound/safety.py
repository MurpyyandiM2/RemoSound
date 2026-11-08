"""The hard limits and URL checks that keep RemoSound polite and safe.

Everything a provider or a remote page could abuse is capped here, in one
place, below the scrapers and the downloader. A plugin source inherits these
limits and cannot opt out of them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

MAX_LIMIT = 250  # sounds collected per run
MAX_CONCURRENCY = 5  # downloads at once
MAX_FILE_BYTES = 50 * 1024 * 1024  # one audio file; a hostile page cannot fill the disk
MAX_REDIRECTS = 5
MIN_START_GAP = 0.2  # seconds between download starts, so five at once never bursts
DAILY_BUDGET = 3000  # downloads per day across every run and every bank


class UnsafeDownloadError(Exception):
    """A download was refused for safety: blocked address, oversized body, or budget."""


def home_dir() -> Path:
    return Path(os.environ.get("REMOSOUND_HOME") or Path.home() / ".remosound")


async def require_public_http_url(url: str) -> None:
    """Raise unless ``url`` is http(s) and every address it resolves to is public.

    An audio link comes from a remote page, so it must not be able to point
    RemoSound at the user's own machine or network (``127.0.0.1``, ``192.168.*``,
    cloud metadata at ``169.254.169.254``). Resolution happens again when the
    connection opens, so a hostile DNS server could still change its answer in
    between; this stops the direct cases, not a determined local attacker.
    """

    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeDownloadError(f"only http(s) links are allowed: {url}")
    try:
        addresses = await _resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as error:
        raise UnsafeDownloadError(f"could not resolve {parts.hostname}: {error}") from error
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeDownloadError(f"{parts.hostname} points to a non-public address ({address})")


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


class DailyBudget:
    """Count downloads per calendar day in one small file, shared by every run.

    It is a guard against a runaway loop, not a security boundary: anyone who
    controls this machine can delete the file.
    """

    def __init__(self, path: Path | None = None, limit: int = DAILY_BUDGET) -> None:
        self.path = Path(path) if path is not None else home_dir() / "budget.json"
        self.limit = limit

    def used(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict) or data.get("date") != date.today().isoformat():
            return 0
        count = data.get("count")
        return count if isinstance(count, int) and count > 0 else 0

    def exhausted(self) -> bool:
        return self.used() >= self.limit

    def spend(self, count: int = 1) -> None:
        payload = {"date": date.today().isoformat(), "count": self.used() + count}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            # A budget file we cannot write must not break a finished download.
            pass
