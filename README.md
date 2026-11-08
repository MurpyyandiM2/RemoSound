# RemoSound

<p align="center">
  <img src="remosound/web/assets/logo-banner.png" alt="RemoSound" width="520">
</p>

<p align="center">
  <a href="https://github.com/MurpyyandiM2/RemoSound/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/MurpyyandiM2/RemoSound?color=22c55e"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
  <a href="https://murpyyandim2.github.io/RemoSound/"><img alt="Website" src="https://img.shields.io/badge/website-live-22c55e"></a>
  <a href="https://github.com/MurpyyandiM2/RemoSound/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/MurpyyandiM2/RemoSound?style=flat"></a>
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="JavaScript" src="https://img.shields.io/badge/JavaScript-F7DF1E?logo=javascript&logoColor=black">
  <img alt="HTML5" src="https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white">
  <img alt="CSS3" src="https://img.shields.io/badge/CSS3-1572B6?logo=css3&logoColor=white">
  <a href="https://www.remotion.dev/"><img alt="Made for Remotion" src="https://img.shields.io/badge/made%20for-Remotion-0B84F3"></a>
</p>

RemoSound is a bulk sound downloader, a Python CLI and a portable Agent Skill.
It fills a local **sound bank** from the sources you choose, then matches those
sounds to a described scene, so a Remotion project can pull the right cue with
`staticFile(...)` instead of hunting for one by hand. Install it as a skill and
your agent can find and download audio while it builds a video.

RemoSound is a tool, not a library of sounds: it ships no audio. [Mixkit](https://mixkit.co/)
(royalty-free) is built in, and any other source is a small plugin you add
yourself (see [Providers](#providers)).

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
.venv\Scripts\python.exe run.py --web
.venv\Scripts\python.exe run.py --trending --limit 20
.venv\Scripts\python.exe run.py --trending --provider mixkit --limit 20
.venv\Scripts\python.exe run.py --category memes --limit 20
.venv\Scripts\python.exe run.py --category transition --provider mixkit --limit 20
.venv\Scripts\python.exe run.py --search "boom" --limit 20
.venv\Scripts\python.exe run.py --search "whoosh" --provider mixkit --limit 20
.venv\Scripts\python.exe run.py --match "explosion in the distance, dust settling"
.venv\Scripts\python.exe run.py --delete boom-1
.venv\Scripts\python.exe run.py --rebuild-index
.venv\Scripts\python.exe run.py --trending --limit 20 --dry-run
.venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public
```

| Command | What it does |
| --- | --- |
| `run.py` | Interactive menu: trending, category, search, match, rebuild index, web studio |
| `--web` | Open the local web studio (soundboard, downloader, Remotion matcher) |
| `--trending --limit N` | Grab what's trending right now |
| `--category <name> --limit N` | Grab one category |
| `--search "<query>" --limit N` | Grab search results |
| `--provider <id>` | Select the source (default `mixkit`; plugins add more ids) |
| `--delete <id>` | Delete one sound from the bank and manifest by ID |
| `--match "<scene text>"` | Rank the bank against a scene description, print JSON (top 5) |
| `--rebuild-index` | Rebuild `sound-bank.json` and `sound-bank.ts` from the manifest |
| `--dry-run` | Print what a run would download; write nothing |
| `--project DIR` | Remotion project folder: the bank lives in `DIR/public/remosound`, no copying needed |
| `--out DIR` | Bank root (default `downloads`) |
| `--remotion-public DIR` | Stage the bank into a Remotion `public/` after a successful run |
| `--max-concurrency N` | Parallel downloads (default 5; values above 5 are capped at 5) |

The mode flags (`--trending`, `--category`, `--search`, `--match`,
`--delete`, `--rebuild-index`, `--web`) are mutually exclusive. `--limit` defaults to 100
and is capped at 250 per run. `--remotion-public` needs a batch command or
`--rebuild-index`. Exit codes: `0` success, `1` runtime failure, `2` usage
error.

Activate the environment once and the same commands shorten to `python run.py ...`:

```bat
.venv\Scripts\activate
```

## Web studio

`run.py --web` serves a local single-page studio at `http://127.0.0.1:5057/`
(the next free port in `5057-5066` if that one is taken) and opens the browser.
It is Python's standard library only — no Node, no npm, no build step, no extra
dependency.

- **Soundboard** — every sound in the bank with search, category filters, a licence badge per source, delete with confirmation, and a persistent player. Audio streams with HTTP Range support.
- **Downloader** — pick a source, choose between category browsing or online search query, set amount, and watch live download progress.
- **Remotion studio** — describe a scene in Indonesian or English, get the top 8 ranked cues in side-by-side view, preview each one, copy ready `<Audio src={staticFile(...)} />` JSX, or delete unwanted cues.

The server binds `127.0.0.1` only and stops on Ctrl+C. `--out` selects the bank
it reads and writes, exactly like the CLI:

```bat
.venv\Scripts\python.exe run.py --web --out .\downloads
```

## Providers

A provider is one sound source. Mixkit is built in and is the default:
`transition`, `nature`, `technology`, `funny`, `notification`, `animals`,
`transport`, `game`, `whoosh`, `impact`, `cinematic`, `ambience`, `alarm`,
`cartoon`.

Any other source is a plugin: a single `.py` file in `~/.remosound/providers`
(or the folder named by `REMOSOUND_PLUGINS`) that defines a `PROVIDER`. Plugins
are ordinary Python that runs on your machine at startup, so install only files
you trust. [`contrib/providers/myinstants.py`](contrib/providers/myinstants.py)
is a working example and the template for your own: subclass `Scraper`, write
`parse_buttons` and three `crawl_*` methods, and expose `PROVIDER`.

```bat
copy contrib\providers\myinstants.py "%USERPROFILE%\.remosound\providers\"
.venv\Scripts\python.exe run.py --trending --provider myinstants --limit 20
```

Pick the source with `--provider <id>` or in the web studio. Every source
brings its own terms; you decide what to download and what to use it for.

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

## Download straight into your Remotion project

Point RemoSound at your Remotion project and the bank lives inside it. There is
one copy of every sound, and it is ready for `staticFile()` the moment it
downloads:

```bat
.venv\Scripts\python.exe run.py --search "wind" --project C:\Users\you\videos\my-video
.venv\Scripts\python.exe run.py --web --project C:\Users\you\videos\my-video
```

Sounds land in `my-video/public/remosound/<category>/`, and `sound-bank.json` and
`sound-bank.ts` are refreshed in `my-video/public/`. The folder must be a
Remotion project (it needs a `package.json`). `--project` replaces `--out` and
`--remotion-public`, so use it on its own. Without `--project` the bank stays in
`downloads/` and you stage it with `--remotion-public`, as below.

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
  // ids are source ids (for example "mixkit-123") and
  // getSound returns undefined for unknown ids, so fall back to a text search.
  const cue = getSound("mixkit-123") ?? findSounds("whoosh")[0];
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

RemoSound is deliberately gentle with remote providers: at most **5 concurrent
downloads**, at least **0.5 s between page fetches**, at most **250 sounds per
run**, and exponential backoff (0.5 s, 1 s, 2 s) on HTTP 429 before it gives up
after 3 retries. `--max-concurrency` can lower the concurrency limit but never
raise it, and `--limit` cannot request more than 250 in one run: asking for
`999999999` simply gives 250.

On top of that, download starts are spaced at least 0.2 s apart, and a daily
budget of **3000 downloads** is shared by every run and every bank (it is kept
in `~/.remosound/budget.json`). When it is spent, RemoSound stops and says so;
try again tomorrow.

An audio link comes from a remote page, so it is never trusted: only `http(s)`
links to **public** addresses are fetched (a link to `127.0.0.1`, your LAN or a
cloud metadata address is refused, and so is a redirect to one), and one file
may not exceed **50 MB**. See [SECURITY.md](SECURITY.md).

These limits live in the shared layers, so a provider plugin cannot opt out of
them: the delay between fetches has a 0.5 s floor, and one listing is never
walked past 250 pages. The web studio runs **one download at a time**, so
repeated requests cannot stack up more parallel downloads.

The studio only answers the person at the machine. It binds `127.0.0.1`,
refuses a foreign `Host` or `Origin` header (so a web page you happen to visit
cannot start downloads, and DNS rebinding does not work), and accepts changes
only as JSON. RemoSound opens no port to the internet and runs no server of
its own that others could point at a site.

## Disclaimer

RemoSound only automates retrieval from the sources you choose, and ships no
audio. The built-in source, Mixkit, publishes its sounds under the Mixkit
licence. Content from any plugin source may be protected by copyright and by
that site's terms of use. You are responsible for checking the rights and terms
for every sound before using it, especially commercially. RemoSound is an
independent project and is not affiliated with Mixkit, Remotion or any site you
download from.

## License

[MIT](LICENSE) © 2026 RemoSound contributors
