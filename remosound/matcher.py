"""Contextual matching of scene text to sounds in the local bank.

A :class:`ContextualSoundMatcher` is a pure, in-memory view over records from
:meth:`remosound.storage.StorageManager.entries`. It never touches the network
or the filesystem, so a match is cheap enough to run per scene while editing.

Scene text is tokenized, then expanded through :data:`CUE_MAP`: an Indonesian
cue such as ``"muncul"`` adds the search terms ``pop``, ``appear`` and
``whoosh``, which is how a title like ``"Pop Whoosh"`` gets found from a scene
written in Indonesian. Every term present in a sound's title as a whole token
scores :data:`TITLE_WEIGHT`; cue expansion terms of at least
:data:`MIN_COMPACT_LENGTH` may also score by appearing inside the title with
every separator removed, so ``sadtrombone`` reaches ``"Sad Trombone"``. Raw
scene words are never substring-matched. Every term in a sound's tags scores
:data:`TAG_WEIGHT`, and every term in its category scores
:data:`CATEGORY_WEIGHT`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Scene cue words (Indonesian and English) -> extra search terms. A cue is
# matched as a whole token sequence, so both "tiba-tiba" and "tiba tiba" work.
CUE_MAP: dict[str, list[str]] = {
    # something appears / enters
    "muncul": ["pop", "appear", "whoosh"],
    "tiba-tiba": ["pop", "appear", "whoosh"],
    "pop": ["pop", "appear", "whoosh"],
    # something startles the viewer
    "kaget": ["scream", "screamers", "boom"],
    "scream": ["scream", "screamers", "boom"],
    "jumpscare": ["scream", "screamers", "boom"],
    # success
    "menang": ["victory", "fanfare", "applause", "win"],
    "juara": ["victory", "fanfare", "applause", "win"],
    "victory": ["victory", "fanfare", "applause", "win"],
    # failure
    "gagal": ["fail", "sadtrombone", "bruh", "wrong"],
    "salah": ["fail", "sadtrombone", "bruh", "wrong"],
    "fail": ["fail", "sadtrombone", "bruh", "wrong"],
    # interaction
    "klik": ["click", "button", "tick"],
    "tombol": ["click", "button", "tick"],
    "click": ["click", "button", "tick"],
    # money
    "uang": ["coin", "cash", "money"],
    "koin": ["coin", "cash", "money"],
    "coin": ["coin", "cash", "money"],
    # something explodes / a heavy hit lands
    "explosion": ["explosion", "boom", "blast", "impact"],
    "ledakan": ["explosion", "boom", "blast", "impact"],
    "meledak": ["explosion", "boom", "blast", "impact"],
    "blast": ["explosion", "boom", "blast", "impact"],
    "boom": ["explosion", "boom", "blast", "impact"],
}

TITLE_WEIGHT = 3
TAG_WEIGHT = 2
CATEGORY_WEIGHT = 1

# A cue expansion shorter than this never substring-matches a title; short
# strings are exactly the ones that hit unrelated words by accident.
MIN_COMPACT_LENGTH = 4

_WORD = re.compile(r"[a-z0-9]+")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, in order, including numbers."""

    return _WORD.findall(text.lower())


def _compact(text: str) -> str:
    """The title with every separator removed (``"Sad Trombone"`` -> ``"sadtrombone"``)."""

    return _NON_WORD.sub("", text.lower())


def _contains(tokens: list[str], sequence: tuple[str, ...]) -> bool:
    """Whether ``sequence`` appears contiguously inside ``tokens``."""

    width = len(sequence)
    if not width or width > len(tokens):
        return False
    windows = (tuple(tokens[index : index + width]) for index in range(len(tokens) - width + 1))
    return any(window == sequence for window in windows)


def _cue_sequences() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Pre-tokenized ``(cue, expansion)`` pairs built once from :data:`CUE_MAP`."""

    pairs = []
    for cue, expansion in CUE_MAP.items():
        terms = tuple(dict.fromkeys(token for term in expansion for token in _tokens(term)))
        pairs.append((tuple(_tokens(cue)), terms))
    return tuple(pairs)


_CUE_SEQUENCES = _cue_sequences()


class ContextualSoundMatcher:
    """Rank bank entries against a scene description."""

    def __init__(self, entries: list[dict]) -> None:
        self._sounds = tuple(self._prepare(entry) for entry in entries)

    def match(self, context_text: str, top_k: int = 1) -> list[dict]:
        """Return up to ``top_k`` sounds for ``context_text``, best first.

        Results are ``{"id", "title", "category", "path", "score"}`` dicts.
        Blank, ``None``, or unmatched text yields ``[]``; nothing here raises on
        ordinary input.
        """

        context_text = context_text or ""
        if top_k <= 0:
            return []

        terms, compact_terms = self._query_terms(context_text)
        if not terms:
            return []

        scored = []
        for sound in self._sounds:
            score = (
                TITLE_WEIGHT * (len(terms & sound.title_tokens) + sound.compact_hits(compact_terms))
                + TAG_WEIGHT * len(terms & sound.tag_tokens)
                + CATEGORY_WEIGHT * len(terms & sound.category_tokens)
            )
            if score <= 0:
                continue
            scored.append(
                {
                    "id": sound.id,
                    "title": sound.title,
                    "category": sound.category,
                    "path": sound.path,
                    "score": score,
                }
            )

        # Score first, then a stable title/id order so equal scores never shuffle.
        scored.sort(key=lambda result: (-result["score"], result["title"].casefold(), str(result["id"])))
        return scored[:top_k]

    @staticmethod
    def _query_terms(context_text: str) -> tuple[set[str], frozenset[str]]:
        """Return the query terms and the subset allowed to match compactly.

        The first set is every term the query contributes — scene tokens plus
        cue expansions — matched against whole title tokens only. The second is
        the cue expansions of at least :data:`MIN_COMPACT_LENGTH` characters
        that the scene text did not already contain; those may also match as a
        substring of the compacted title.

        Restricting compact matching to cue terms is what keeps ordinary scene
        words from firing by accident: ``"in"`` is a substring of
        ``"coinpickup"``, but it arrives as a raw token, not as a cue.
        """

        tokens = _tokens(context_text)
        terms = set(tokens)
        expansions: set[str] = set()
        for sequence, expansion in _CUE_SEQUENCES:
            if _contains(tokens, sequence):
                expansions.update(expansion)
        terms.update(expansions)
        compact = frozenset(
            term for term in expansions if len(term) >= MIN_COMPACT_LENGTH and term not in tokens
        )
        return terms, compact

    @staticmethod
    def _prepare(entry: dict) -> "_Sound":
        title = str(entry.get("title") or "")
        tags = entry.get("tags") or []
        return _Sound(
            id=entry.get("id"),
            title=title,
            category=str(entry.get("category") or ""),
            path=str(entry.get("path") or ""),
            title_tokens=frozenset(_tokens(title)),
            title_compact=_compact(title),
            tag_tokens=frozenset(token for tag in tags for token in _tokens(str(tag))),
            category_tokens=frozenset(_tokens(str(entry.get("category") or ""))),
        )


@dataclass(frozen=True)
class _Sound:
    """An entry with its tokenized fields precomputed."""

    id: str
    title: str
    category: str
    path: str
    title_tokens: frozenset[str]
    title_compact: str
    tag_tokens: frozenset[str]
    category_tokens: frozenset[str]

    def compact_hits(self, terms: frozenset[str] | set[str]) -> int:
        """How many cue terms appear inside the compacted title.

        Only cue expansion terms are ever passed here. A term that already
        matched a whole title token is skipped so one cue cannot score twice,
        and :data:`MIN_COMPACT_LENGTH` keeps short expansions out entirely.
        """

        return sum(1 for term in terms if term not in self.title_tokens and term in self.title_compact)
