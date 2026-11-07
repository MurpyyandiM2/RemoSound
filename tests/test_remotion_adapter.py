"""Tests for the Remotion registry and TypeScript helper adapters."""

import json

import pytest

from remosound.remotion_adapter import (
    export_registry,
    export_typescript_helper,
    stage_into_remotion,
)


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


def test_export_typescript_helper_handles_an_empty_bank(tmp_path):
    registry_path = export_registry([], tmp_path / "sound-bank.json")
    output = tmp_path / "sound-bank.ts"

    export_typescript_helper(registry_path, output)

    source = output.read_text(encoding="utf-8")
    assert "export const SOUND_BANK: SoundEntry[] = []" in source


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
