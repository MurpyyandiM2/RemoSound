"""Command-line interface for RemoSound.

Two ways in: flags for scripted use and an interactive menu for humans. Both
paths converge on :func:`_dispatch`, which owns the four bank operations below.

======================  ===================================================
Operation               Behaviour
======================  ===================================================
batch (trending /       scrape a listing, download into the bank, optionally
category / search)      rebuild the index and stage it into Remotion
``--match``             rank the bank against scene text, print JSON
``--rebuild-index``     write ``sound-bank.json`` and ``sound-bank.ts``
======================  ===================================================
Exit codes: ``0`` success, ``1`` a runtime failure (network, disk, bad
registry), ``2`` a usage error (argparse).

The registry files live beside the bank root, so the default ``--out
downloads`` produces ``downloads/`` and ``sound-bank.json`` in the project
root, matching the documented bank layout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from remosound.downloader import BulkDownloader
from remosound.matcher import ContextualSoundMatcher
from remosound.remotion_adapter import (
    HELPER_NAME,
    REGISTRY_NAME,
    export_registry,
    export_typescript_helper,
    stage_into_remotion,
)
from remosound.scraper import InstantButton, MyInstantsScraper
from remosound.storage import StorageManager

DEFAULT_OUT = "downloads"
DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
DEFAULT_CONCURRENCY = 5
MATCH_LIMIT = 5


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the whole CLI surface."""

    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Download MyInstants sounds into a local bank and match them to Remotion scenes.",
        epilog=(
            "Without a mode flag an interactive menu opens. "
            "Exit codes: 0 success, 1 runtime failure, 2 usage error."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--trending", action="store_true", help="download the trending listing")
    mode.add_argument("--category", metavar="NAME", type=_non_empty, help="download one category")
    mode.add_argument("--search", metavar="QUERY", type=_non_empty, help="download search results")
    mode.add_argument(
        "--match",
        metavar="TEXT",
        dest="match",
        type=_non_empty,
        help=f"rank the bank against scene text and print JSON (top {MATCH_LIMIT})",
    )
    mode.add_argument(
        "--rebuild-index",
        action="store_true",
        help="rebuild sound-bank.json and sound-bank.ts beside the bank root",
    )

    parser.add_argument(
        "--limit",
        type=_limit,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"how many sounds to collect (default {DEFAULT_LIMIT}, capped at {MAX_LIMIT} per run)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT),
        metavar="DIR",
        help=f"bank root (default {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--remotion-public",
        type=Path,
        metavar="DIR",
        help="copy the bank into a Remotion public/ directory after a successful run",
    )
    parser.add_argument(
        "--max-concurrency",
        type=_concurrency,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=f"parallel downloads (default {DEFAULT_CONCURRENCY}; values above 5 are capped at 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without downloading or writing any file",
    )
    return parser


async def main_async(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run one command, returning its exit code."""

    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.remotion_public is not None and not _writes_bank(args):
        parser.error("--remotion-public needs a batch download or --rebuild-index")
    return await _dispatch(args, Console())


def main(argv: list[str] | None = None) -> int:
    """Synchronous entry point used by ``run.py``."""

    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        return 130


async def interactive_menu(args: argparse.Namespace, console: Console) -> int:
    """Ask for one of four operations, then run it on the parsed flags.

    ``args`` carries the options the user already supplied (``--dry-run``,
    ``--out``, ``--limit``, ``--max-concurrency``); the menu only fills in the
    missing mode, so the interactive path behaves like the flag-driven one.
    Kept separate from argument parsing so tests never need stdin.
    """

    menu = Table(title="RemoSound", show_header=False)
    menu.add_column("Option", justify="right")
    menu.add_column("Action")
    menu.add_row("1", "Trending sounds")
    menu.add_row("2", "Browse a category")
    menu.add_row("3", "Search")
    menu.add_row("4", "Match scene text")
    console.print(menu)

    choice = Prompt.ask("Choose", choices=["1", "2", "3", "4"], default="1", console=console)
    if choice == "1":
        mode = {"trending": True}
    elif choice == "2":
        mode = {"category": _ask("Category", console)}
    elif choice == "3":
        mode = {"search": _ask("Query", console)}
    else:
        mode = {"match": _ask("Scene text", console)}

    selected = argparse.Namespace(**vars(args))
    for field, value in mode.items():
        setattr(selected, field, value)
    return await _dispatch(selected, console)


async def _dispatch(args: argparse.Namespace, console: Console) -> int:
    """Run the operation named by ``args``."""

    if args.match is not None:
        return _run_match(args, console)
    if args.rebuild_index:
        return await _run_rebuild(args, console)
    if args.trending or args.category is not None or args.search is not None:
        return await _run_batch(args, console)
    return await interactive_menu(args, console)


# --- match -----------------------------------------------------------------


def _run_match(args: argparse.Namespace, console: Console) -> int:
    """Print ranked matches as JSON on stdout, diagnostics on stderr."""

    storage = StorageManager(args.out)
    entries = storage.entries()
    matches = ContextualSoundMatcher(entries).match(args.match, top_k=MATCH_LIMIT)
    if not entries:
        _warn(f"The bank at {args.out} is empty; run a download first.")
    elif not matches:
        _warn(f"No sound in {args.out} matches this scene text.")

    payload = {"context": args.match, "matches": matches}
    # ASCII-only output keeps this stream-safe: on Windows a redirected stdout
    # defaults to cp1252, and MyInstants titles routinely contain emoji.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    return 0


# --- rebuild index ---------------------------------------------------------


async def _run_rebuild(args: argparse.Namespace, console: Console) -> int:
    """Write the registry and TypeScript helper beside the bank root."""

    storage = StorageManager(args.out)
    registry, helper = _registry_paths(args.out)
    if args.dry_run:
        console.print(f"Dry run: would write {escape(str(registry))} and {escape(str(helper))}.")
        if args.remotion_public is not None:
            console.print(f"Dry run: would stage into {escape(str(args.remotion_public))}.")
        return 0

    try:
        _write_index(storage, registry, helper)
        console.print(f"Wrote {escape(str(registry))} and {escape(str(helper))}.")
        if args.remotion_public is not None:
            staged = stage_into_remotion(registry, args.out, args.remotion_public)
            console.print(f"Staged the bank into {escape(str(staged.parent))}.")
    except (OSError, ValueError) as error:
        _error(f"Could not rebuild the index: {error}")
        return 1
    return 0


# --- batch downloads -------------------------------------------------------


async def _run_batch(args: argparse.Namespace, console: Console) -> int:
    """Scrape one listing, download it, and report what happened."""

    storage = StorageManager(args.out)

    scraper = MyInstantsScraper()
    try:
        buttons = await _collect(scraper, args)
    except httpx.HTTPError as error:
        _error(f"Could not fetch sounds: {error}")
        return 1
    finally:
        await scraper.close()

    if not buttons:
        console.print("No sounds found for that listing.")
        return 0

    # One record per instant id; the downloader collapses duplicates too, but
    # counting them once here keeps the plan and the summary truthful.
    unique = list({button.id: button for button in buttons}.values())
    skipped = [button for button in unique if storage.is_downloaded(button.id)]
    pending = [button for button in unique if not storage.is_downloaded(button.id)]

    if args.dry_run:
        _print_plan(pending, skipped, console, args)
        return 0

    if pending:
        await _download(pending, storage, args, console)
    else:
        console.print("Nothing new to download.")

    # A download that returns no path is either "already owned" or "failed";
    # only the manifest tells them apart.
    failed = [button for button in pending if not storage.is_downloaded(button.id)]
    downloaded = len(pending) - len(failed)
    _print_summary(downloaded, skipped, failed, console)

    if args.remotion_public is not None:
        if failed:
            console.print("Skipped staging because some downloads failed.")
        else:
            registry, helper = _registry_paths(args.out)
            try:
                _write_index(storage, registry, helper)
                staged = stage_into_remotion(registry, args.out, args.remotion_public)
                console.print(f"Staged the bank into {escape(str(staged.parent))}.")
            except (OSError, ValueError) as error:
                _error(f"Could not stage the bank into Remotion: {error}")
                return 1

    return 1 if failed else 0


async def _collect(scraper: MyInstantsScraper, args: argparse.Namespace) -> list[InstantButton]:
    """Crawl the listing selected by ``args``."""

    if args.trending:
        return await scraper.crawl_trending(args.limit)
    if args.category is not None:
        return await scraper.crawl_category(args.category, args.limit)
    return await scraper.crawl_search(args.search, args.limit)


async def _download(
    pending: list[InstantButton],
    storage: StorageManager,
    args: argparse.Namespace,
    console: Console,
) -> None:
    """Download ``pending`` with a progress bar and a bounded concurrency."""

    downloader = BulkDownloader(storage, max_concurrency=args.max_concurrency)
    columns = (
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
    )
    try:
        with Progress(*columns, console=console) as progress:
            task = progress.add_task("Downloading", total=len(pending))

            def on_progress(completed: int, total: int, _title: str) -> None:
                progress.update(task, completed=completed, total=total)

            await downloader.download_batch(pending, on_progress=on_progress)
    finally:
        await downloader.close()


def _print_plan(
    pending: list[InstantButton],
    skipped: list[InstantButton],
    console: Console,
    args: argparse.Namespace,
) -> None:
    """Print what a real run would download, without touching the disk."""

    if pending:
        plan = Table(title=f"Plan for {escape(str(args.out))}")
        plan.add_column("#", justify="right")
        plan.add_column("Title")
        plan.add_column("Category")
        plan.add_column("Id")
        for index, button in enumerate(pending, start=1):
            plan.add_row(
                str(index),
                escape(button.title or button.id),
                escape(button.category),
                escape(button.id),
            )
        console.print(plan)

    console.print(
        f"Dry run: would download {len(pending)} sound(s); "
        f"{len(skipped)} already in the bank; no files written."
    )


def _print_summary(
    downloaded: int,
    skipped: list[InstantButton],
    failed: list[InstantButton],
    console: Console,
) -> None:
    """Print the batch totals and, when needed, the failed ids."""

    summary = Table(title="Results", show_header=False)
    summary.add_column("Outcome")
    summary.add_column("Count", justify="right")
    summary.add_row("Downloaded", str(downloaded))
    summary.add_row("Already in the bank", str(len(skipped)))
    summary.add_row("Failed", str(len(failed)))
    console.print(summary)

    if failed:
        failures = Table(title="Failed downloads")
        failures.add_column("Id")
        failures.add_column("Title")
        for button in failed:
            failures.add_row(escape(button.id), escape(button.title or button.id))
        console.print(failures)


# --- shared helpers --------------------------------------------------------


def _configure_stdout() -> None:
    """Make stdout UTF-8 so redirected output never fails on remote titles.

    Windows otherwise hands a redirected stream cp1252, which cannot encode the
    emoji that appear in real MyInstants titles. Test doubles without a
    ``reconfigure`` method are left alone.
    """

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return


def _registry_paths(out: Path) -> tuple[Path, Path]:
    """Registry and helper paths beside the bank root (default: project root)."""

    parent = Path(out).parent
    return parent / REGISTRY_NAME, parent / HELPER_NAME


def _write_index(storage: StorageManager, registry: Path, helper: Path) -> None:
    """Write ``sound-bank.json`` and then ``sound-bank.ts`` from the manifest."""

    export_registry(storage.entries(), registry)
    export_typescript_helper(registry, helper)


def _writes_bank(args: argparse.Namespace) -> bool:
    """Whether ``args`` names an operation that produces bank audio."""

    return (
        args.trending
        or args.category is not None
        or args.search is not None
        or args.rebuild_index
    )


def _ask(prompt: str, console: Console) -> str:
    """Prompt until the answer is not blank."""

    while True:
        value = Prompt.ask(prompt, console=console).strip()
        if value:
            return value
        console.print("[yellow]Please enter a value.[/yellow]")


def _limit(value: str) -> int:
    """A positive limit, silently capped so one run cannot request more."""

    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"limit must be an integer, got {value!r}") from error
    if number < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return min(number, MAX_LIMIT)


def _concurrency(value: str) -> int:
    """A positive concurrency; the downloader owns the upper clamp at 5."""

    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"max-concurrency must be an integer, got {value!r}"
        ) from error
    if number < 1:
        raise argparse.ArgumentTypeError("max-concurrency must be at least 1")
    return number


def _non_empty(value: str) -> str:
    """A stripped, non-empty piece of user text."""

    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("value must not be empty")
    return text


def _warn(message: str) -> None:
    Console(stderr=True).print(f"[yellow]{escape(message)}[/yellow]")


def _error(message: str) -> None:
    Console(stderr=True).print(f"[red]{escape(message)}[/red]")
