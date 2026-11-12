"""Headless Rich terminal reporter for server, SSH, and scripted disk analysis."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from storage_view.core.models import DirNode, ScanResult, ScanStats, TreeNode
from storage_view.core.scanner import ScanConfig, StorageScanner
from storage_view.core.utils import (
    export_to_csv,
    export_to_json,
    filter_nodes,
    format_bar,
    format_bytes,
    format_duration,
    format_timestamp,
)


def _display_path(node: TreeNode, root_path: str) -> str:
    """Display path relative to root when nested, falling back to full path."""
    if node.path == root_path:
        return node.path
    try:
        rel = os.path.relpath(node.path, root_path)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return node.path


def _usage_color(percent: float) -> str:
    if percent >= 40.0:
        return "bold red"
    if percent >= 15.0:
        return "yellow"
    return "cyan"


def _can_encode(console: Console, sample: str) -> bool:
    """Check whether the console encoding safely supports the given characters."""
    if getattr(console, "legacy_windows", False):
        return False
    enc = getattr(console, "encoding", None) or "utf-8"
    try:
        sample.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render_report(
    result: ScanResult,
    *,
    top_n: int = 20,
    min_size: int = 0,
    target_type: str = "all",
    sort_by: str = "size",
    max_depth: Optional[int] = None,
    extensions: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
    console: Optional[Console] = None,
    show_errors: bool = False,
) -> None:
    """Render a summary panel and Top-N disk usage table using Rich."""
    out = console or Console()
    stats = result.stats
    root = result.root

    summary = Text()
    summary.append("Target: ", style="bold dim")
    summary.append(f"{stats.root_path}\n", style="bold white")
    summary.append("Total Size: ", style="bold dim")
    summary.append(f"{format_bytes(stats.total_size)}", style="bold cyan")
    summary.append("  |  Files: ", style="bold dim")
    summary.append(f"{stats.total_files:,}", style="bold white")
    summary.append("  |  Dirs: ", style="bold dim")
    summary.append(f"{stats.total_dirs:,}\n", style="bold white")
    summary.append("Elapsed: ", style="bold dim")
    summary.append(f"{format_duration(stats.elapsed_seconds)}", style="green")
    summary.append("  |  Speed: ", style="bold dim")
    summary.append(f"{stats.files_per_second:,.0f} files/s", style="green")
    if stats.symlinks_skipped:
        summary.append("  |  Symlinks skipped: ", style="bold dim")
        summary.append(f"{stats.symlinks_skipped:,}", style="yellow")
    if stats.inaccessible_count:
        summary.append("  |  Inaccessible: ", style="bold dim")
        summary.append(f"{stats.inaccessible_count:,}", style="bold red")
    if stats.cancelled:
        summary.append("  [PARTIAL - CANCELLED]", style="bold red")

    unicode_ok = _can_encode(out, "█░—")
    title_sep = "—" if unicode_ok else "-"
    bar_filled = "█" if unicode_ok else "#"
    bar_empty = "░" if unicode_ok else "-"

    out.print(
        Panel(
            summary,
            title=f"[bold cyan]Storage View {title_sep} Disk Usage Summary[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        )
    )

    entries = filter_nodes(
        root,
        min_size=min_size,
        node_type=target_type,
        extensions=extensions,
        exclude_patterns=exclude_patterns,
        max_depth=max_depth,
        sort_by=sort_by,
        reverse=(sort_by != "name"),
        top_n=top_n,
    )

    if not entries:
        out.print(
            f"[yellow]No entries matched the active filters "
            f"(type={target_type}, min_size={format_bytes(min_size)}).[/yellow]"
        )
        return

    compact_mode = out.width < 100
    bar_width = 10 if compact_mode else 14

    table = Table(
        title=f"Top {len(entries)} Largest Entries ({target_type})",
        box=box.ROUNDED,
        header_style="bold magenta",
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Type", justify="center", no_wrap=True)
    table.add_column("Size", justify="right", style="bold cyan", no_wrap=True)
    table.add_column("%", justify="right", no_wrap=True)
    table.add_column("Usage", width=bar_width, no_wrap=True)
    table.add_column("Files", justify="right", style="dim", no_wrap=True)
    if not compact_mode:
        table.add_column("Modified", style="dim", no_wrap=True)
    table.add_column("Path", min_width=18, ratio=1, overflow="fold")

    root_size = root.size
    for idx, node in enumerate(entries, start=1):
        pct = node.percentage_of(root_size)
        color = _usage_color(pct)
        bar = format_bar(
            pct / 100.0,
            width=bar_width,
            filled=bar_filled,
            empty=bar_empty,
        )

        if isinstance(node, DirNode):
            kind = "[bold blue]DIR[/bold blue]"
            if node.inaccessible:
                kind = "[bold red]DENY[/bold red]"
            elif node.is_symlink:
                kind = "[yellow]LDIR[/yellow]"
            items = f"{node.file_count:,}"
        else:
            kind = "[yellow]LINK[/yellow]" if node.is_symlink else "[green]FILE[/green]"
            items = "-"

        row_cells = [
            str(idx),
            kind,
            format_bytes(node.size),
            f"[{color}]{pct:5.1f}%[/{color}]",
            f"[{color}]{bar}[/{color}]",
            items,
        ]
        if not compact_mode:
            row_cells.append(format_timestamp(node.mtime))
        row_cells.append(_display_path(node, stats.root_path))

        table.add_row(*row_cells)

    out.print(table)

    if stats.inaccessible_count > 0:
        out.print(
            f"[yellow]Note: {stats.inaccessible_count} path(s) could not be read due to "
            "permissions or OS errors.[/yellow]"
        )
        if show_errors:
            for err_path in stats.inaccessible_paths[:25]:
                out.print(f"  [dim red]- {err_path}[/dim red]")
            if len(stats.inaccessible_paths) > 25:
                out.print(
                    f"  [dim]... and {len(stats.inaccessible_paths) - 25} more.[/dim]"
                )


def run_headless_scan(
    target_path: str | Path,
    *,
    config: Optional[ScanConfig] = None,
    top_n: int = 20,
    min_size: int = 0,
    target_type: str = "all",
    sort_by: str = "size",
    max_depth: Optional[int] = None,
    extensions: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
    output_json: bool = False,
    output_csv: bool = False,
    export_json_path: Optional[Path] = None,
    export_csv_path: Optional[Path] = None,
    show_errors: bool = False,
    quiet: bool = False,
    console: Optional[Console] = None,
) -> ScanResult:
    """Execute a scan in headless mode and render or serialize the results."""
    scanner = StorageScanner(config=config)
    out_console = console or Console()
    err_console = Console(stderr=True)
    show_progress = (
        not quiet
        and not output_json
        and not output_csv
        and err_console.is_terminal
    )

    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=24),
            TextColumn("[progress.percentage]{task.fields[info]}"),
            TimeElapsedColumn(),
            console=err_console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("Scanning...", total=None, info="starting")

            def _on_progress(stats: ScanStats) -> None:
                info = (
                    f"{format_bytes(stats.total_size)} | "
                    f"{stats.total_files:,} files | "
                    f"{stats.files_per_second:,.0f} files/s"
                )
                progress.update(task_id, info=info)

            result = scanner.scan(target_path, progress_callback=_on_progress)
    else:
        result = scanner.scan(target_path)

    if export_json_path is not None:
        export_to_json(result, export_json_path, max_depth=max_depth)
        if not output_json and not output_csv:
            out_console.print(f"[green]Exported JSON report to:[/green] {export_json_path}")

    if export_csv_path is not None:
        export_to_csv(result, export_csv_path)
        if not output_json and not output_csv:
            out_console.print(f"[green]Exported CSV report to:[/green] {export_csv_path}")

    if output_json:
        sys.stdout.write(export_to_json(result, max_depth=max_depth) + "\n")
    elif output_csv:
        nodes = filter_nodes(
            result.root,
            min_size=min_size,
            node_type=target_type,
            extensions=extensions,
            exclude_patterns=exclude_patterns,
            max_depth=max_depth,
            sort_by=sort_by,
            reverse=(sort_by != "name"),
            top_n=top_n if (min_size > 0 or top_n > 0) else None,
            include_root=True,
        )
        sys.stdout.write(export_to_csv(result, nodes=nodes))
    else:
        render_report(
            result,
            top_n=top_n,
            min_size=min_size,
            target_type=target_type,
            sort_by=sort_by,
            max_depth=max_depth,
            extensions=extensions,
            exclude_patterns=exclude_patterns,
            console=out_console,
            show_errors=show_errors,
        )

    return result
