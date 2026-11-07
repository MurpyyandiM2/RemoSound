"""Tests for Windows-safe filenames and the download manifest."""

import json

from remosound.storage import StorageManager, sanitize_filename

FORBIDDEN_CHARS = set('\\/:*?"<>|')


def test_sanitize_filename_removes_forbidden_characters():
    result = sanitize_filename('What? Are: You | Kidding*/\\"<>')

    assert result == "What Are You Kidding"
    assert not (set(result) & FORBIDDEN_CHARS)
    assert all(ord(char) >= 32 for char in result)


def test_sanitize_filename_strips_control_characters():
    result = sanitize_filename("boom\x00\x1f sound")

    assert result == "boom sound"


def test_sanitize_filename_falls_back_when_nothing_usable_remains():
    assert sanitize_filename("   ...   ") == "sound"
    assert sanitize_filename("   ...   ", fallback="boom") == "boom"


def test_sanitize_filename_truncates_to_max_length():
    result = sanitize_filename("x" * 300, max_length=100)

    assert len(result) <= 100
    assert result == "x" * 100


def test_sanitize_filename_does_not_end_with_dot_or_space_after_truncation():
    # truncation lands exactly on the separator, which Windows would strip
    assert sanitize_filename("abcde fg", max_length=6) == "abcde"
    assert sanitize_filename("abcdef.gh", max_length=7) == "abcdef"


def test_sanitize_filename_keeps_emoji_titles_usable():
    result = sanitize_filename("boom 💥 sound")

    assert result == "boom 💥 sound"
    assert not (set(result) & FORBIDDEN_CHARS)


def test_manifest_round_trip(tmp_path):
    root = tmp_path / "downloads"
    storage = StorageManager(root)

    assert storage.manifest == {}
    assert storage.is_downloaded("abc123") is False

    storage.record_download(
        instant_id="abc123",
        title="Boom",
        category="Sound Effects",
        relative_path="sound-effects/boom.mp3",
        tags=["boom", "impact"],
    )
    storage.save()

    manifest_path = root / "manifest.json"
    assert manifest_path.is_file()
    assert not (root / "manifest.json.tmp").exists()

    reloaded = StorageManager(root)
    assert reloaded.is_downloaded("abc123") is True
    assert reloaded.entries() == [
        {
            "id": "abc123",
            "title": "Boom",
            "category": "Sound Effects",
            "path": "sound-effects/boom.mp3",
            "tags": ["boom", "impact"],
        }
    ]
    assert reloaded.manifest["abc123"]["title"] == "Boom"


def test_record_download_twice_does_not_duplicate_entries(tmp_path):
    storage = StorageManager(tmp_path / "downloads")

    storage.record_download("abc", "One", "Cat", "cat/one.mp3", [])
    storage.record_download("abc", "Two", "Cat", "cat/two.mp3", ["updated"])

    assert len(storage.entries()) == 1
    entry = storage.entries()[0]
    assert entry["id"] == "abc"
    assert entry["title"] == "Two"
    assert entry["path"] == "cat/two.mp3"
    assert entry["tags"] == ["updated"]


def test_category_dir_is_sanitized_lowercased_and_created(tmp_path):
    root = tmp_path / "downloads"
    storage = StorageManager(root)

    directory = storage.category_dir("Sound Effects")

    assert directory == root / "sound-effects"
    assert directory.is_dir()


def test_category_dir_sanitizes_awkward_names(tmp_path):
    storage = StorageManager(tmp_path / "downloads")

    directory = storage.category_dir("Meme: Compilation?")

    assert directory.name == "meme-compilation"
    assert directory.is_dir()


def test_missing_manifest_starts_empty(tmp_path):
    storage = StorageManager(tmp_path / "does-not-exist")

    assert storage.manifest == {}
    assert storage.entries() == []


def test_corrupt_manifest_starts_empty_and_can_be_rewritten(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{ this is not json", encoding="utf-8")

    storage = StorageManager(root)
    assert storage.manifest == {}

    storage.record_download("abc", "Boom", "Cat", "cat/boom.mp3", ["boom"])
    storage.save()

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "abc": {
            "id": "abc",
            "title": "Boom",
            "category": "Cat",
            "path": "cat/boom.mp3",
            "tags": ["boom"],
        }
    }


def test_manifest_ignores_non_object_payload(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "manifest.json").write_text('["not", "a", "mapping"]', encoding="utf-8")

    storage = StorageManager(root)

    assert storage.manifest == {}
    assert storage.entries() == []
