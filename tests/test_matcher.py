"""Tests for the contextual scene-text sound matcher."""

import httpx
import pytest

from remosound.downloader import BulkDownloader
from remosound.matcher import CUE_MAP, ContextualSoundMatcher
from remosound.scraper import InstantButton
from remosound.storage import StorageManager

BASE_URL = "https://www.myinstants.com"


def _entry(instant_id, title, category, tags):
    return {
        "id": instant_id,
        "title": title,
        "category": category,
        "path": f"{category}/{title}-{instant_id}.mp3".replace(" ", "_"),
        "tags": tags,
    }


@pytest.fixture()
def entries():
    """A small bank spanning the cue families in ``CUE_MAP``."""

    records = [
        _entry("boom-1", "Vine Boom", "memes", ["boom", "explosion"]),
        _entry("win-1", "Win Fanfare", "sfx", ["victory", "applause"]),
        _entry("sad-1", "Sad Trombone", "sfx", ["fail", "wrong"]),
        _entry("click-1", "Click Button", "ui", ["click", "button"]),
        _entry("camera-1", "Camera Shutter", "sfx", ["camera", "photo"]),
        _entry("pop-1", "Pop Whoosh", "sfx", ["pop", "appear"]),
        _entry("scream-1", "Screamer Jump Scare", "scary", ["scream", "screamers", "jumpscare"]),
        _entry("coin-1", "Coin Pickup", "games", ["coin", "money"]),
    ]
    return records


def _titles(results):
    return [result["title"] for result in results]


# --- CUE_MAP contract ------------------------------------------------------


@pytest.mark.parametrize(
    ("cue", "expected_terms"),
    [
        ("muncul", ["pop", "appear", "whoosh"]),
        ("tiba-tiba", ["pop", "appear", "whoosh"]),
        ("pop", ["pop", "appear", "whoosh"]),
        ("kaget", ["scream", "screamers", "boom"]),
        ("scream", ["scream", "screamers", "boom"]),
        ("jumpscare", ["scream", "screamers", "boom"]),
        ("menang", ["victory", "fanfare", "applause", "win"]),
        ("juara", ["victory", "fanfare", "applause", "win"]),
        ("victory", ["victory", "fanfare", "applause", "win"]),
        ("gagal", ["fail", "sadtrombone", "bruh", "wrong"]),
        ("salah", ["fail", "sadtrombone", "bruh", "wrong"]),
        ("fail", ["fail", "sadtrombone", "bruh", "wrong"]),
        ("klik", ["click", "button", "tick"]),
        ("tombol", ["click", "button", "tick"]),
        ("click", ["click", "button", "tick"]),
        ("uang", ["coin", "cash", "money"]),
        ("koin", ["coin", "cash", "money"]),
        ("coin", ["coin", "cash", "money"]),
        ("explosion", ["explosion", "boom", "blast", "impact"]),
        ("ledakan", ["explosion", "boom", "blast", "impact"]),
        ("meledak", ["explosion", "boom", "blast", "impact"]),
        ("blast", ["explosion", "boom", "blast", "impact"]),
        ("boom", ["explosion", "boom", "blast", "impact"]),
    ],
)
def test_cue_map_covers_the_required_families(cue, expected_terms):
    assert CUE_MAP[cue] == expected_terms


# --- Cue-driven matching ---------------------------------------------------


def test_indonesian_cue_word_reaches_a_pop_or_boom_title(entries):
    matcher = ContextualSoundMatcher(entries)

    results = matcher.match("tiba-tiba muncul monster")

    assert results
    assert results[0]["id"] in {"pop-1", "boom-1"}
    title = results[0]["title"].lower()
    assert "pop" in title or "whoosh" in title or "boom" in title


def test_hyphenated_cue_also_matches_when_written_with_a_space(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match("tiba tiba ada monster")[0]["id"] in {"pop-1", "boom-1"}


def test_juara_matches_the_win_fanfare(entries):
    matcher = ContextualSoundMatcher(entries)

    assert _titles(matcher.match("kamu berhasil menjadi juara"))[:1] == ["Win Fanfare"]


def test_gagal_matches_the_sad_trombone(entries):
    matcher = ContextualSoundMatcher(entries)

    assert _titles(matcher.match("rencana tersebut gagal total"))[:1] == ["Sad Trombone"]


def test_kaget_matches_the_scream_family(entries):
    matcher = ContextualSoundMatcher(entries)

    ids = {result["id"] for result in matcher.match("kaget banget dong", top_k=3)}

    assert "scream-1" in ids
    assert ids <= {"scream-1", "boom-1"}


def test_jumpscare_cue_prefers_the_screamer(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match("ada jumpscare di akhir")[0]["id"] == "scream-1"


def test_klik_matches_the_click_button(entries):
    matcher = ContextualSoundMatcher(entries)

    assert _titles(matcher.match("selesai, klik tombol berikutnya"))[:1] == ["Click Button"]


def test_uang_matches_the_coin_pickup(entries):
    matcher = ContextualSoundMatcher(entries)

    assert _titles(matcher.match("dapat uang koin banyak"))[:1] == ["Coin Pickup"]


def test_english_cues_match_too(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match("a sudden pop")[0]["id"] in {"pop-1", "boom-1"}
    assert _titles(matcher.match("fail"))[:1] == ["Sad Trombone"]


def test_explosion_scene_reaches_a_vine_boom_title():
    """The README example must bridge to the boom family, not return nothing."""

    matcher = ContextualSoundMatcher([_entry("boom-1", "Vine Boom", "memes", [])])

    results = matcher.match("explosion in the distance, dust settling")

    assert [result["id"] for result in results] == ["boom-1"]
    assert results[0]["score"] == 3  # one title hit at TITLE_WEIGHT


def test_ledakan_cue_reaches_a_vine_boom_title():
    matcher = ContextualSoundMatcher([_entry("boom-1", "Vine Boom", "memes", [])])

    assert [result["id"] for result in matcher.match("ledakan besar")] == ["boom-1"]


# --- Compact matching ------------------------------------------------------


def test_compact_cue_term_matches_a_multi_word_title():
    matcher = ContextualSoundMatcher([_entry("sad-1", "Sad Trombone", "sfx", [])])

    # "gagal" expands to "sadtrombone", which is not a title token.
    results = matcher.match("rencana tersebut gagal total")

    assert [result["id"] for result in results] == ["sad-1"]
    assert results[0]["score"] == 3  # one title hit, at TITLE_WEIGHT


def test_compact_cue_term_matches_an_inflected_title():
    matcher = ContextualSoundMatcher([_entry("scream-1", "Screamer", "scary", [])])

    # "kaget" expands to "scream", which is not a token of "Screamer".
    results = matcher.match("kaget banget dong")

    assert [result["id"] for result in results] == ["scream-1"]
    assert results[0]["score"] == 3


def test_raw_scene_words_never_compact_match_a_title():
    """Only cue expansions bridge; short scene words must not hit by accident."""

    entries = [
        _entry("boom-1", "Vine Boom", "memes", []),
        _entry("coin-1", "Coin Pickup", "games", []),
        _entry("win-1", "Win Fanfare", "sfx", []),
    ]

    # "in" is a substring of "coinpickup" and "winfanfare". The query carries no
    # cue word, so nothing is allowed to bridge into those titles.
    assert ContextualSoundMatcher(entries).match("in the distance, dust settling") == []


def test_two_letter_scene_word_does_not_hit_a_title():
    matcher = ContextualSoundMatcher([_entry("ding-1", "Ding", "ui", [])])

    # "di" is a substring of "ding"; it is a raw token, not a cue expansion.
    assert matcher.match("di rumah") == []


def test_the_gagal_cue_still_bridges_to_a_multi_word_title():
    matcher = ContextualSoundMatcher([_entry("sad-1", "Sad Trombone", "sfx", [])])

    assert [result["id"] for result in matcher.match("rencana tersebut gagal total")] == ["sad-1"]


def test_compact_matching_does_not_double_count_a_token_hit():
    matcher = ContextualSoundMatcher([_entry("pop-1", "Pop", "sfx", [])])

    assert matcher.match("pop")[0]["score"] == 3


def test_compact_matching_ignores_single_character_terms():
    # A one-character scene word is never compact-matched against a title.
    matcher = ContextualSoundMatcher([_entry("sad-1", "Sad Trombone", "sfx", [])])

    assert matcher.match("a") == []


# --- Integration across the download seam ----------------------------------


async def test_a_real_download_feeds_the_matcher_without_hand_written_tags(tmp_path):
    """Download -> manifest on disk -> matcher, with tags derived by the downloader."""

    sounds = {
        "/media/sounds/sad-trombone.mp3": ("sadtrombone-1", "Sad Trombone", "sfx"),
        "/media/sounds/vine-boom.mp3": ("vine-boom-1", "Vine Boom", "memes"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ID3-fake")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = StorageManager(tmp_path / "downloads")
    downloader = BulkDownloader(storage, client=client)
    buttons = [
        InstantButton(
            id=instant_id,
            title=title,
            audio_url=f"{BASE_URL}{path}",
            category=category,
            page_url=f"{BASE_URL}/en/instant/{instant_id}/",
        )
        for path, (instant_id, title, category) in sounds.items()
    ]

    async with client:
        paths = await downloader.download_batch(buttons)

    assert len(paths) == 2
    # Reload from disk so the test sees exactly what a later run would read.
    entries = StorageManager(tmp_path / "downloads").entries()
    assert all(entry["tags"] for entry in entries)

    matches = ContextualSoundMatcher(entries).match("rencana tersebut gagal total")

    assert [result["title"] for result in matches][:1] == ["Sad Trombone"]


# --- Scoring ---------------------------------------------------------------


def test_title_only_match_outranks_tag_only_match():
    # Neither entry matches in both fields, so the weights decide alone.
    title_only = _entry("title-only", "Button", "ui", ["tick"])
    tag_only = _entry("tag-only", "Tick", "ui", ["button"])
    matcher = ContextualSoundMatcher([tag_only, title_only])

    results = {result["id"]: result["score"] for result in matcher.match("button", top_k=2)}

    assert results["title-only"] == 3  # TITLE_WEIGHT
    assert results["tag-only"] == 2  # TAG_WEIGHT
    assert matcher.match("button")[0]["id"] == "title-only"


def test_results_carry_the_result_shape(entries):
    matcher = ContextualSoundMatcher(entries)

    result = matcher.match("win")[0]

    assert set(result) == {"id", "title", "category", "path", "score"}


# --- Empty, unmatched, and slicing -----------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text_returns_nothing(entries, text):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match(text) == []


def test_none_text_returns_nothing(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match(None) == []


def test_unmatched_text_returns_nothing_and_does_not_raise(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match("zzz qqq", top_k=2) == []


def test_empty_bank_returns_nothing():
    assert ContextualSoundMatcher([]).match("muncul") == []


def test_top_k_slices_the_ranking(entries):
    matcher = ContextualSoundMatcher(entries)

    results = matcher.match("sfx", top_k=2)

    assert len(results) == 2
    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)


def test_top_k_is_capped_and_scores_are_non_increasing(entries):
    matcher = ContextualSoundMatcher(entries)

    results = matcher.match("win", top_k=5)

    assert len(results) <= 5
    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)


def test_non_positive_top_k_returns_nothing(entries):
    matcher = ContextualSoundMatcher(entries)

    assert matcher.match("win", top_k=0) == []
    assert matcher.match("win", top_k=-3) == []


def test_matching_is_deterministic(entries):
    matcher = ContextualSoundMatcher(list(reversed(entries)))

    first = matcher.match("sfx", top_k=4)
    second = matcher.match("sfx", top_k=4)

    assert first == second
