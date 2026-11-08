"""Manifest-backed storage for the local sound bank.

A :class:`StorageManager` owns ``<root>/manifest.json`` — the dedupe record
that keeps already-downloaded sounds from being fetched again — and creates
the sanitized category directories the downloader writes into.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
from pathlib import Path

# Characters Windows forbids in filenames, plus ASCII control characters.
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# Names Windows reserves for DOS devices. A file stem matching one of these is
# unusable even when an extension follows ("CON.mp3" opens the console device).
_RESERVED_DEVICE_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def sanitize_filename(title: str, fallback: str = "sound", max_length: int = 100) -> str:
    """Return a filename stem that is safe to write on Windows.

    Forbidden and control characters are removed, whitespace is collapsed,
    trailing dots and spaces are stripped (Windows silently drops them),
    reserved DOS device stems get a trailing ``_`` (``"CON"`` -> ``"CON_"``,
    ``"CON.mp3"`` -> ``"CON_.mp3"``), and the result is truncated to
    ``max_length``. When nothing usable remains the sanitized ``fallback`` is
    used instead.
    """

    cleaned = _clean(title, max_length)
    if cleaned:
        return cleaned
    return _clean(fallback, max_length) or "sound"


def _clean(value: str, max_length: int) -> str:
    cleaned = _FORBIDDEN_CHARS.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = cleaned[: max(max_length, 0)].rstrip(" .")
    return _avoid_reserved(cleaned, max_length)


def _avoid_reserved(name: str, max_length: int) -> str:
    stem, dot, extension = name.partition(".")
    if stem.strip().upper() not in _RESERVED_DEVICE_STEMS:
        return name

    extension = dot + extension
    # a trailing space before the dot is ignored by Win32, so trim it too
    stem = stem.rstrip(" .")
    # keep the guard inside max_length by trimming the stem, never the guard
    stem = stem[: max(max_length - len(extension) - 1, 0)]
    return f"{stem}_{extension}"[:max_length].rstrip(" .") or "_"


def category_slug(name: str) -> str:
    """Return the directory name used for a category.

    The name is sanitized for Windows, lowercased, and has whitespace replaced
    with dashes (``"Sound Effects"`` -> ``"sound-effects"``). The bank layout
    and the Remotion registry both derive from this, so a category always maps
    to one directory name.
    """

    return _WHITESPACE.sub("-", sanitize_filename(name, fallback="uncategorized")).lower()


class StorageManager:
    """Own the download manifest and category directories under ``root``."""

    MANIFEST_NAME = "manifest.json"
    CORRUPT_SUFFIX = ".corrupt"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest: dict[str, dict] = self._load()

    @property
    def manifest_path(self) -> Path:
        return self.root / self.MANIFEST_NAME

    @property
    def corrupt_manifest_path(self) -> Path:
        """Where an unreadable manifest is moved instead of being lost."""

        return self.manifest_path.with_name(self.MANIFEST_NAME + self.CORRUPT_SUFFIX)

    def is_downloaded(self, instant_id: str) -> bool:
        return instant_id in self.manifest

    def record_download(
        self,
        instant_id: str,
        title: str,
        category: str,
        relative_path: str,
        tags: list[str],
        provider: str = "myinstants",
    ) -> None:
        """Store (or overwrite) the manifest entry for ``instant_id``."""

        self.manifest[instant_id] = {
            "id": instant_id,
            "title": title,
            "category": category,
            "path": relative_path,
            "tags": list(tags),
            "provider": provider,
        }

    def delete_sound(self, instant_id: str) -> bool:
        """Remove a single sound from the bank and manifest, saving the manifest.

        Returns ``True`` if the sound was found and removed, ``False`` if not found.
        """

        deleted = self.delete_sounds([instant_id])
        return bool(deleted)

    def delete_sounds(self, instant_ids: list[str]) -> list[str]:
        """Remove multiple sounds from the bank and manifest, saving the manifest once.

        Returns the list of sound IDs that were successfully removed.
        """

        deleted: list[str] = []
        root_resolved = self.root.resolve()
        for instant_id in instant_ids:
            if instant_id not in self.manifest:
                continue
            entry = self.manifest.pop(instant_id)
            rel_path = entry.get("path")
            if rel_path:
                sound_file = (self.root / rel_path).resolve()
                if sound_file == root_resolved or root_resolved in sound_file.parents:
                    sound_file.unlink(missing_ok=True)
            deleted.append(instant_id)

        if deleted:
            self.save()
        return deleted

    def category_dir(self, category: str) -> Path:
        """Return the category directory, creating it if needed.

        The name is sanitized for Windows, lowercased, and has whitespace
        replaced with dashes (``"Sound Effects"`` -> ``sound-effects``).
        """

        slug = category_slug(category)
        directory = self.root / slug
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save(self) -> None:
        """Persist the manifest atomically via a unique temp file and ``os.replace``."""

        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=self.root, prefix="manifest.", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, self.manifest_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def entries(self) -> list[dict]:
        """Return the manifest records as plain dicts."""

        records = []
        for entry in self.manifest.values():
            tags = entry.get("tags")
            records.append(
                dict(
                    entry,
                    tags=list(tags) if isinstance(tags, (list, tuple)) else [],
                    provider=entry.get("provider", "myinstants"),
                )
            )
        return records

    def _load(self) -> dict[str, dict]:
        """Read the manifest, quarantining a file this class cannot trust.

        Dumping a corrupt manifest is not an option: the user's staged registry
        still refers to sounds it recorded, so the bad file is renamed to
        ``manifest.json.corrupt`` (and loudly warned about) before the empty
        manifest takes its place.
        """

        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self._quarantine(f"unreadable manifest ({error})")
            return {}

        if not isinstance(raw, dict):
            self._quarantine(f"manifest is a JSON {type(raw).__name__}, not an object")
            return {}

        manifest: dict[str, dict] = {}
        for instant_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tags")
            raw_id = entry.get("id", instant_id)
            manifest[str(instant_id)] = {
                "id": raw_id if isinstance(raw_id, str) else str(raw_id),
                "title": entry.get("title", ""),
                "category": entry.get("category", ""),
                "path": entry.get("path", ""),
                "tags": list(tags) if isinstance(tags, list) else [],
                "provider": entry.get("provider", "myinstants"),
            }
        return manifest

    def _quarantine(self, reason: str) -> None:
        """Move the unusable manifest aside and warn once, naming the new path."""

        quarantined = self.corrupt_manifest_path
        try:
            os.replace(self.manifest_path, quarantined)
        except OSError as error:
            warnings.warn(
                f"{reason}: could not move it to {quarantined} ({error}); "
                "the bank starts empty and the file is still at "
                f"{self.manifest_path}",
                stacklevel=3,
            )
            return
        warnings.warn(
            f"{reason}: moved to {quarantined}; the bank starts empty. "
            f"Check {quarantined.name} before rebuilding the registry.",
            stacklevel=3,
        )
