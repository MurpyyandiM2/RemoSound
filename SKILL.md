---
name: remosound
description: Bulk-download MyInstants sounds into a local sound bank and match sounds to video scene text for Remotion.
---

# RemoSound

RemoSound is a Python CLI that fills a local **sound bank** from
[MyInstants](https://www.myinstants.com/), ranks that bank against a scene
description, and emits `staticFile()`-ready paths for a Remotion project. It
runs entirely on the machine, records every download in a manifest, and never
fetches the same sound twice.

`run.py` is the only entrypoint. Never re-implement scraping inline; call
`run.py`. Extend `remosound/` if behaviour is missing.

## When to use

Use this skill when a task needs any of the following:

- bulk-download sound effects (trending, one category, or a search term) into a
  reusable local bank;
- pick a sound effect for a described video moment ("explosion in the
  distance", "something pops in", "the fail buzzer");
- wire bank audio into a Remotion composition through `staticFile(...)`;
- rebuild `sound-bank.json` / `sound-bank.ts` after the bank changed;
- stage the bank into a Remotion project's `public/` directory.

Do not use it to edit or re-encode audio, to fetch anything from a host other
than MyInstants, or to build a UI.

## Environment

- Windows host with spaces in paths — quote every path.
- Use the virtualenv interpreter, never a global Python:

  ```bat
  cmd /c ".venv\Scripts\python.exe -m pip install -r requirements.txt"
  ```

- Raw PowerShell is broken on this host (a `caveman shrink` hook rejects bare
  `git ...` / `python ...`). Prefix every shell command with `cmd /c "..."`
  and join commands with `&`, never `&&`:

  ```bat
  cmd /c ".venv\Scripts\python.exe -m pytest -q"
  ```

## Canonical commands

`run.py` with no mode flag opens an interactive menu (Trending / Category /
Search / Match). Modes are mutually exclusive.

| Command | What it does |
| --- | --- |
| `.venv\Scripts\python.exe run.py` | Interactive menu |
| `run.py --trending [--limit N]` | Download the trending listing |
| `run.py --category <name> [--limit N]` | Download one category |
| `run.py --search "<query>" [--limit N]` | Download search results |
| `run.py --match "<scene text>"` | Rank the bank and print JSON (top 5) |
| `run.py --rebuild-index` | Write `sound-bank.json` and `sound-bank.ts` |
| `run.py [mode] --dry-run` | Print the plan; write nothing |
| `run.py [mode] --out DIR` | Bank root (default `downloads`) |
| `run.py [mode] --remotion-public <project>/public` | Stage the bank into Remotion after a successful run |
| `run.py [mode] --max-concurrency N` | Parallel downloads (default 5; values above 5 are capped at 5) |
| `.venv\Scripts\python.exe -m pytest -q` | Run the test suite |

Concrete spellings:

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

Rules the parser enforces:

- `--limit` is a positive integer, default **100**, and is silently capped at
  **5000 per run**.
- `--max-concurrency` is a positive integer, default **5**; the downloader
  clamps anything higher back to 5.
- `--remotion-public` needs a batch mode or `--rebuild-index`; otherwise the
  CLI exits 2 with a usage error.
- `--match` always exits 0 and prints
  `{"context": "...", "matches": [{"id", "title", "category", "path", "score"}]}`
  on stdout, with diagnostics on stderr.
- Exit codes: `0` success, `1` runtime failure (network, disk, bad registry),
  `2` usage error.

## Ethics limits (hard)

- At most **5 concurrent downloads** — `--max-concurrency` may lower this,
  never raise it.
- At least **0.5 s between page fetches**.
- Honor HTTP **429** with exponential backoff: 0.5 s, 1 s, 2 s, then give up
  after 3 retries.
- At most **5000 sounds per run** — `--limit` cannot request more.
- Anything already recorded in `<out>/manifest.json` is never downloaded
  again. Partial files carry a `.part` suffix and are deleted on failure.
- Every filename written to disk strips or replaces `\ / : * ? " < > |` and
  control characters, drops trailing dots/spaces, and never ends up empty
  (falls back to the instant id).

## Sound-bank layout

```
downloads/                    # --out, default "downloads"
  manifest.json               # dedupe record: id, title, category, path, tags
  <category-slug>/            # e.g. sound-effects, memes
    <sanitized-title>.mp3
sound-bank.json               # beside <out>: {"version": 1, "sounds": [...]}
sound-bank.ts                 # beside <out>: dependency-free lookup module
```

- `<category-slug>` is `category_slug(name)`: sanitized for Windows,
  lowercased, whitespace replaced with dashes (`"Sound Effects"` →
  `sound-effects`).
- Registry entries are `{id, title, category, file, src}` where `src` is
  `remosound/<category-slug>/<file>` — the path Remotion will serve.
- `downloads/`, `sound-bank.json`, and `sound-bank.ts` are runtime data, not
  source: they are git-ignored and must never be committed or hand-edited.
  Change the code that generates them instead.

After `--remotion-public <project>/public`, the public tree holds:

```
<project>/public/
  sound-bank.json
  remosound/
    <category-slug>/<file>.mp3
```

## Using the bank from Remotion

1. Generate the registry and helper:

   ```bat
   cmd /c ".venv\Scripts\python.exe run.py --rebuild-index"
   ```

2. Copy `sound-bank.ts` into the Remotion source tree (for example
   `src/lib/sound-bank.ts`). It has no imports and type-checks on its own,
   exporting `SOUND_BANK`, `SoundEntry`, `getSound(id)`, and
   `findSounds(query)`.

3. Stage the audio into `public/`:

   ```bat
   cmd /c ".venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public"
   ```

4. Play a sound. `getSound` returns `SoundEntry | undefined` and ids are
   MyInstants detail slugs (for example `vine-boom-101`), so fall back to a
   text search instead of asserting:

   ```tsx
   import { Audio, staticFile } from "remotion";
   import { getSound, findSounds } from "./lib/sound-bank";

   export const Boom = () => {
     const cue = getSound("vine-boom-101") ?? findSounds("vine boom")[0];
     if (!cue) return null;
     return <Audio src={staticFile(cue.src)} />;
   };
   ```

`staticFile` resolves against `public/`, which is why `src` is
`remosound/<category>/<file>.mp3`. For a hand-written path use
`staticFile("remosound/<category>/<file>.mp3")` with the slug from the bank
layout above.

## License and content rights

Code is MIT (see `LICENSE`). The audio is community-uploaded MyInstants
content: RemoSound only automates retrieval, and users are responsible for
verifying rights before commercial use.
