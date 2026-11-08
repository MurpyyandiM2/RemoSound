# Repository Guide: RemoSound

RemoSound is a Python CLI that bulk-downloads sounds from the sources the user
chooses (Mixkit built in, others as provider plugins) into a local sound bank
(`downloads/`) and exposes a
contextual matcher for Remotion video projects. This file is the operating
guide for every agent working in this repository — OpenCode, Claude Code,
Cursor, Antigravity, or a human in a shell.

## Environment

- **Windows host, spaces in paths.** Paths on Windows often contain
  spaces, so quote every path in commands and scripts.
- **Always use the virtualenv interpreter.** Dependencies live in `.venv/`
  and are not installed system-wide:

  ```bat
  cmd /c "python -m venv .venv"
  cmd /c ".venv\Scripts\python.exe -m pip install -r requirements.txt"
  ```

  `.venv\Scripts\activate` lets you type `python` afterwards, but the
  canonical spelling in this repo is the explicit `.venv\Scripts\python.exe`.
- **Windows Shell & Quoting.** On Windows PowerShell or environments with
  quoting hooks, wrap shell commands in `cmd /c "..."` and use `&` — never `&&` —
  inside a single `cmd /c`.

  ```bat
  cmd /c "git status"
  cmd /c ".venv\Scripts\python.exe -m pytest -q"
  cmd /c "git add -A"
  cmd /c "git commit -F commitmsg.tmp"
  ```

  For commits with quotes or multiple lines, writing the message to a file
  and committing with `-F` avoids shell quote mangling across environments.

  Prefer dedicated file tools (`read`, `write`, `edit`, `glob`, `grep`) over
  shell one-liners.
- **Scratch space.** Use the OS temp directory (`%TEMP%`)
  for temporary files, never a unix `/tmp`.

## Canonical commands

`run.py` is the single entrypoint. Do not add parallel scripts; extend the CLI.
Do not re-implement scraping inline; call `run.py`.

```bat
.venv\Scripts\python.exe run.py
.venv\Scripts\python.exe run.py --web
.venv\Scripts\python.exe run.py --trending --limit 20
.venv\Scripts\python.exe run.py --category memes --limit 20
.venv\Scripts\python.exe run.py --search "boom" --limit 20
.venv\Scripts\python.exe run.py --match "explosion in the distance, dust settling"
.venv\Scripts\python.exe run.py --rebuild-index
.venv\Scripts\python.exe run.py --trending --limit 20 --dry-run
.venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public
.venv\Scripts\python.exe -m pytest -q
```

`run.py` with no flags opens the interactive menu (Trending / Category /
Search / Match / Rebuild index / Web studio); the mode flags (`--trending`,
`--category`, `--search`, `--match`, `--rebuild-index`, `--web`) are mutually
exclusive.

| Flag | Meaning |
| --- | --- |
| `--web` | Serve the local web studio on `127.0.0.1:5057-5066` and open it |
| `--trending` | Collect trending sounds |
| `--category NAME` | Collect sounds from one category |
| `--search QUERY` | Collect sounds matching search query |
| `--provider P` | Sound source: `mixkit` (default, built in) or any plugin id |
| `--delete ID` | Delete a sound from bank and manifest by ID |
| `--limit N` | Sounds to collect; default 100, capped at 1000 per run |
| `--project DIR` | Remotion project: bank lives in `DIR/public/remosound`, no copying |
| `--out DIR` | Bank root, default `downloads` |
| `--dry-run` | Print the plan; write nothing |
| `--max-concurrency N` | Parallel downloads, default 5; may only lower the cap |
| `--remotion-public DIR` | Stage the bank into a Remotion `public/`; needs a batch mode or `--rebuild-index` |

`--match` prints `{"context", "matches"}` JSON on stdout and exits 0. Exit
codes: `0` success, `1` runtime failure, `2` usage error.

## Module map

Data flows one direction; keep it that way.

```
run.py            entrypoint, argument parsing, wiring
remosound/
  cli.py          menus, flags, rich output
  web.py          local HTTP studio: jobs, JSON API, Range audio streaming
  web/index.html  studio markup; web/static/studio.css and studio.js hold its styles and script
  web/assets/     brand images served at /assets/ (also used by README)
  scraper.py      Scraper base + Mixkit: pages -> sound records (rate limited)
  providers.py    provider registry: built-in Mixkit + plugins from ~/.remosound/providers
  ../contrib/providers/  example provider plugins (MyInstants); not installed by default
  downloader.py   sound records -> files under downloads/ (bounded concurrency)
  storage.py      downloads/manifest.json (dedupe record)
  matcher.py      scene text -> ranked sounds
  remotion_adapter.py  staticFile()-ready paths for Remotion projects
```

`scraper` → `downloader` → `storage` → `matcher` → `remotion_adapter` → `cli`
and `web`. Lower layers never import `cli` or `web`; neither `cli` nor `web`
imports the other, and nothing but `run.py` touches `sys.argv`.

## Web studio

`--web` is a second surface over the same layers, not a second pipeline. It
must keep these invariants:

- stdlib only (`http.server`, `ThreadingHTTPServer`) — no Node, npm, or build
  step; adding a dependency for the UI is a regression.
- binds `127.0.0.1` only; audio exists solely at `/api/audio?id=<instant-id>`,
  never at a filesystem path, and the id resolves through the manifest with a
  bank-root containment check.
- downloads go through `BulkDownloader` and the same ethics limits; each job
  runs on a daemon thread with its own event loop and its own
  `StorageManager`.
- the UI lives in `remosound/web/index.html` plus `web/static/studio.css` and `studio.js`; every user-supplied string from
  the API must be HTML-escaped before it reaches `innerHTML`.

## Download ethics (hard limits)

- At most **5 concurrent downloads** (`--max-concurrency` may lower this,
  never raise it).
- At least **0.5 s between page fetches** (a plugin cannot lower it) and at
  most **250 pages** walked per listing.
- At most **1000 sounds per run** — `--limit` cannot request more — and at most
  **3000 downloads per day** across all runs.
- Downloads start at least **0.2 s** apart. Audio links must be `http(s)` to a
  public address (redirects included); one file may not exceed **50 MB**.
All of these live in `remosound/safety.py`; raising them is a regression.
- The web studio runs **one download job at a time** and refuses a foreign
  `Host`/`Origin` header or a non-JSON POST.
- Honor HTTP **429** with exponential backoff: 0.5 s, 1 s, 2 s, then give up
  after 3 retries.
- Anything already recorded in `downloads/manifest.json` is never downloaded
  again. Partial files carry a `.part` suffix and are deleted on failure.
- Every filename written to disk strips or replaces `\ / : * ? " < > |`,
  control characters, and trailing dots/spaces, and never ends up empty
  (fall back to the instant id).

## Runtime data vs. source

`downloads/`, `sound-bank.json`, and `sound-bank.ts` are **runtime data, not
source**. They are git-ignored, may be deleted at any time, and must never be
committed or edited by hand. Change the code that generates them instead.

## License and content rights

Code is MIT (see `LICENSE`). RemoSound ships no audio and only automates
retrieval: Mixkit content is royalty-free under its own licence, content from
plugin sources (`contrib/providers/`) may be copyrighted, and users are
responsible for verifying rights before commercial use. Keep that
disclaimer in `README.md`.
