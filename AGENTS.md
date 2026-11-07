# Repository Guide: RemoSound

RemoSound is a Python CLI that bulk-downloads sound effects from MyInstants
into a local sound bank (`downloads/`) and exposes a contextual matcher for
Remotion video projects. This file is the operating guide for every agent
working in this repository — OpenCode, Claude Code, Cursor, Antigravity, or a
human in a shell.

## Environment

- **Windows host, spaces in paths.** The checkout lives under
  `C:\Users\MyBook SAGA 12\InstallSound`. Quote every path in commands and
  scripts.
- **Always use the virtualenv interpreter.** Dependencies live in `.venv/`
  and are not installed system-wide:

  ```bat
  cmd /c "python -m venv .venv"
  cmd /c ".venv\Scripts\python.exe -m pip install -r requirements.txt"
  ```

  `.venv\Scripts\activate` lets you type `python` afterwards, but the
  canonical spelling in this repo is the explicit `.venv\Scripts\python.exe`.
- **Raw PowerShell is broken here.** A shell hook (`caveman shrink`) rejects
  bare `git ...` / `python ...` commands with `Unexpected token 'shrink'`.
  Wrap every shell command in `cmd /c "..."`, and use `&` — never `&&` —
  inside a single `cmd /c`.

  ```bat
  cmd /c "git status"
  cmd /c ".venv\Scripts\python.exe -m pytest -q"
  cmd /c "git add -A"
  cmd /c "git commit -F commitmsg.tmp"
  ```

  `git commit -m "message"` with escaped quotes is unreliable on this host —
  the hook mangles it and git treats the message words as pathspecs. Write the
  message to a file, commit with `-F`, then delete the file.

  Prefer dedicated file tools (`read`, `write`, `edit`, `glob`, `grep`) over
  shell one-liners.
- **Scratch space.** Use `C:\Users\MyBook SAGA 12\AppData\Local\Temp\opencode`
  for temporary files, never a unix `/tmp`.

## Canonical commands

`run.py` is the single entrypoint. Do not add parallel scripts; extend the CLI.
Do not re-implement scraping inline; call `run.py`.

```bat
.venv\Scripts\python.exe run.py
.venv\Scripts\python.exe run.py --trending --limit 20
.venv\Scripts\python.exe run.py --category memes --limit 20
.venv\Scripts\python.exe run.py --search "boom" --limit 20
.venv\Scripts\python.exe run.py --match "explosion in the distance, dust settling"
.venv\Scripts\python.exe run.py --rebuild-index
.venv\Scripts\python.exe run.py --trending --limit 20 --dry-run
.venv\Scripts\python.exe run.py --trending --remotion-public .\my-video\public
.venv\Scripts\python.exe -m pytest -q
```

`run.py` with no flags opens the interactive menu; the mode flags
(`--trending`, `--category`, `--search`, `--match`, `--rebuild-index`) are
mutually exclusive.

| Flag | Meaning |
| --- | --- |
| `--limit N` | Sounds to collect; default 100, capped at 5000 per run |
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
  scraper.py      MyInstants pages -> sound records (rate limited)
  downloader.py   sound records -> files under downloads/ (bounded concurrency)
  storage.py      downloads/manifest.json (dedupe record)
  matcher.py      scene text -> ranked sounds
  remotion_adapter.py  staticFile()-ready paths for Remotion projects
```

`scraper` → `downloader` → `storage` → `matcher` → `remotion_adapter` → `cli`.
Lower layers never import `cli`; nothing but `run.py` touches `sys.argv`.

## Download ethics (hard limits)

- At most **5 concurrent downloads** (`--max-concurrency` may lower this,
  never raise it).
- At least **0.5 s between page fetches**.
- At most **5000 sounds per run** — `--limit` cannot request more.
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

Code is MIT (see `LICENSE`). The audio is community-uploaded MyInstants
content: RemoSound only automates retrieval, and users are responsible for
verifying rights before commercial use. Keep that disclaimer in `README.md`.
