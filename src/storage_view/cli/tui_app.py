"""Interactive ncdu-style Terminal User Interface built with Textual."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from storage_view.core.models import DirNode, FileNode, ScanResult, ScanStats, TreeNode
from storage_view.core.scanner import ScanConfig, StorageScanner
from storage_view.core.utils import (
    export_to_json,
    format_bar,
    format_bytes,
    format_duration,
    format_timestamp,
    move_to_trash,
)


class ConfirmTrashScreen(ModalScreen[bool]):
    """Modal confirmation dialog before moving a file or folder to Trash."""

    CSS = """
    ConfirmTrashScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 64;
        height: auto;
        border: heavy $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #confirm-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("y", "confirm_yes", "Yes"),
        Binding("n", "confirm_no", "No"),
        Binding("escape", "confirm_no", "Cancel"),
    ]

    def __init__(self, target_name: str, target_path: str, size_bytes: int) -> None:
        super().__init__()
        self.target_name = target_name
        self.target_path = target_path
        self.size_bytes = size_bytes

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("Move Item to Trash?", id="confirm-title")
            yield Label(
                f"Name: {self.target_name}\n"
                f"Size: {format_bytes(self.size_bytes)}\n"
                f"Path: {self.target_path}"
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel [n]", id="cancel-btn", variant="default")
                yield Button("Move to Trash [y]", id="trash-btn", variant="error")

    @on(Button.Pressed, "#trash-btn")
    def _on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-btn")
    def _on_no(self) -> None:
        self.dismiss(False)

    def action_confirm_yes(self) -> None:
        self.dismiss(True)

    def action_confirm_no(self) -> None:
        self.dismiss(False)


class ScanInfoScreen(ModalScreen[None]):
    """Modal showing scan statistics and inaccessible paths."""

    CSS = """
    ScanInfoScreen {
        align: center middle;
    }
    #info-dialog {
        width: 78;
        max-height: 28;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #info-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #info-body {
        height: auto;
        max-height: 18;
        overflow-y: auto;
    }
    #info-close {
        margin-top: 1;
        align-horizontal: right;
    }
    """

    BINDINGS = [
        Binding("escape", "close_modal", "Close"),
        Binding("i", "close_modal", "Close"),
        Binding("q", "close_modal", "Close"),
    ]

    def __init__(self, stats: ScanStats) -> None:
        super().__init__()
        self.stats = stats

    def compose(self) -> ComposeResult:
        s = self.stats
        lines = [
            f"Root Path:          {s.root_path}",
            f"Total Size:         {format_bytes(s.total_size)} ({s.total_size:,} bytes)",
            f"Total Files:        {s.total_files:,}",
            f"Total Directories:  {s.total_dirs:,}",
            f"Scan Duration:      {format_duration(s.elapsed_seconds)}",
            f"Throughput:         {s.files_per_second:,.0f} files/s",
            f"Symlinks Skipped:   {s.symlinks_skipped:,}",
            f"Hardlinks Deduped:  {s.hardlinks_deduplicated:,}",
            f"Inaccessible Paths: {s.inaccessible_count:,}",
        ]
        if s.inaccessible_paths:
            lines.append("\nInaccessible / Error Paths (up to 30):")
            for err_path in s.inaccessible_paths[:30]:
                lines.append(f"  - {err_path}")
            if len(s.inaccessible_paths) > 30:
                lines.append(f"  ... and {len(s.inaccessible_paths) - 30} more.")

        with Vertical(id="info-dialog"):
            yield Label("Scan Telemetry & Errors", id="info-title")
            yield Static("\n".join(lines), id="info-body")
            with Horizontal(id="info-close"):
                yield Button("Close [Esc]", id="close-info-btn", variant="primary")

    @on(Button.Pressed, "#close-info-btn")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class StorageTUIApp(App[None]):
    """Interactive ncdu-inspired disk usage explorer for the terminal."""

    TITLE = "Storage View"
    SUB_TITLE = "Disk Space Analyzer"

    CSS = """
    Screen {
        background: #0f172a;
        color: #e2e8f0;
    }
    #path-bar {
        height: 3;
        background: #1e293b;
        border-bottom: solid #334155;
        padding: 0 2;
        content-align: left middle;
    }
    #status-bar {
        height: 2;
        background: #162032;
        color: #94a3b8;
        padding: 0 2;
        content-align: left middle;
    }
    #filter-input {
        display: none;
        margin: 0 1;
        border: tall #38bdf8;
    }
    #filter-input.visible {
        display: block;
    }
    #entries-table {
        height: 1fr;
        background: #0f172a;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "enter_dir", "Open", show=False),
        Binding("right", "enter_dir", "Open"),
        Binding("h", "go_up", "Parent", show=False),
        Binding("left", "go_up", "Parent"),
        Binding("backspace", "go_up", "Parent", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("s", "cycle_sort", "Sort"),
        Binding("slash", "toggle_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("d", "trash_selected", "Trash"),
        Binding("r", "rescan", "Rescan"),
        Binding("e", "export_json", "Export"),
        Binding("i", "show_info", "Info"),
    ]

    _SORT_MODES = ("size", "name", "files", "mtime")

    def __init__(
        self,
        root_path: str | Path,
        config: Optional[ScanConfig] = None,
    ) -> None:
        super().__init__()
        self.root_path = os.path.abspath(os.fspath(root_path))
        self.scan_config = config or ScanConfig()
        self.scanner = StorageScanner(self.scan_config)
        self._scan_result: Optional[ScanResult] = None
        self._current_dir: Optional[DirNode] = None
        self._sort_idx = 0
        self._filter_text = ""
        self._row_nodes: list[Optional[TreeNode]] = []
        self._is_scanning = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"Path: {self.root_path}", id="path-bar")
        yield Static("Initializing scan...", id="status-bar")
        yield Input(placeholder="Filter current folder (Esc to clear)...", id="filter-input")
        yield DataTable(id="entries-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        table.add_columns("Type", "Name", "Size", "%", "Usage", "Files", "Modified")
        table.focus()
        self.start_scan()

    def on_unmount(self) -> None:
        self.scanner.cancel()

    @work(exclusive=True)
    async def start_scan(self) -> None:
        """Run directory scan in a background thread and stream live telemetry."""
        self._is_scanning = True
        status = self.query_one("#status-bar", Static)
        status.update(f"Scanning {self.root_path}...")

        def _on_progress(stats: ScanStats) -> None:
            try:
                self.call_from_thread(self._update_scanning_status, stats)
            except RuntimeError:
                pass

        try:
            result = await asyncio.to_thread(
                self.scanner.scan,
                self.root_path,
                progress_callback=_on_progress,
            )
        except OSError as exc:
            self._is_scanning = False
            status.update(f"Scan failed: {exc}")
            self.notify(str(exc), severity="error")
            return

        self._is_scanning = False
        self._scan_result = result
        self._current_dir = result.root
        self._refresh_view()

    def _update_scanning_status(self, stats: ScanStats) -> None:
        if not self._is_scanning:
            return
        status = self.query_one("#status-bar", Static)
        cur = stats.current_path
        if len(cur) > 55:
            cur = "..." + cur[-52:]
        status.update(
            f"Scanning: {format_bytes(stats.total_size)} | "
            f"{stats.total_files:,} files | "
            f"{stats.files_per_second:,.0f} files/s | {cur}"
        )

    def _refresh_view(self, preserve_row: int = 0) -> None:
        if self._current_dir is None or self._scan_result is None:
            return

        cur = self._current_dir
        stats = self._scan_result.stats
        sort_mode = self._SORT_MODES[self._sort_idx]

        path_bar = self.query_one("#path-bar", Static)
        path_bar.update(
            f"Path: {cur.path}   |   Folder Size: {format_bytes(cur.size)}   "
            f"|   Items: {len(cur.children):,}   |   Sort: {sort_mode}"
        )

        status = self.query_one("#status-bar", Static)
        filter_badge = f"   |   Filter: '{self._filter_text}'" if self._filter_text else ""
        err_badge = (
            f"   |   Inaccessible: {stats.inaccessible_count}"
            if stats.inaccessible_count
            else ""
        )
        status.update(
            f"Total: {format_bytes(stats.total_size)} | "
            f"{stats.total_files:,} files in {format_duration(stats.elapsed_seconds)} "
            f"({stats.files_per_second:,.0f} files/s){err_badge}{filter_badge}"
        )

        children = list(cur.children)
        if self._filter_text:
            needle = self._filter_text.lower()
            children = [c for c in children if needle in c.name.lower()]

        reverse = sort_mode != "name"
        if sort_mode == "name":
            children.sort(key=lambda n: (not n.is_dir, n.name.lower()))
        elif sort_mode == "files":
            children.sort(
                key=lambda n: (n.file_count if isinstance(n, DirNode) else 1, n.size),
                reverse=True,
            )
        elif sort_mode == "mtime":
            children.sort(key=lambda n: (n.mtime, n.size), reverse=True)
        else:
            children.sort(key=lambda n: (n.size, n.name.lower()), reverse=reverse)

        table = self.query_one("#entries-table", DataTable)
        table.clear()
        self._row_nodes = []

        if cur.parent is not None:
            self._row_nodes.append(None)
            table.add_row(
                Text("UP", style="bold cyan"),
                Text("..", style="bold cyan"),
                "-",
                "-",
                "",
                "-",
                "-",
                key="parent",
            )

        parent_size = cur.size
        for idx, node in enumerate(children):
            self._row_nodes.append(node)
            pct = node.percentage_of(parent_size)
            bar_str = format_bar(pct / 100.0, width=16)

            if pct >= 40.0:
                bar_style = "bold red"
            elif pct >= 15.0:
                bar_style = "yellow"
            else:
                bar_style = "cyan"

            if isinstance(node, DirNode):
                if node.inaccessible:
                    kind = Text("DENY", style="bold red")
                    name_text = Text(f"/{node.name}", style="bold red")
                elif node.is_symlink:
                    kind = Text("LDIR", style="yellow")
                    name_text = Text(f"/{node.name}", style="yellow")
                else:
                    kind = Text("DIR", style="bold blue")
                    name_text = Text(f"/{node.name}", style="bold white")
                items = f"{node.file_count:,}"
            else:
                if node.is_symlink:
                    kind = Text("LINK", style="yellow")
                    name_text = Text(node.name, style="yellow")
                else:
                    kind = Text("FILE", style="green")
                    name_text = Text(node.name, style="white")
                items = "-"

            table.add_row(
                kind,
                name_text,
                Text(format_bytes(node.size), style="bold cyan"),
                Text(f"{pct:5.1f}%", style=bar_style),
                Text(bar_str, style=bar_style),
                items,
                format_timestamp(node.mtime),
                key=f"row-{idx}",
            )

        if self._row_nodes:
            clamped = max(0, min(preserve_row, len(self._row_nodes) - 1))
            table.move_cursor(row=clamped)

    def _selected_node(self) -> tuple[int, Optional[TreeNode], bool]:
        """Return (row_index, node_or_None, is_parent_row)."""
        table = self.query_one("#entries-table", DataTable)
        if not self._row_nodes:
            return -1, None, False
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._row_nodes):
            return -1, None, False
        node = self._row_nodes[row_idx]
        return row_idx, node, (node is None)

    @on(DataTable.RowSelected, "#entries-table")
    def _on_row_selected(self) -> None:
        self.action_enter_dir()

    @on(Input.Changed, "#filter-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter_text = event.value.strip()
        self._refresh_view()

    @on(Input.Submitted, "#filter-input")
    def _on_filter_submitted(self) -> None:
        self.query_one("#entries-table", DataTable).focus()

    def action_cursor_down(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        table.action_cursor_up()

    def action_cursor_top(self) -> None:
        if self._row_nodes:
            self.query_one("#entries-table", DataTable).move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        if self._row_nodes:
            self.query_one("#entries-table", DataTable).move_cursor(
                row=len(self._row_nodes) - 1
            )

    def action_enter_dir(self) -> None:
        _, node, is_parent = self._selected_node()
        if is_parent:
            self.action_go_up()
            return
        if isinstance(node, DirNode):
            if node.inaccessible:
                self.notify(f"Inaccessible directory: {node.error or node.path}", severity="warning")
                return
            self._current_dir = node
            self._filter_text = ""
            filter_input = self.query_one("#filter-input", Input)
            filter_input.value = ""
            filter_input.remove_class("visible")
            self._refresh_view()

    def action_go_up(self) -> None:
        if self._current_dir is not None and self._current_dir.parent is not None:
            previous = self._current_dir
            self._current_dir = self._current_dir.parent
            self._filter_text = ""
            filter_input = self.query_one("#filter-input", Input)
            filter_input.value = ""
            filter_input.remove_class("visible")
            self._refresh_view()
            for idx, node in enumerate(self._row_nodes):
                if node is previous:
                    self.query_one("#entries-table", DataTable).move_cursor(row=idx)
                    break

    def action_cycle_sort(self) -> None:
        self._sort_idx = (self._sort_idx + 1) % len(self._SORT_MODES)
        self._refresh_view()

    def action_toggle_filter(self) -> None:
        filter_input = self.query_one("#filter-input", Input)
        if filter_input.has_class("visible"):
            filter_input.focus()
        else:
            filter_input.add_class("visible")
            filter_input.focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one("#filter-input", Input)
        if filter_input.has_class("visible") or self._filter_text:
            self._filter_text = ""
            filter_input.value = ""
            filter_input.remove_class("visible")
            self._refresh_view()
            self.query_one("#entries-table", DataTable).focus()

    def action_rescan(self) -> None:
        if self._is_scanning:
            return
        self.start_scan()

    def action_export_json(self) -> None:
        if self._scan_result is None:
            self.notify("Wait for the scan to finish before exporting.", severity="warning")
            return
        out_path = Path.cwd() / "storage-view-export.json"
        try:
            export_to_json(self._scan_result, out_path)
            self.notify(f"Saved JSON export to {out_path}")
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")

    def action_show_info(self) -> None:
        if self._scan_result is None:
            return
        self.push_screen(ScanInfoScreen(self._scan_result.stats))

    def action_trash_selected(self) -> None:
        row_idx, node, is_parent = self._selected_node()
        if is_parent or node is None or self._current_dir is None:
            return

        def _after_confirm(confirmed: Optional[bool]) -> None:
            if not confirmed or self._current_dir is None:
                return
            self._perform_trash(node, row_idx)

        self.push_screen(
            ConfirmTrashScreen(node.name, node.path, node.size),
            callback=_after_confirm,
        )

    @work(exclusive=False)
    async def _perform_trash(self, node: TreeNode, row_idx: int) -> None:
        try:
            await asyncio.to_thread(move_to_trash, node.path)
        except Exception as exc:
            self.notify(f"Could not move to Trash: {exc}", severity="error")
            return

        if self._current_dir is not None:
            self._current_dir.remove_child(node)
        if self._scan_result is not None:
            self._scan_result.stats.total_size = self._scan_result.root.size
            self._scan_result.stats.total_files = self._scan_result.root.file_count
            self._scan_result.stats.total_dirs = self._scan_result.root.dir_count

        self._refresh_view(preserve_row=row_idx)
        self.notify(f"Moved '{node.name}' to Trash ({format_bytes(node.size)} freed)")


def run_tui(
    target_path: str | Path,
    *,
    config: Optional[ScanConfig] = None,
) -> None:
    """Launch the interactive Textual TUI disk explorer."""
    app = StorageTUIApp(target_path, config=config)
    app.run()
