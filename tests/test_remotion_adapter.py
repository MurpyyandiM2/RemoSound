"""Tests for the Remotion registry and TypeScript helper adapters."""

import json

import pytest

from remosound.remotion_adapter import (
    export_registry,
    export_typescript_helper,
    stage_into_remotion,
)
from remosound.storage import StorageManager


def _entry(instant_id, title, category, tags=(), path=None):
    return {
        "id": instant_id,
        "title": title,
        "category": category,
        "path": path if path is not None else f"{category}/{title}-{instant_id}.mp3",
        "tags": list(tags),
    }


@pytest.fixture()
def entries():
    # A Windows-style bank-relative path: the adapter has to normalize it.
    return [_entry("abc123", "Vine Boom", "sfx", ["boom"], path="sfx\\Vine Boom-abc123.mp3")]


# --- export_registry -------------------------------------------------------


def test_export_registry_writes_versioned_json(tmp_path, entries):
    output = tmp_path / "sound-bank.json"

    result = export_registry(entries, output)

    assert result == output
    registry = json.loads(output.read_text(encoding="utf-8"))
    assert registry["version"] == 1
    assert registry["sounds"] == [
        {
            "id": "abc123",
            "title": "Vine Boom",
            "category": "sfx",
            "file": "Vine Boom-abc123.mp3",
            "src": "remosound/sfx/Vine Boom-abc123.mp3",
        }
    ]


def test_export_registry_uses_forward_slashes_on_every_platform(tmp_path, entries):
    output = tmp_path / "sound-bank.json"

    export_registry(entries, output)

    registry = json.loads(output.read_text(encoding="utf-8"))
    src = registry["sounds"][0]["src"]
    assert src == "remosound/sfx/Vine Boom-abc123.mp3"
    assert "\\" not in src


def test_registry_src_uses_the_same_category_dir_as_the_bank(tmp_path):
    # A multiword category must slug to the directory the downloader writes into.
    entries = [_entry("abc123", "Boom", "Sound Effects", path="Sound Effects/Boom-abc123.mp3")]
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    bank_dir = StorageManager(tmp_path / "downloads").category_dir("Sound Effects")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert bank_dir.name == "sound-effects"
    assert registry["sounds"][0]["src"] == f"remosound/{bank_dir.name}/Boom-abc123.mp3"


def test_export_registry_keeps_ids_sorted_for_stable_output(tmp_path):
    output = tmp_path / "sound-bank.json"
    entries = [
        _entry("b2", "Second", "sfx"),
        _entry("a1", "First", "sfx"),
    ]

    export_registry(entries, output)

    registry = json.loads(output.read_text(encoding="utf-8"))
    assert [sound["id"] for sound in registry["sounds"]] == ["a1", "b2"]


def test_export_registry_handles_an_empty_bank(tmp_path):
    output = tmp_path / "nested" / "sound-bank.json"

    export_registry([], output)

    registry = json.loads(output.read_text(encoding="utf-8"))
    assert registry == {"version": 1, "sounds": []}


def test_export_registry_leaves_no_temporary_file(tmp_path, entries):
    output = tmp_path / "sound-bank.json"

    export_registry(entries, output)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["sound-bank.json"]


def test_export_registry_keeps_the_previous_file_when_writing_fails(tmp_path):
    output = tmp_path / "sound-bank.json"
    export_registry([], output)
    before = output.read_text(encoding="utf-8")

    with pytest.raises(TypeError):
        export_registry(
            [{"id": {1, 2}, "title": "Bad", "category": "sfx", "path": "sfx/bad.mp3"}], output
        )

    assert output.read_text(encoding="utf-8") == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["sound-bank.json"]


# --- export_typescript_helper ---------------------------------------------


def test_export_typescript_helper_exports_the_public_api(tmp_path, entries):
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"

    result = export_typescript_helper(registry_path, output)

    assert result == output
    source = output.read_text(encoding="utf-8")
    assert "export type SoundEntry" in source
    assert "export const SOUND_BANK: SoundEntry[]" in source
    assert "export function getSound(" in source
    assert "export function findSounds(" in source
    assert "remosound/sfx/Vine Boom-abc123.mp3" in source


def test_typescript_helper_stays_dependency_free(tmp_path, entries):
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"

    export_typescript_helper(registry_path, output)

    source = output.read_text(encoding="utf-8")
    assert "import " not in source
    assert "require(" not in source


def test_generated_header_points_at_a_real_command(tmp_path, entries):
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"

    export_typescript_helper(registry_path, output)

    source = output.read_text(encoding="utf-8")
    assert "--rebuild-index" in source
    assert "--export-registry" not in source


def test_export_typescript_helper_handles_an_empty_bank(tmp_path):
    registry_path = export_registry([], tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"

    export_typescript_helper(registry_path, output)

    source = output.read_text(encoding="utf-8")
    assert "export const SOUND_BANK: SoundEntry[] = []" in source


def test_export_typescript_helper_replaces_an_existing_file_atomically(tmp_path, entries):
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"
    output.write_text("stale", encoding="utf-8")

    export_typescript_helper(registry_path, output)

    assert "export const SOUND_BANK" in output.read_text(encoding="utf-8")
    assert "stale" not in output.read_text(encoding="utf-8")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["sound-bank.json", "sound-bank.ts"]


# --- registry validation ---------------------------------------------------


def test_consumers_reject_a_payload_that_is_not_an_object(tmp_path):
    registry_path = tmp_path / "sound-bank.json"
    registry_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a JSON object"):
        export_typescript_helper(registry_path, tmp_path / "sound-bank.ts")

    with pytest.raises(ValueError, match="must be a JSON object"):
        stage_into_remotion(registry_path, tmp_path / "downloads", tmp_path / "public")


def test_consumers_reject_an_unknown_registry_version(tmp_path, entries):
    registry_path = tmp_path / "sound-bank.json"
    registry = json.loads(export_registry(entries, registry_path).read_text(encoding="utf-8"))
    registry["version"] = 2
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported version"):
        export_typescript_helper(registry_path, tmp_path / "sound-bank.ts")

    with pytest.raises(ValueError, match="unsupported version"):
        stage_into_remotion(registry_path, tmp_path / "downloads", tmp_path / "public")


# --- stage_into_remotion ---------------------------------------------------


def test_stage_into_remotion_copies_audio_and_registry(tmp_path, entries):
    sound_root = tmp_path / "downloads"
    audio = sound_root / "sfx" / "Vine Boom-abc123.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-audio")
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    public_dir = tmp_path / "remotion" / "public"

    result = stage_into_remotion(registry_path, sound_root, public_dir)

    assert result == public_dir / "sound-bank.json"
    assert (public_dir / "remosound" / "sfx" / "Vine Boom-abc123.mp3").read_bytes() == b"ID3-audio"
    staged = json.loads((public_dir / "sound-bank.json").read_text(encoding="utf-8"))
    assert staged["sounds"][0]["src"] == "remosound/sfx/Vine Boom-abc123.mp3"


def test_staging_replaces_the_registry_atomically(tmp_path, entries):
    sound_root = tmp_path / "downloads"
    audio = sound_root / "sfx" / "Vine Boom-abc123.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-audio")
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "sound-bank.json").write_text("stale", encoding="utf-8")

    stage_into_remotion(registry_path, sound_root, public_dir)

    staged = json.loads((public_dir / "sound-bank.json").read_text(encoding="utf-8"))
    assert staged["sounds"][0]["id"] == "abc123"
    assert sorted(path.name for path in public_dir.iterdir()) == ["remosound", "sound-bank.json"]


def test_stage_into_remotion_reports_a_missing_source(tmp_path, entries):
    registry_path = export_registry(entries, tmp_path / "sound-bank.json")

    with pytest.raises(FileNotFoundError):
        stage_into_remotion(registry_path, tmp_path / "downloads", tmp_path / "public")


def test_stage_into_remotion_rejects_a_src_that_escapes_the_public_dir(tmp_path, entries):
    sound_root = tmp_path / "downloads"
    (sound_root / "sfx").mkdir(parents=True)
    (tmp_path / "evil.mp3").write_bytes(b"nope")
    registry_path = tmp_path / "sound-bank.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sounds": [
                    {
                        "id": "evil",
                        "title": "Evil",
                        "category": "sfx",
                        "file": "evil.mp3",
                        "src": "remosound/../../evil.mp3",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        stage_into_remotion(registry_path, sound_root, tmp_path / "public")
