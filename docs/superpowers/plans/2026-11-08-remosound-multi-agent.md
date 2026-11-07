# RemoSound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `RemoSound`, a Python CLI + portable Agent Skill that bulk-downloads sound effects and music clips from MyInstants, organizes them into a local sound bank, and lets Remotion/LLM agents pick sounds automatically from video scene text.

**Architecture:** Small, single-responsibility modules: `scraper.py` (polite async MyInstants provider), `downloader.py` (bounded-concurrency streaming download), `storage.py` (Windows-safe filename sanitization + manifest dedupe), `matcher.py` (contextual sound matching), `remotion_adapter.py` (registry + TypeScript helper export), `cli.py` (rich interactive + argparse non-interactive), plus multi-harness agent rule files.

**Tech Stack:** Python 3.12+ (runtime here is 3.14), `httpx`, `beautifulsoup4`, `rich`, `pytest`, `pytest-asyncio`.

**Spec:** Agreed in-session design (2026-11-08): RemoSound — MyInstants Bulk Sound Bank & Remotion AI Matcher.

## Global Constraints

- **Windows host**: every filename written to disk must strip or replace `\ / : * ? " < > |`, control chars, and trailing dots/spaces; never produce an empty stem (fall back to the instant id). Paths contain spaces (`C:\Users\MyBook SAGA 12\...`) — always quote.
- **Shell**: raw PowerShell is broken in this environment by a `caveman shrink` hook. Every shell command must be prefixed `cmd /c ...`; never use `&&` (use `cmd /c "a & b"`).
- **Python environment**: create and use `.venv` in the repo (`.venv\Scripts\python.exe`). Install deps there; run tests with `.venv\Scripts\python.exe -m pytest`.
- **Ethics / etiquette**: max 5 concurrent downloads, minimum 0.5s between page fetches, honor HTTP 429 with exponential backoff (0.5s, 1s, 2s, then give up after 3 retries). A `--max-concurrency` flag may lower but never raise the 5 limit.
- **Deduplication**: anything recorded in `downloads/manifest.json` is never downloaded again; partial files use a `.part` suffix and are removed on failure.
- **License**: MIT (`LICENSE`) plus an explicit third-party-audio disclaimer in `README.md`.
- **Multi-agent portability**: instruction files for `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/remosound.mdc`, and `SKILL.md` must all describe the same commands. No harness-specific code paths.
- **Naming**: package `remosound`, entrypoint `run.py`, default output root `downloads/`, manifest `downloads/manifest.json`, registry `sound-bank.json`.

## Review Focus

1. *Weird MyInstants titles* (emoji, `?`, `|`, trailing dots, 300+ chars) — must yield a valid, unique Windows filename, never a crash or silent overwrite.
2. *HTTP 429 / 5xx mid-batch* — must back off and retry, and must never leave a corrupt `.mp3` in the bank.
3. *MyInstants HTML drift* — parsing must fall back across `onmousedown="play(...)"`, `data-url`, and `<audio src>` instead of silently returning zero buttons.
4. *Empty or unmatched scene text* — the matcher must return a neutral fallback (or empty list with a clear reason), never raise.
5. *Interrupted run resumed later* — manifest must survive, finished files must be skipped, and re-running must not duplicate entries.

---

### Task 1: Repository scaffolding, license, and multi-agent rule files

**Files:**
- Create: `LICENSE`
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `README.md`
- Create: `AGENTS.md` (replace the existing starter file)
- Create: `CLAUDE.md`
- Create: `.cursor/rules/remosound.mdc`
- Create: `remosound/__init__.py`
- Create: `tests/test_environment.py`

**Interfaces:**
- Consumes: nothing.
- Produces: importable package `remosound` with `__version__ = "0.1.0"`; `.venv` with deps installed; the canonical command set that every later rule file references:
  - `python run.py` (interactive menu)
  - `python run.py --trending --limit N`
  - `python run.py --category <name> --limit N`
  - `python run.py --search "<query>" --limit N`
  - `python run.py --match "<scene text>"`
  - `python run.py --rebuild-index`
  - `.venv\Scripts\python.exe -m pytest -q`

- [ ] **Step 1: Create the virtualenv and install dependencies**

Run: `cmd /c "python -m venv .venv & .venv\Scripts\python.exe -m pip install --upgrade pip & .venv\Scripts\python.exe -m pip install httpx beautifulsoup4 rich pytest pytest-asyncio"`

- [ ] **Step 2: Write the failing environment test**

`tests/test_environment.py`:

```python
import remosound


def test_package_imports_with_version():
    assert remosound.__version__ == "0.1.0"
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_environment.py -q"`
Expected: FAIL (ModuleNotFoundError: remosound / missing version).

- [ ] **Step 4: Create the package, manifests, and license files**

- `remosound/__init__.py` with `__version__ = "0.1.0"`.
- `pyproject.toml` (setuptools, `name = "remosound"`, `requires-python = ">=3.12"`, deps `httpx>=0.27`, `beautifulsoup4>=4.12`, `rich>=13.7`; `[tool.pytest.ini_options]` with `asyncio_mode = "auto"` and `testpaths = ["tests"]`).
- `requirements.txt` listing `httpx>=0.27.0`, `beautifulsoup4>=4.12.0`, `rich>=13.7.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`.
- `LICENSE`: standard MIT text, `Copyright (c) 2026 RemoSound contributors`.
- `.gitignore`: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `downloads/`, `.superpowers/`, `*.mp3.part`.

- [ ] **Step 5: Write `README.md`, `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules/remosound.mdc`**

- `README.md`: what RemoSound is, install steps, the canonical commands above, Remotion usage (`staticFile("remosound/<category>/<file>.mp3")`), the MIT badge, and this disclaimer verbatim: "Audio files are community-uploaded content from MyInstants. RemoSound only automates retrieval. You are responsible for verifying rights before commercial use."
- `AGENTS.md`: replace the starter content with the real repo guide — canonical commands, `.venv` usage, `cmd /c` shell rule, module map (`scraper` → `downloader` → `storage` → `matcher` → `remotion_adapter` → `cli`), download ethics limits, and the rule that `downloads/` is runtime data not source.
- `CLAUDE.md`: same content as `AGENTS.md` (Claude Code reads `CLAUDE.md`); add one line telling the agent to prefer the CLI over re-implementing scraping inline.
- `.cursor/rules/remosound.mdc` with frontmatter `description: RemoSound sound-bank downloader` / `alwaysApply: true`, then the same command table and shell rule.

- [ ] **Step 6: Run the test to confirm it passes**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_environment.py -q"`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
cmd /c "git add -A & git commit -m \"feat: scaffold RemoSound with MIT license and multi-agent rule files\""
```

---

### Task 2: Storage manager, Windows filename sanitizer, and manifest deduplication

**Files:**
- Create: `remosound/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sanitize_filename(title: str, fallback: str = "sound", max_length: int = 100) -> str`
  - `class StorageManager:` constructed as `StorageManager(root: Path)`
    - `manifest: dict[str, dict]` (keyed by instant id)
    - `is_downloaded(instant_id: str) -> bool`
    - `record_download(instant_id: str, title: str, category: str, relative_path: str, tags: list[str]) -> None`
    - `category_dir(category: str) -> Path` (creates it; sanitized, lowercased, spaces → `-`)
    - `save() -> None` (atomic: write `manifest.json.tmp`, then replace)
    - `entries() -> list[dict]` (each dict: `id`, `title`, `category`, `path`, `tags`)

- [ ] **Step 1: Write the failing tests**

`tests/test_storage.py` covering:
- `sanitize_filename('What? Are: You | Kidding*/\\"<>')` → no forbidden chars, non-empty.
- `sanitize_filename('   ...   ')` → falls back to `"sound"`.
- `sanitize_filename('x' * 300, max_length=100)` → length ≤ 100.
- emoji title (`"boom 💥 sound"`) survives with a usable name.
- `StorageManager` round-trip: record one download, `save()`, rebuild from disk, `is_downloaded(id)` is True, `entries()` returns the record.
- `record_download` twice with the same id does not duplicate entries.
- `category_dir("Sound Effects")` returns `root/"sound-effects"` and the dir exists.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_storage.py -q"`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `remosound/storage.py`**

Use `re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title)`, collapse whitespace, strip trailing dots/spaces, truncate to `max_length`, fall back when empty. Manifest load tolerates missing/corrupt JSON by starting empty. `save()` writes a temp file then `os.replace`.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_storage.py -q"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cmd /c "git add remosound/storage.py tests/test_storage.py & git commit -m \"feat: add Windows-safe storage manager and manifest dedupe\""
```

---

### Task 3: Polite MyInstants scraper provider

**Files:**
- Create: `remosound/scraper.py`
- Test: `tests/test_scraper.py`
- Fixture: `tests/fixtures/myinstants_page.html`

**Interfaces:**
- Consumes: nothing (pure parsing + HTTP).
- Produces:
  - `@dataclass InstantButton(id, title, audio_url, category, page_url)`
  - `class MyInstantsScraper(client: httpx.AsyncClient | None = None, delay: float = 0.5, max_retries: int = 3)`
    - `async def fetch_page(url: str) -> str`
    - `async def close() -> None`
    - `def parse_buttons(html: str, category_hint: str = "general") -> list[InstantButton]`
    - `async def crawl_category(category: str, limit: int = 100) -> list[InstantButton]`
    - `async def crawl_search(query: str, limit: int = 100) -> list[InstantButton]`
    - `async def crawl_trending(limit: int = 100) -> list[InstantButton]`
  - URL builders as module constants: `BASE_URL`, `TRENDING_URL`, `CATEGORY_URL`, `SEARCH_URL`.

- [ ] **Step 1: Create the HTML fixture and the failing tests**

`tests/fixtures/myinstants_page.html` must include: one `.instant` card whose audio is in `onmousedown="play('/media/sounds/a.mp3')"`, one using `data-url="/media/sounds/b.mp3"`, one using `<audio src="/media/sounds/c.mp3">`, and one card with no audio reference at all (must be skipped). Include a `/instant/<slug>-<id>/` detail link.

Tests assert:
- `parse_buttons` returns exactly 3 buttons, in document order.
- `audio_url` values are absolute (`https://www.myinstants.com/media/sounds/a.mp3`).
- ids are extracted from the detail link slug (last path segment).
- `category_hint="music"` propagates to every returned button.
- `crawl_trending` (with `httpx.MockTransport` returning the fixture) stops at `limit=2` and performs no more than 1 page fetch.
- `fetch_page` retries a 429 once then succeeds (MockTransport returns 429, then 200) — assert 2 calls.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_scraper.py -q"`
Expected: FAIL.

- [ ] **Step 3: Implement `remosound/scraper.py`**

- `parse_buttons`: iterate `soup.select("div.instant")`; title from `.instant-link`; detail link from `a.instant-link[href]` or any `a[href^="/instant/"]`; audio from `onmousedown` regex `play\('([^']+\.mp3)'\)`, then `button[data-url]`, then `audio[src]`; skip cards with no audio; absolutize relative URLs with `urljoin`.
- `fetch_page`: `asyncio.sleep(self.delay)` before each request; retry on 429/5xx with `delay * 2**attempt`, max `max_retries`.
- Crawlers paginate (`?page=n`, 1-based) until `limit` reached or a page yields no buttons.
- Sortable deterministic output: preserve page order.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_scraper.py -q"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cmd /c "git add remosound/scraper.py tests/test_scraper.py tests/fixtures & git commit -m \"feat: add polite MyInstants scraper with retry and fallback selectors\""
```

---

### Task 4: Bounded-concurrency downloader

**Files:**
- Create: `remosound/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `InstantButton`, `StorageManager`.
- Produces:
  - `class BulkDownloader(storage: StorageManager, max_concurrency: int = 5, client: httpx.AsyncClient | None = None)`
    - `async def download_one(button: InstantButton) -> Path | None`
    - `async def download_batch(buttons: list[InstantButton], on_progress: Callable[[int, int, str], None] | None = None) -> list[Path]`
    - `async def close() -> None`
  - Constructor clamps `max_concurrency` to `1..5`.

- [ ] **Step 1: Write the failing tests**

Using `httpx.MockTransport` that serves bytes `b"ID3fake"`:
- `download_one` writes the file into `storage.category_dir(category)` with a sanitized name, and records it in the manifest.
- A second `download_one` for the same button returns `None` and performs no HTTP request (assert transport call count stays 1).
- A transport that returns status 500 always → `download_one` returns `None` and no `.mp3` and no `.part` file remains.
- `max_concurrency=99` clamps to 5 (assert `downloader.semaphore._value == 5` is avoided; instead assert `downloader.max_concurrency == 5`).
- `download_batch([b1, b2, b3])` returns 3 paths and calls `on_progress` 3 times with increasing completed counts.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_downloader.py -q"`
Expected: FAIL.

- [ ] **Step 3: Implement `remosound/downloader.py`**

- `asyncio.Semaphore(self.max_concurrency)` around each download.
- Stream to `path.with_suffix(path.suffix + ".part")`, then `os.replace`; on exception delete the `.part`.
- Filename: `f"{sanitize_filename(button.title, fallback=button.id)}-{button.id}.mp3"` truncated so the full name stays under 150 chars.
- Skip when `storage.is_downloaded(button.id)` or the target file already exists.
- `record_download` after the final rename.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_downloader.py -q"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cmd /c "git add remosound/downloader.py tests/test_downloader.py & git commit -m \"feat: add bounded-concurrency downloader with part-file safety\""
```

---

### Task 5: Contextual sound matcher and Remotion adapter

**Files:**
- Create: `remosound/matcher.py`
- Create: `remosound/remotion_adapter.py`
- Test: `tests/test_matcher.py`
- Test: `tests/test_remotion_adapter.py`

**Interfaces:**
- Consumes: `StorageManager.entries()`.
- Produces:
  - `class ContextualSoundMatcher(entries: list[dict])`
    - `match(context_text: str, top_k: int = 1) -> list[dict]` — each result `{"id","title","category","path","score"}`, sorted by score desc then title.
  - `remosound/matcher.py` module constant `CUE_MAP: dict[str, list[str]]` (Indonesian + English cue words → search terms), at minimum: `muncul/tiba-tiba/pop` → `["pop","appear","whoosh"]`, `kaget/scream/jumpscare` → `["scream","screamers","boom"]`, `menang/juara/victory` → `["victory","fanfare","applause","win"]`, `gagal/salah/fail` → `["fail","sadtrombone","bruh","wrong"]`, `klik/tombol/click` → `["click","button","tick"]`, `uang/koin/coin` → `["coin","cash","money"]`.
  - `remosound/remotion_adapter.py`
    - `export_registry(entries: list[dict], output_path: Path) -> Path` — writes `sound-bank.json` (`{"version": 1, "sounds": [...]}` with `id,title,category,file,src` where `src` is the Remotion-safe relative path `remosound/<category>/<file>`).
    - `export_typescript_helper(registry_path: Path, output_path: Path) -> Path` — writes `sound-bank.ts` exporting `type SoundEntry`, `const SOUND_BANK: SoundEntry[]`, `function getSound(id: string): SoundEntry | undefined`, `function findSounds(query: string): SoundEntry[]`.
    - `stage_into_remotion(registry_path: Path, sound_root: Path, remotion_public_dir: Path) -> Path` — copies audio into `<public>/remosound/<category>/` and writes `<public>/sound-bank.json`; returns the copied registry path.

- [ ] **Step 1: Write the failing matcher tests**

Entries fixture with titles like `"Vine Boom"`, `"Win Fanfare"`, `"Sad Trombone"`, `"Click Button"`, `"Camera Shutter"`.
- `match("tiba-tiba muncul monster")` → first result title contains `boom` or `pop`/`whoosh` family (assert `ids` are a subset of the expected set).
- `match("kamu berhasil menjadi juara")` → top result is `Win Fanfare`.
- `match("rencana tersebut gagal total")` → `Sad Trombone`.
- `match("")` → returns `[]`.
- `match("zzz qqq", top_k=2)` → returns `[]` (no spurious match) or fewer than 2 items — assert it does not raise.
- `match("win", top_k=5)` returns at most 5 and scores non-increasing.

- [ ] **Step 2: Run matcher tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_matcher.py -q"`
Expected: FAIL.

- [ ] **Step 3: Implement `remosound/matcher.py`**

Tokenize lowercased text; expand cue words from `CUE_MAP`; score = 3× title token match + 2× tag match + 1× category match; require score > 0.

- [ ] **Step 4: Run matcher tests to confirm they pass**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_matcher.py -q"`
Expected: all pass.

- [ ] **Step 5: Write the failing adapter tests**

- `export_registry` writes valid JSON with `version == 1` and `src == "remosound/sfx/title-id.mp3"`.
- `export_typescript_helper` output contains `export function getSound(` and `export const SOUND_BANK`.
- `stage_into_remotion` copies the file and writes the registry into the given public dir.

- [ ] **Step 6: Run adapter tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_remotion_adapter.py -q"`
Expected: FAIL.

- [ ] **Step 7: Implement `remosound/remotion_adapter.py` and re-run**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_remotion_adapter.py -q"`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cmd /c "git add remosound/matcher.py remosound/remotion_adapter.py tests/test_matcher.py tests/test_remotion_adapter.py & git commit -m \"feat: add contextual sound matcher and Remotion registry adapter\""
```

---

### Task 6: CLI — interactive and flag-driven

**Files:**
- Create: `remosound/cli.py`
- Create: `run.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `MyInstantsScraper`, `BulkDownloader`, `StorageManager`, `ContextualSoundMatcher`, adapter functions.
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `async def main_async(argv: list[str] | None = None) -> int`
  - `def main(argv: list[str] | None = None) -> int`
  - Flags: `--trending`, `--category NAME`, `--search QUERY`, `--match TEXT`, `--limit N` (default 100), `--out DIR` (default `downloads`), `--rebuild-index`, `--remotion-public DIR`, `--max-concurrency N` (default 5), `--dry-run`.
  - No mode flags → interactive `rich` menu with 4 choices (Trending / Category / Search / Match scene text).

- [ ] **Step 1: Write the failing tests**

- `build_parser().parse_args(["--category", "music", "--limit", "5"])` → `args.category == "music"`, `args.limit == 5`.
- Mutually exclusive mode flags raise `SystemExit` when combined.
- `--limit 0` and negative limits rejected (`SystemExit`).
- `--max-concurrency 99` parses but is clamped to 5 by the downloader (assert via `main_async` with mocked scraper? no — assert `build_parser` default is 5 and document clamping; test the clamp in Task 4).
- `main_async(["--dry-run","--category","music","--limit","2"])` with a monkeypatched `MyInstantsScraper.crawl_category` returning 2 fake buttons prints plans and returns `0` without writing files.
- `--match "win"` with a temp manifest prints matches in JSON and returns `0`.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_cli.py -q"`
Expected: FAIL.

- [ ] **Step 3: Implement `remosound/cli.py` and `run.py`**

`run.py` is a 4-line launcher: `from remosound.cli import main; raise SystemExit(main())`. Use `rich.progress` for download progress and a `rich.table` summary. Keep interactive input isolated in `interactive_menu()` so tests never touch stdin. Wire `--rebuild-index` to `export_registry` + `export_typescript_helper` into `--out`, and `--remotion-public` to `stage_into_remotion` after a successful batch.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest tests/test_cli.py -q"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cmd /c "git add remosound/cli.py run.py tests/test_cli.py & git commit -m \"feat: add interactive and flag-driven CLI\""
```

---

### Task 7: Portable agent skill, docs, and final verification

**Files:**
- Create: `SKILL.md`
- Create: `.cursor/rules/remosound.mdc` (only if Task 1 did not already create it — verify, then update)
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: the canonical command set from Task 1.
- Produces: a harness-agnostic `SKILL.md` that any of OpenCode, Claude Code, Cursor, or Antigravity can follow; final green test suite.

- [ ] **Step 1: Write `SKILL.md`**

Frontmatter: `name: remosound`, `description: Bulk-download MyInstants sounds into a local sound bank and match sounds to video scene text for Remotion.` Body: when to use, the canonical command table, the ethics limits (5 concurrency, 0.5s delay, no `--limit` above 5000 per run), the sound-bank layout, and the Remotion usage snippet with `staticFile("remosound/<category>/<file>.mp3")`.

- [ ] **Step 2: Reconcile the rule files**

Confirm `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules/remosound.mdc` all list the same canonical commands and the `cmd /c` shell rule. Fix any drift. Add one line to each: "Do not re-implement scraping inline; call `run.py`."

- [ ] **Step 3: Update `README.md`**

Add a "Remotion" section: `--remotion-public <project>/public`, the `sound-bank.ts` helper (`getSound`, `findSounds`), and an `<Audio src={staticFile(getSound("vine-boom")!.src)} />` example. Add "Supported harnesses: OpenCode, Claude Code, Cursor, Antigravity" and how each discovers its rule file.

- [ ] **Step 4: Run the full suite**

Run: `cmd /c ".venv\Scripts\python.exe -m pytest -q"`
Expected: all tests pass, no warnings that indicate un-awaited coroutines.

- [ ] **Step 5: Smoke-test the CLI surface**

Run: `cmd /c ".venv\Scripts\python.exe run.py --help"`
Expected: exit 0 and all flags listed.

- [ ] **Step 6: Commit**

```bash
cmd /c "git add -A & git commit -m \"docs: add portable SKILL.md and Remotion integration guide\""
```
