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
```

| Command | What it does |
| --- | --- |
| `run.py` | Interactive menu |
| `--trending --limit N` | Grab what's trending right now |
| `--category <name> --limit N` | Grab one category |
| `--search "<query>" --limit N` | Grab search results |
| `--match "<scene text>"` | Rank the bank against a scene description |
| `--rebuild-index` | Rebuild `sound-bank.json` from the manifest |

Activate the environment once and the same commands shorten to `python run.py ...`:

```bat
.venv\Scripts\activate
```

Tests:

```bat
.venv\Scripts\python.exe -m pytest -q
```

## What it produces

```
downloads/
  manifest.json          # every sound ever downloaded — never fetched twice
  <category>/
    <sound>.mp3
sound-bank.json          # searchable registry built from the manifest
```

`downloads/` is runtime data. It is git-ignored and can be deleted at any time;
`sound-bank.json` is regenerated with `--rebuild-index`.

## Using the bank from Remotion

Copy (or symlink) `downloads/` into your project's `public/remosound/`, then
reference a file by its category and filename:

```tsx
import { Audio, staticFile, Sequence } from "remotion";

export const ExplosionScene = () => (
  <Sequence from={0} durationInFrames={90}>
    <Audio src={staticFile("remosound/explosions/boom-01.mp3")} />
  </Sequence>
);
```

`sound-bank.json` maps each sound to its category, tags, duration, and source
page, so you can match a scene with `--match "<scene text>"` first and paste the
suggested path into `staticFile(...)`.

## Etiquette and limits

RemoSound is deliberately gentle with MyInstants: at most **5 concurrent
downloads**, at least **0.5 s between page fetches**, and exponential backoff
(0.5 s, 1 s, 2 s) on HTTP 429 before it gives up on that request.
`--max-concurrency` can lower the limit but never raise it.

## Disclaimer

Audio files are community-uploaded content from MyInstants. RemoSound only automates retrieval. You are responsible for verifying rights before commercial use.

## License

[MIT](LICENSE) © 2026 RemoSound contributors
