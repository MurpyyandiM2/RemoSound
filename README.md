# RemoSound

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

RemoSound is a small Python CLI that bulk-downloads sound effects from
[MyInstants](https://www.myinstants.com/) into a local **sound bank** and then
matches those sounds to a described scene, so a Remotion project can pull the
right cue with `staticFile(...)` instead of hunting for one by hand.

It runs entirely on your machine, keeps a manifest of everything it has already
fetched, and never re-downloads a sound it already owns.

## Requirements

- Windows, macOS, or Linux
- Python 3.12 or newer (developed against 3.14)

## Install

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS/Linux the interpreter path is `.venv/bin/python`.

## Commands

Run everything through the virtualenv interpreter. `run.py` is the only
entrypoint — there are no separate scripts to remember.

```bat
.venv\Scripts\python.exe run.py
.venv\Scripts\python.exe run.py --trending --limit 20
.venv\Scripts\python.exe run.py --category memes --limit 20
.venv\Scripts\python.exe run.py --search "boom" --limit 20
.venv\Scripts\python.exe run.py --match "explosion in the distance, dust settling"
.venv\Scripts\python.exe run.py --rebuild-index
.venv\Scripts\python.exe run.py --trending --limit 20 --dry-run
.venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public
```

| Command | What it does |
| --- | --- |
| `run.py` | Interactive menu |
| `--trending --limit N` | Grab what's trending right now |
| `--category <name> --limit N` | Grab one category |
| `--search "<query>" --limit N` | Grab search results |
| `--match "<scene text>"` | Rank the bank against a scene description, print JSON (top 5) |
| `--rebuild-index` | Rebuild `sound-bank.json` and `sound-bank.ts` from the manifest |
| `--dry-run` | Print what a run would download; write nothing |
| `--out DIR` | Bank root (default `downloads`) |
| `--remotion-public DIR` | Stage the bank into a Remotion `public/` after a successful run |
| `--max-concurrency N` | Parallel downloads (default 5; values above 5 are capped at 5) |

The mode flags (`--trending`, `--category`, `--search`, `--match`,
`--rebuild-index`) are mutually exclusive. `--limit` defaults to 100 and is
capped at 5000 per run. `--remotion-public` needs a batch command or
`--rebuild-index`. Exit codes: `0` success, `1` runtime failure, `2` usage
error.

Activate the environment once and the same commands shorten to `python run.py ...`:

```bat
.venv\Scripts\activate
```

Tests:

```bat
.venv\Scripts\python.exe -m pytest -q
```

## Supported harnesses

Supported harnesses: OpenCode, Claude Code, Cursor, Antigravity. Each one reads
the same canonical command set, from the instruction file it discovers on its
own:

| Harness | Rule file it reads |
| --- | --- |
| OpenCode | `AGENTS.md` |
| Claude Code | `CLAUDE.md` |
| Cursor | `.cursor/rules/remosound.mdc` |
| Antigravity | `AGENTS.md` (and `GEMINI.md` if present) |

`SKILL.md` is the harness-agnostic skill: any agent with skill support can load
it, and it documents the identical command set.

## What it produces

```
downloads/                    # --out, default
  manifest.json               # every sound ever downloaded — never fetched twice
  <category>/                 # sanitized, lowercased category slug
    <sound>.mp3
sound-bank.json               # searchable registry built from the manifest
sound-bank.ts                 # dependency-free lookup helper for Remotion
```

`downloads/`, `sound-bank.json`, and `sound-bank.ts` are runtime data. They are
git-ignored and can be deleted at any time; both registry files are regenerated
with `--rebuild-index`.

## Using the bank from Remotion

`--remotion-public` does both halves of the integration in one run: it copies
the bank into `<project>/public/remosound/<category>/` and writes
`sound-bank.json` beside it, so `staticFile()` resolves at render time.

```bat
.venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public
```

To restage an existing bank without downloading anything new:

```bat
.venv\Scripts\python.exe run.py --rebuild-index --remotion-public .\my-video\public
```

The same run writes `sound-bank.ts` next to the bank root. Copy it into your
Remotion source tree (for example `src/lib/sound-bank.ts`); it has no imports
and exports `SOUND_BANK`, the `SoundEntry` type, `getSound(id)`, and
`findSounds(query)`. Every entry's `src` is already
`remosound/<category>/<file>.mp3`, so it can go straight into `staticFile(...)`:

```tsx
import { Audio, staticFile, Sequence } from "remotion";
import { findSounds, getSound } from "./lib/sound-bank";

export const ExplosionScene = () => {
  // ids are MyInstants detail slugs (for example "vine-boom-101") and
  // getSound returns undefined for unknown ids, so fall back to a text search.
  const cue = getSound("vine-boom-101") ?? findSounds("vine boom")[0];
  if (!cue) return null;
  return (
    <Sequence from={0} durationInFrames={90}>
      <Audio src={staticFile(cue.src)} />
    </Sequence>
  );
};
```

For a hand-written path, use the same layout directly:
`staticFile("remosound/explosions/boom-01.mp3")`.

`sound-bank.json` records `{id, title, category, file, src}` per sound. `--match`
prints `{"context": "...", "matches": [{"id", "title", "category", "path", "score"}]}`
on stdout and exits 0. That `path` is bank-relative (`memes/vine-boom.mp3`),
while the staged audio lives at `public/remosound/memes/vine-boom.mp3` — so
prefix the returned `path` with `remosound/` before passing it to
`staticFile(...)`, or use the registry `src`, which is already prefixed.

## Etiquette and limits

RemoSound is deliberately gentle with MyInstants: at most **5 concurrent
downloads**, at least **0.5 s between page fetches**, at most **5000 sounds per
run**, and exponential backoff (0.5 s, 1 s, 2 s) on HTTP 429 before it gives up
after 3 retries. `--max-concurrency` can lower the concurrency limit but never
raise it, and `--limit` cannot request more than 5000 in one run.

## Disclaimer

Audio files are community-uploaded content from MyInstants. RemoSound only automates retrieval. You are responsible for verifying rights before commercial use.

## License

[MIT](LICENSE) © 2026 RemoSound contributors
