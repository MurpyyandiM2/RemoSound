"""Manifest-backed storage for the local sound bank.

A :class:`StorageManager` owns ``<root>/manifest.json`` — the dedupe record
that keeps already-downloaded sounds from being fetched again — and creates
the sanitized category directories the downloader writes into.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Characters Windows forbids in filenames, plus ASCII control characters.
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def sanitize_filename(title: str, fallback: str = "sound", max_length: int = 100) -> str:
    """Return a filename stem that is safe to write on Windows.

    Forbidden and control characters are removed, whitespace is collapsed,
    trailing dots and spaces are stripped (Windows silently drops them), and
    the result is truncated to ``max_length``. When nothing usable remains the
    sanitized ``fallback`` is used instead.
    """

    cleaned = _clean(title, max_length)
    if cleaned:
        return cleaned
    return _clean(fallback, max_length) or "sound"


def _clean(value: str, max_length: int) -> str:
    cleaned = _FORBIDDEN_CHARS.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned[: max(max_length, 0)].rstrip(" .")


class StorageManager:
    """Own the download manifest and category directories under ``root``."""

    MANIFEST_NAME = "manifest.json"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest: dict[str, dict] = self._load()

    @property
    def manifest_path(self) -> Path:
        return self.root / self.MANIFEST_NAME

    def is_downloaded(self, instant_id: str) -> bool:
        return instant_id in self.manifest

    def record_download(
        self,
        instant_id: str,
        title: str,
        category: str,
        relative_path: str,
        tags: list[str],
    ) -> None:
        """Store (or overwrite) the manifest entry for ``instant_id``."""

        self.manifest[instant_id] = {
            "id": instant_id,
            "title": title,
            "category": category,
            "path": relative_path,
            "tags": list(tags),
        }

    def category_dir(self, category: str) -> Path:
        """Return the category directory, creating it if needed.

        The name is sanitized for Windows, lowercased, and has whitespace
        replaced with dashes (``"Sound Effects"`` -> ``sound-effects``).
        """

        slug = _WHITESPACE.sub("-", sanitize_filename(category, fallback="uncategorized")).lower()
        directory = self.root / slug
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save(self) -> None:
        """Persist the manifest atomically via a temp file and ``os.replace``."""

        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = self.manifest_path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, self.manifest_path)

    def entries(self) -> list[dict]:
        """Return the manifest records as plain dicts."""

        return [dict(entry, tags=list(entry["tags"])) for entry in self.manifest.values()]

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}

        manifest: dict[str, dict] = {}
        for instant_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tags")
            manifest[str(instant_id)] = {
                "id": entry.get("id", instant_id),
                "title": entry.get("title", ""),
                "category": entry.get("category", ""),
                "path": entry.get("path", ""),
                "tags": list(tags) if isinstance(tags, list) else [],
            }
        return manifest
