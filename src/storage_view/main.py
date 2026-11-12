"""CLI/TUI/GUI entrypoint router for Storage View."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

import typer

from storage_view import __version__
from storage_view.core.scanner import ScanConfig
from storage_view.core.utils import parse_size

app = typer.Typer(
    name="storage-view",
    help="High-performance open-source disk space analyzer (GUI + TUI + Headless CLI).",
    add_completion=False,
    no_args_is_help=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"storage-view {__version__}")
        raise typer.Exit(0)


def _has_pyside6() -> bool:
    return importlib.util.find_spec("PySide6") is not None


def _is_interactive_tty() -> bool:
    return bool(sys.stdout and sys.stdout.isatty() and sys.stdin and sys.stdin.isatty())


@app.command()
def cli(
    path: Optional[Path] = typer.Argument(
        None,
        help="Target directory to analyze. Defaults to current directory (or drive picker in GUI).",
    ),
    gui: bool = typer.Option(
        False,
        "--gui",
        help="Force launch the desktop GUI (PySide6).",
    ),
    tui: bool = typer.Option(
        False,
        "--tui",
        help="Force launch the interactive terminal TUI (ncdu-style).",
    ),
    headless: bool = typer.Option(
        False,
        "--headless",
        "--cli",
        help="Run in non-interactive headless CLI mode and print a summary table.",
    ),
    top: Optional[int] = typer.Option(
        None,
        "--top",
        "-n",
        min=1,
        help="Show the Top N largest entries in headless CLI mode.",
    ),
    min_size: Optional[str] = typer.Option(
        None,
        "--min-size",
        "-m",
        help="Minimum item size filter (e.g. '100M', '1.5G', '512K').",
    ),
    entry_type: str = typer.Option(
        "all",
        "--type",
        help="Filter entry types in headless mode: all, files, or dirs.",
    ),
    sort_by: str = typer.Option(
        "size",
        "--sort",
        help="Sort entries by: size, name, files, or mtime.",
    ),
    max_depth: Optional[int] = typer.Option(
        None,
        "--max-depth",
        min=1,
        help="Limit reporting/export depth.",
    ),
    ext: list[str] = typer.Option(
        [],
        "--ext",
        help="Filter by file extension (repeatable, e.g. --ext .log --ext .tmp).",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        "-e",
        help="Glob pattern to exclude during scan (repeatable).",
    ),
    hidden: bool = typer.Option(
        True,
        "--hidden/--no-hidden",
        help="Include or skip hidden files and directories.",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Follow symbolic links (cycle-protected via device/inode tracking).",
    ),
    one_file_system: bool = typer.Option(
        False,
        "--one-file-system",
        "-x",
        help="Do not cross filesystem/mount boundaries.",
    ),
    workers: int = typer.Option(
        0,
        "--workers",
        "-w",
        min=0,
        help="Scanner worker threads (0 = auto).",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Print scan results as JSON to stdout (implies headless mode).",
    ),
    output_csv: bool = typer.Option(
        False,
        "--csv",
        help="Print scan results as CSV to stdout (implies headless mode).",
    ),
    export_json_path: Optional[Path] = typer.Option(
        None,
        "--export-json",
        help="Write scan results to a JSON file (implies headless mode).",
    ),
    export_csv_path: Optional[Path] = typer.Option(
        None,
        "--export-csv",
        help="Write scan results to a CSV file (implies headless mode).",
    ),
    show_errors: bool = typer.Option(
        False,
        "--show-errors",
        help="Print inaccessible paths encountered during scan.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress live progress output on stderr.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Analyze disk usage using GUI, interactive TUI, or headless CLI."""
    del version  # handled eagerly by callback

    mode_flags = sum([bool(gui), bool(tui), bool(headless)])
    if mode_flags > 1:
        typer.echo(
            "Error: --gui, --tui, and --headless/--cli are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(2)

    normalized_type = entry_type.lower()
    if normalized_type not in {"all", "files", "dirs"}:
        typer.echo("Error: --type must be one of: all, files, dirs.", err=True)
        raise typer.Exit(2)

    normalized_sort = sort_by.lower()
    if normalized_sort not in {"size", "name", "files", "mtime"}:
        typer.echo("Error: --sort must be one of: size, name, files, mtime.", err=True)
        raise typer.Exit(2)

    try:
        min_bytes = parse_size(min_size) if min_size is not None else 0
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2)

    has_cli_query_flags = any(
        [
            headless,
            top is not None,
            min_size is not None,
            entry_type != "all",
            max_depth is not None,
            bool(ext),
            output_json,
            output_csv,
            export_json_path is not None,
            export_csv_path is not None,
            show_errors,
            quiet,
        ]
    )

    config = ScanConfig(
        max_workers=workers,
        follow_symlinks=follow_symlinks,
        one_file_system=one_file_system,
        include_hidden=hidden,
        exclude_patterns=tuple(exclude),
    )

    # Determine effective execution mode:
    # 1. Explicit --gui / --tui / --headless
    # 2. Query flags (--top, --min-size, --json, --csv, etc.) -> headless CLI
    # 3. Default by platform: Windows -> GUI (when PySide6 installed), Linux/macOS -> TUI (if TTY)
    if gui:
        selected_mode = "gui"
    elif tui:
        selected_mode = "tui"
    elif has_cli_query_flags:
        selected_mode = "headless"
    elif sys.platform == "win32" and _has_pyside6():
        selected_mode = "gui"
    elif _is_interactive_tty():
        selected_mode = "tui"
    else:
        selected_mode = "headless"

    if selected_mode == "gui":
        if not _has_pyside6():
            typer.echo(
                "PySide6 is required for GUI mode. Install with: pip install 'storage-view[gui]'",
                err=True,
            )
            raise typer.Exit(1)
        from storage_view.gui import run_gui

        target_str = os.path.abspath(os.fspath(path)) if path is not None else None
        exit_code = run_gui(initial_path=target_str)
        raise typer.Exit(exit_code)

    target_path = Path(path) if path is not None else Path.cwd()
    if not target_path.exists():
        typer.echo(f"Error: Path does not exist: {target_path}", err=True)
        raise typer.Exit(1)
    if not target_path.is_dir():
        typer.echo(f"Error: Path is not a directory: {target_path}", err=True)
        raise typer.Exit(1)

    if selected_mode == "tui":
        from storage_view.cli.tui_app import run_tui

        run_tui(target_path, config=config)
        return

    from storage_view.cli.reporter import run_headless_scan

    run_headless_scan(
        target_path,
        config=config,
        top_n=top if top is not None else 20,
        min_size=min_bytes,
        target_type=normalized_type,
        sort_by=normalized_sort,
        max_depth=max_depth,
        extensions=ext,
        exclude_patterns=exclude,
        output_json=output_json,
        output_csv=output_csv,
        export_json_path=export_json_path,
        export_csv_path=export_csv_path,
        show_errors=show_errors,
        quiet=quiet,
    )


def main() -> None:
    """Console script and standalone executable entrypoint."""
    # Support GUI launch if packaged without console (sys.stdout is None on Windows)
    if sys.platform == "win32" and sys.stdout is None and len(sys.argv) <= 1:
        if _has_pyside6():
            from storage_view.gui import run_gui

            raise SystemExit(run_gui())
    app()


if __name__ == "__main__":
    main()
