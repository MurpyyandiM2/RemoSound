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
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Prompt
from rich.table import Table

from remosound.downloader import BulkDownloader
from remosound.matcher import ContextualSoundMatcher
from remosound.remotion_adapter import (
    HELPER_NAME,
    REGISTRY_NAME,
    REMOTION_PREFIX,
    registry_paths,
    stage_into_remotion,
    write_index,
)
from remosound.providers import DEFAULT_PROVIDER, providers
from remosound.scraper import InstantButton, Scraper
from remosound.storage import StorageManager
from remosound.web import serve as serve_web

DEFAULT_OUT = "downloads"
DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
DEFAULT_CONCURRENCY = 5
MATCH_LIMIT = 5

SAMPLE_SIZE = 5
PROGRESS_TITLE_WIDTH = 42


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the whole CLI surface."""

    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Bulk-download sounds into a local bank and match them to Remotion scenes.",
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
    mode.add_argument(
        "--web",
        action="store_true",
        help="open the local RemoSound web studio (soundboard, downloader, Remotion matcher)",
    )
    mode.add_argument(
        "--delete",
        metavar="ID",
        dest="delete",
        type=_non_empty,
        help="delete one sound from the bank and manifest by instant ID",
    )

    parser.add_argument(
        "--provider",
        choices=sorted(providers()),
        default=DEFAULT_PROVIDER,
        help=f"sound source (default {DEFAULT_PROVIDER}); more can be added as plugins, see README",
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
        "--project",
        type=Path,
        metavar="DIR",
        help="Remotion project folder: keep the bank inside its public/ so downloads are usable with no copying",
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
    if args.project is not None:
        if args.out != Path(DEFAULT_OUT):
            parser.error("--project already chooses the bank folder; drop --out")
        if args.remotion_public is not None:
            parser.error("--project already keeps the bank in the project; drop --remotion-public")
        if not (args.project / "package.json").is_file():
            parser.error(f"--project: {args.project} is not a Remotion project (no package.json)")
        args.out = args.project / "public" / REMOTION_PREFIX
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
    """Ask for one operation, then run it on the parsed flags.

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
    menu.add_row("5", "Rebuild the Remotion index")
    menu.add_row("6", "Open the web studio")
    console.print(menu)

    choice = Prompt.ask(
        "Choose", choices=["1", "2", "3", "4", "5", "6"], default="1", console=console
    )
    if choice == "1":
        mode = {"trending": True}
    elif choice == "2":
        mode = {"category": _ask_category(console, args.provider)}
    elif choice == "3":
        mode = {"search": _ask("Query", console)}
    elif choice == "4":
        mode = {"match": _ask("Scene text", console)}
    elif choice == "5":
        mode = {"rebuild_index": True}
    else:
        mode = {"web": True}

    selected = argparse.Namespace(**vars(args))
    for field, value in mode.items():
        setattr(selected, field, value)
    return await _dispatch(selected, console)


def _ask_category(console: Console, provider: str = DEFAULT_PROVIDER) -> str:
    """Show the provider's categories and return the chosen slug.

    Picking a number is the common path; ``0`` keeps free-text available for a
    slug the site adds after this table was written.
    """

    categories = providers()[provider].categories
    table = Table(title=f"{providers()[provider].name} categories", show_header=False, box=None, pad_edge=False)
    for _ in range(4):
        table.add_column()
    entries = [(str(index), label) for index, (label, _slug) in enumerate(categories, start=1)]
    half = (len(entries) + 1) // 2
    for offset in range(half):
        row: list[str] = []
        for entry in (entries[offset], entries[offset + half] if offset + half < len(entries) else None):
            row.extend(entry if entry is not None else ("", ""))
        table.add_row(*row)
    table.add_row("0", "Type a category name or slug")
    console.print(table)

    choices = [entry[0] for entry in entries] + ["0"]
    choice = Prompt.ask("Category", choices=choices, default="1", console=console)
    if choice == "0":
        return _ask("Category name or slug", console)
    return categories[int(choice) - 1][1]


async def _dispatch(args: argparse.Namespace, console: Console) -> int:
    """Run the operation named by ``args``."""

    if args.web:
        return _run_web(args, console)
    if getattr(args, "delete", None) is not None:
        return _run_delete(args, console)
    if args.match is not None:
        return _run_match(args, console)
    if args.rebuild_index:
        return await _run_rebuild(args, console)
    if args.trending or args.category is not None or args.search is not None:
        return await _run_batch(args, console)
    return await interactive_menu(args, console)


def _run_delete(args: argparse.Namespace, console: Console) -> int:
    """Delete a sound from the bank and manifest."""

    storage = StorageManager(args.out)
    sound_id = args.delete
    if not storage.is_downloaded(sound_id):
        _warn(f"Sound {sound_id} is not in the bank at {args.out}.")
        return 1

    success = storage.delete_sound(sound_id)
    if success:
        console.print(f"Deleted sound [bold]{escape(sound_id)}[/bold] from the bank.")
        return 0
    _error(f"Could not delete sound {sound_id}.")
    return 1


def _run_web(args: argparse.Namespace, console: Console) -> int:
    """Serve the local web studio until the user stops it."""

    try:
        serve_web(args.out, console=console, refresh_index=args.project is not None)
    except OSError as error:
        _error(f"Could not start the web studio: {error}")
        return 1
    return 0


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
    # defaults to cp1252, and remote titles routinely contain emoji.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    return 0


# --- rebuild index ---------------------------------------------------------


async def _run_rebuild(args: argparse.Namespace, console: Console) -> int:
    """Write the registry and TypeScript helper beside the bank root."""

    storage = StorageManager(args.out)
    registry, helper = registry_paths(args.out)
    if args.dry_run:
        console.print(f"Dry run: would write {escape(str(registry))} and {escape(str(helper))}.")
        if args.remotion_public is not None:
            console.print(f"Dry run: would stage into {escape(str(args.remotion_public))}.")
        return 0

    try:
        write_index(storage, args.out)
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

    scraper = providers()[getattr(args, "provider", DEFAULT_PROVIDER)].scraper()
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
    _print_summary(downloaded, skipped, failed, console, pending, storage, args)

    if args.remotion_public is not None:
        if failed:
            console.print("Skipped staging because some downloads failed.")
        else:
            registry, helper = registry_paths(args.out)
            try:
                write_index(storage, args.out)
                staged = stage_into_remotion(registry, args.out, args.remotion_public)
                console.print(f"Staged the bank into {escape(str(staged.parent))}.")
            except (OSError, ValueError) as error:
                _error(f"Could not stage the bank into Remotion: {error}")
                return 1

    if args.project is not None:
        # The bank already sits in the project, so only the lookup files need refreshing.
        try:
            write_index(storage, args.out)
        except (OSError, ValueError) as error:
            _error(f"Could not update the Remotion index: {error}")
            return 1

    return 1 if failed else 0


async def _collect(scraper: Scraper, args: argparse.Namespace) -> list[InstantButton]:
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
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.fields[current]}", style="dim"),
    )
    try:
        with Progress(*columns, console=console) as progress:
            task = progress.add_task("Downloading", total=len(pending), current="")

            def on_progress(completed: int, total: int, _title: str) -> None:
                progress.update(
                    task,
                    completed=completed,
                    total=total,
                    current=_fit_title(_title),
                )

            await downloader.download_batch(pending, on_progress=on_progress)
    finally:
        await downloader.close()


def _fit_title(title: str, width: int = PROGRESS_TITLE_WIDTH) -> str:
    """Trim a remote title to one short, single-line progress field."""

    text = " ".join((title or "").split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


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
    if args.remotion_public is not None:
        console.print(f"Dry run: would stage into {escape(str(args.remotion_public))}.")


def _print_summary(
    downloaded: int,
    skipped: list[InstantButton],
    failed: list[InstantButton],
    console: Console,
    pending: list[InstantButton],
    storage: StorageManager,
    args: argparse.Namespace,
) -> None:
    """Print totals, the bank location, a sample of new files, and next steps."""

    summary = Table(show_header=False, box=None)
    summary.add_column("Outcome", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Downloaded", str(downloaded))
    summary.add_row("Already in the bank", str(len(skipped)))
    summary.add_row("Failed", str(len(failed)))
    summary.add_row("Bank", escape(str(Path(storage.root).resolve())))
    console.print(Panel(summary, title="Download summary", border_style="green"))

    fresh = _fresh_entries(pending, storage)
    if fresh:
        sample = Table(title="New this run")
        sample.add_column("Title")
        sample.add_column("File", style="dim")
        for title, relative_path in fresh[:SAMPLE_SIZE]:
            sample.add_row(escape(title), escape(relative_path))
        console.print(sample)
        if len(fresh) > SAMPLE_SIZE:
            console.print(f"[dim]…and {len(fresh) - SAMPLE_SIZE} more.[/dim]")

    if failed:
        failures = Table(title="Failed downloads")
        failures.add_column("Id")
        failures.add_column("Title")
        for button in failed:
            failures.add_row(escape(button.id), escape(button.title or button.id))
        console.print(failures)

    if args.project is not None:
        out_flag = f' --project "{args.project}"'
    else:
        out_flag = f' --out "{args.out}"' if str(args.out) != DEFAULT_OUT else ""
    hints = (
        f'[bold]Match a scene[/bold]   python run.py --match "boom"{escape(out_flag)}\n'
        f"[bold]Open the studio[/bold] python run.py --web{escape(out_flag)}\n"
        f"[bold]Rebuild index[/bold]   python run.py --rebuild-index{escape(out_flag)}"
    )
    console.print(Panel(hints, title="Next steps", border_style="cyan"))


def _fresh_entries(
    pending: list[InstantButton], storage: StorageManager
) -> list[tuple[str, str]]:
    """The manifest records written by this run, in pending order.

    A button is in ``pending`` because it was missing from the manifest, so an
    entry that exists now means the file landed — adopting an interrupted
    download counts too, because the user still gained a usable file.
    """

    fresh: list[tuple[str, str]] = []
    for button in pending:
        entry = storage.manifest.get(button.id)
        if entry is None:
            continue
        fresh.append(
            (
                str(entry.get("title") or button.title or button.id),
                str(entry.get("path") or ""),
            )
        )
    return fresh


# --- shared helpers --------------------------------------------------------


def _configure_stdout() -> None:
    """Make stdout UTF-8 so redirected output never fails on remote titles.

    Windows otherwise hands a redirected stream cp1252, which cannot encode the
    emoji that appear in real remote titles. Test doubles without a
    ``reconfigure`` method are left alone.
    """

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return


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
