"""Tests for the local web studio.

Everything runs on an ephemeral loopback port with a manifest seeded in a
pytest ``tmp_path``; no test touches MyInstants.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from remosound import web as web_module
from remosound.remotion_adapter import write_index
from remosound.storage import StorageManager
from remosound.web import (
    RemoSoundServer,
    audio_path,
    bank_payload,
    clamp_limit,
    match_payload,
    parse_range,
)


def seed_bank(tmp_path, records):
    """Write a manifest with real files and ``(id, title, category)`` records."""

    bank = tmp_path / "downloads"
    storage = StorageManager(bank)
    for instant_id, title, category in records:
        directory = storage.category_dir(category)
        target = directory / f"{title}-{instant_id}.mp3"
        target.write_bytes(b"ID3" + instant_id.encode("ascii"))
        storage.record_download(
            instant_id=instant_id,
            title=title,
            category=category,
            relative_path=target.relative_to(bank).as_posix(),
            tags=[],
        )
    storage.save()
    return bank


# --- payloads --------------------------------------------------------------


def test_bank_payload_sorts_sounds_and_counts_categories(tmp_path):
    bank = seed_bank(
        tmp_path,
        [
            ("boom-1", "Vine Boom", "memes"),
            ("pop-1", "Pop Whoosh", "sound effects"),
            ("boom-2", "Boom Again", "memes"),
        ],
    )

    payload = bank_payload(StorageManager(bank))

    assert payload["total"] == 3
    assert [sound["title"] for sound in payload["sounds"]] == ["Boom Again", "Vine Boom", "Pop Whoosh"]
    assert payload["categories"] == [
        {"name": "memes", "count": 2},
        {"name": "sound effects", "count": 1},
    ]
    assert payload["available"][0]["slug"] == "transition"


def test_bank_payload_gives_every_sound_a_play_url_and_remotion_src(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "Sound Effects")])

    sound = bank_payload(StorageManager(bank))["sounds"][0]

    assert sound["url"] == "/api/audio?id=boom-1"
    assert sound["src"] == "remosound/sound-effects/Vine Boom-boom-1.mp3"
    assert sound["path"] == "sound-effects/Vine Boom-boom-1.mp3"


def test_match_payload_ranks_the_bank_and_adds_playback_fields(tmp_path):
    bank = seed_bank(
        tmp_path,
        [("boom-1", "Vine Boom", "memes"), ("pop-1", "Pop Whoosh", "memes")],
    )

    payload = match_payload(StorageManager(bank), "boom")

    assert payload["context"] == "boom"
    top = payload["matches"][0]
    assert top["id"] == "boom-1"
    assert top["url"] == "/api/audio?id=boom-1"
    assert top["src"].startswith("remosound/")


def test_match_payload_with_no_hits_is_empty_not_an_error(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes")])

    assert match_payload(StorageManager(bank), "zzzzz")["matches"] == []


# --- audio resolution ------------------------------------------------------


def test_audio_path_resolves_a_manifest_id_inside_the_bank(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes")])

    path = audio_path(StorageManager(bank), "boom-1")

    assert path is not None
    assert path.read_bytes() == b"ID3boom-1"


def test_audio_path_refuses_an_unknown_id(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes")])

    assert audio_path(StorageManager(bank), "missing") is None
    assert audio_path(StorageManager(bank), "../manifest.json") is None


def test_audio_path_refuses_a_manifest_entry_that_escapes_the_bank(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes")])
    storage = StorageManager(bank)
    storage.manifest["evil"] = {"id": "evil", "path": "../../etc/passwd"}
    storage.save()

    assert audio_path(StorageManager(bank), "evil") is None


# --- range parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=100-", 1000, (100, 999)),
        ("bytes=-100", 1000, (900, 999)),
        ("bytes=0-99999", 1000, (0, 999)),
        ("bytes=-9999", 1000, (0, 999)),
    ],
)
def test_parse_range_reads_a_single_browser_range(header, size, expected):
    assert parse_range(header, size) == expected


@pytest.mark.parametrize(
    ("header", "size"),
    [
        ("", 1000),
        ("bytes=", 1000),
        ("bytes=1000-", 1000),
        ("bytes=50-10", 1000),
        ("items=0-10", 1000),
        ("bytes=0-10", 0),
    ],
)
def test_parse_range_rejects_what_it_cannot_serve(header, size):
    assert parse_range(header, size) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 100), ("abc", 100), (0, 1), (-5, 1), (12, 12), (99999, 5000)],
)
def test_clamp_limit_keeps_requests_inside_the_ethics_cap(value, expected):
    assert clamp_limit(value) == expected


# --- HTTP integration ------------------------------------------------------


@pytest.fixture()
def studio(tmp_path):
    """A real server on an ephemeral loopback port, torn down after the test."""

    bank = seed_bank(
        tmp_path,
        [("boom-1", "Vine Boom", "memes"), ("pop-1", "Pop Whoosh", "memes")],
    )
    server = RemoSoundServer((web_module.HOST, 0), bank)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{web_module.HOST}:{server.server_address[1]}"
    try:
        yield base, bank
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_index_is_served_as_html(studio):
    base, _bank = studio

    status, headers, body = get(base + "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"RemoSound" in body
    assert b"/assets/app-icon.png" in body
    assert b"/assets/logo.png" in body


def test_bank_endpoint_matches_the_manifest(studio):
    base, _bank = studio

    status, _headers, body = get(base + "/api/bank")

    payload = json.loads(body)
    assert status == 200
    assert payload["total"] == 2
    assert {sound["id"] for sound in payload["sounds"]} == {"boom-1", "pop-1"}


def test_audio_endpoint_streams_the_whole_file(studio):
    base, _bank = studio

    status, headers, body = get(base + "/api/audio?id=boom-1")

    assert status == 200
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "audio/mpeg"
    assert body == b"ID3boom-1"


def test_audio_endpoint_answers_a_range_with_206(studio):
    base, _bank = studio

    status, headers, body = get(base + "/api/audio?id=boom-1", headers={"Range": "bytes=3-5"})

    assert status == 206
    assert headers["Content-Range"] == "bytes 3-5/9"
    assert body == b"boo"


def test_audio_endpoint_rejects_an_unsatisfiable_range(studio):
    base, _bank = studio

    with pytest.raises(urllib.error.HTTPError) as caught:
        get(base + "/api/audio?id=boom-1", headers={"Range": "bytes=99-"})

    assert caught.value.code == 416


def test_audio_endpoint_reports_a_missing_sound(studio):
    base, _bank = studio

    with pytest.raises(urllib.error.HTTPError) as caught:
        get(base + "/api/audio?id=missing")

    assert caught.value.code == 404


def test_match_endpoint_returns_ranked_matches(studio):
    base, _bank = studio

    status, payload = post(base + "/api/match", {"text": "boom"})

    assert status == 200
    assert payload["matches"][0]["id"] == "boom-1"


def test_match_endpoint_rejects_blank_text(studio):
    base, _bank = studio

    status, payload = post(base + "/api/match", {"text": "   "})

    assert status == 400
    assert "required" in payload["error"]


def test_download_endpoint_rejects_an_unknown_mode(studio):
    base, _bank = studio

    status, payload = post(base + "/api/download", {"mode": "everything"})

    assert status == 400
    assert "unknown mode" in payload["error"]


def test_download_endpoint_requires_a_target_for_a_search(studio):
    base, _bank = studio

    status, payload = post(base + "/api/download", {"mode": "search", "target": ""})

    assert status == 400
    assert "required" in payload["error"]


def test_job_endpoint_reports_an_unknown_job(studio):
    base, _bank = studio

    with pytest.raises(urllib.error.HTTPError) as caught:
        get(base + "/api/jobs/doesnotexist")

    assert caught.value.code == 404


def test_bank_payload_includes_provider_and_providers_list(tmp_path):
    bank = seed_bank(
        tmp_path,
        [("boom-1", "Vine Boom", "memes")],
    )
    storage = StorageManager(bank)
    storage.record_download(
        "mixkit-1", "Whoosh", "transition", "transition/whoosh.mp3", [], provider="mixkit"
    )
    storage.save()

    payload = bank_payload(storage)
    sounds_by_id = {s["id"]: s for s in payload["sounds"]}
    assert sounds_by_id["boom-1"]["provider"] == "myinstants"
    assert sounds_by_id["mixkit-1"]["provider"] == "mixkit"
    assert "providers" in payload
    provider_ids = [p["id"] for p in payload["providers"]]
    assert provider_ids == ["mixkit"]


def test_delete_endpoint_removes_sound(studio):
    base, bank = studio
    storage = StorageManager(bank)
    assert storage.is_downloaded("boom-1")

    status, payload = post(base + "/api/delete", {"id": "boom-1"})
    assert status == 200
    assert payload["ok"] is True
    assert payload["id"] == "boom-1"

    reloaded = StorageManager(bank)
    assert not reloaded.is_downloaded("boom-1")


def test_delete_endpoint_supports_bulk_ids(studio):
    base, bank = studio
    storage = StorageManager(bank)
    assert storage.is_downloaded("boom-1")
    assert storage.is_downloaded("pop-1")

    status, payload = post(base + "/api/delete", {"ids": ["boom-1", "pop-1"]})
    assert status == 200
    assert payload["ok"] is True
    assert set(payload["deleted"]) == {"boom-1", "pop-1"}

    reloaded = StorageManager(bank)
    assert not reloaded.is_downloaded("boom-1")
    assert not reloaded.is_downloaded("pop-1")



def test_delete_endpoint_reports_missing_sound(studio):
    base, _bank = studio
    status, payload = post(base + "/api/delete", {"id": "nonexistent-id"})
    assert status == 404
    assert "error" in payload


def test_download_endpoint_accepts_provider_mixkit(studio):
    base, _bank = studio
    status, payload = post(base + "/api/download", {"mode": "search", "target": "whoosh", "provider": "mixkit"})
    assert status == 202
    assert "job" in payload


def test_asset_serving_returns_the_brand_images(studio):
    base, _bank = studio

    for name in ("logo.png", "app-icon.png", "logo-banner.png"):
        status, headers, body = get(base + "/assets/" + name)
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body




# --- static files and project mode ------------------------------------------


def test_studio_css_and_js_are_served_with_their_own_types(studio):
    base, _bank = studio

    for name, kind in (("studio.css", "text/css"), ("studio.js", "text/javascript")):
        status, headers, body = get(base + "/static/" + name)
        assert status == 200
        assert headers["Content-Type"].startswith(kind)
        assert body

    _status, _headers, page = get(base + "/")
    assert b"/static/studio.css" in page
    assert b"/static/studio.js" in page


def test_the_static_route_cannot_leave_its_folder(studio):
    base, _bank = studio

    with pytest.raises(urllib.error.HTTPError) as refused:
        get(base + "/static/..%2Findex.html")

    assert refused.value.code == 403


def test_a_project_studio_keeps_the_index_in_step_with_deletes(tmp_path):
    bank = seed_bank(tmp_path, [("boom-1", "Vine Boom", "memes"), ("pop-1", "Pop Whoosh", "memes")])
    write_index(StorageManager(bank), bank)
    registry = bank.parent / "sound-bank.json"
    server = RemoSoundServer((web_module.HOST, 0), bank, refresh_index=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _payload = post(f"http://{web_module.HOST}:{server.server_address[1]}/api/delete", {"id": "boom-1"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    ids = [sound["id"] for sound in json.loads(registry.read_text(encoding="utf-8"))["sounds"]]
    assert ids == ["pop-1"]


# --- who may call the local API ---------------------------------------------


def raw_request(url, method="GET", headers=None, body=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_a_foreign_host_header_is_refused(studio):
    base, _bank = studio

    assert raw_request(base + "/api/bank", headers={"Host": "evil.example"}) == 403


def test_a_foreign_origin_cannot_start_a_download(studio):
    base, _bank = studio
    headers = {"Content-Type": "application/json", "Origin": "http://evil.example"}

    assert raw_request(base + "/api/download", "POST", headers, b'{"mode": "trending"}') == 403


def test_a_post_that_is_not_json_is_refused(studio):
    base, _bank = studio
    # A cross-site HTML form can send text/plain without a preflight; JSON it cannot.
    headers = {"Content-Type": "text/plain"}

    assert raw_request(base + "/api/download", "POST", headers, b'{"mode": "trending"}') == 415


def test_a_second_download_is_refused_while_one_runs(studio, monkeypatch):
    base, _bank = studio
    started = threading.Event()
    release = threading.Event()

    def slow_job(job, *args, **kwargs):
        job.update(state="running")
        started.set()
        release.wait(timeout=5)
        job.update(state="done")

    monkeypatch.setattr(web_module, "_run_job", slow_job)
    try:
        first_status, _ = post(base + "/api/download", {"mode": "trending"})
        assert started.wait(timeout=5)
        second_status, second = post(base + "/api/download", {"mode": "trending"})
    finally:
        release.set()

    assert first_status == 202
    assert second_status == 409
    assert "already running" in second["error"]
