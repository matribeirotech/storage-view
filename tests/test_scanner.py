"""Comprehensive test suite for Storage View core engine, CLI, TUI, and GUI layout."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from storage_view.core.models import DirNode, FileNode, ScanStats
from storage_view.core.scanner import ScanConfig, StorageScanner
from storage_view.core.utils import (
    export_to_csv,
    export_to_json,
    filter_nodes,
    format_bar,
    format_bytes,
    format_duration,
    parse_size,
)
from storage_view.main import app as cli_app


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a" * size)


# ---------------------------------------------------------------------------
# Formatting & Model Unit Tests
# ---------------------------------------------------------------------------


def test_format_bytes_and_parse_size() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1024**2 * 1.5) == "1.50 MB"
    assert format_bytes(1024**3 * 2, precision=1) == "2.0 GB"
    assert format_bytes(1024, iec=True) == "1.00 KiB"

    assert parse_size("1024") == 1024
    assert parse_size("100M") == 100 * 1024**2
    assert parse_size("1.5GB") == int(1.5 * 1024**3)
    assert parse_size("2 KiB") == 2048

    with pytest.raises(ValueError):
        parse_size("-10M")
    with pytest.raises(ValueError):
        parse_size("invalid")


def test_format_helpers() -> None:
    assert format_bar(0.5, width=10) == "█████░░░░░"
    assert format_bar(1.5, width=4) == "████"
    assert format_duration(0.25) == "250 ms"
    assert format_duration(12.5) == "12.50 s"
    assert format_duration(125) == "2m 5.0s"


def test_dir_node_remove_child_propagates_totals() -> None:
    root = DirNode(name="root", path="/root", size=1500, own_size=500, file_count=3, dir_count=1)
    sub = DirNode(name="sub", path="/root/sub", size=1000, own_size=1000, file_count=2, dir_count=0, parent=root)
    f1 = FileNode(name="a.bin", path="/root/a.bin", size=500, parent=root)
    f2 = FileNode(name="b.bin", path="/root/sub/b.bin", size=700, parent=sub)
    f3 = FileNode(name="c.bin", path="/root/sub/c.bin", size=300, parent=sub)
    root.children = [sub, f1]
    sub.children = [f2, f3]

    assert sub.remove_child(f2) is True
    assert sub.size == 300
    assert sub.own_size == 300
    assert sub.file_count == 1
    assert root.size == 800
    assert root.file_count == 2

    assert root.remove_child(sub) is True
    assert root.size == 500
    assert root.file_count == 1
    assert root.dir_count == 0


def test_dir_node_dict_roundtrip() -> None:
    root = DirNode(name="root", path="/root", size=300, own_size=100, file_count=2, dir_count=1)
    sub = DirNode(name="sub", path="/root/sub", size=200, own_size=200, file_count=1, dir_count=0, parent=root)
    root.children = [
        sub,
        FileNode(name="a.txt", path="/root/a.txt", size=100, parent=root),
    ]
    sub.children = [FileNode(name="b.txt", path="/root/sub/b.txt", size=200, parent=sub)]

    serialized = root.to_dict()
    rehydrated = DirNode.from_dict(serialized)
    assert rehydrated.size == 300
    assert rehydrated.file_count == 2
    assert len(rehydrated.children) == 2
    assert rehydrated.children[0].parent is rehydrated


# ---------------------------------------------------------------------------
# Core Scanner Tests
# ---------------------------------------------------------------------------


def test_scanner_recursive_sizes_and_counts(tmp_path: Path) -> None:
    _write_bytes(tmp_path / "root_small.txt", 100)
    _write_bytes(tmp_path / "logs" / "app.log", 2048)
    _write_bytes(tmp_path / "logs" / "archive" / "old.gz", 4096)
    _write_bytes(tmp_path / "media" / "clip.mp4", 8192)
    (tmp_path / "empty_dir").mkdir()

    scanner = StorageScanner()
    result = scanner.scan(tmp_path)

    expected_total = 100 + 2048 + 4096 + 8192
    assert result.root.size == expected_total
    assert result.stats.total_size == expected_total
    assert result.stats.total_files == 4
    assert result.stats.total_dirs == 4  # logs, archive, media, empty_dir
    assert result.stats.inaccessible_count == 0

    # Children should be sorted descending by size by default
    child_names = [c.name for c in result.root.children]
    assert child_names[0] == "media"
    assert child_names[1] == "logs"
    assert child_names[2] == "root_small.txt"
    assert child_names[3] == "empty_dir"


def test_scanner_missing_and_non_dir_errors(tmp_path: Path) -> None:
    scanner = StorageScanner()
    with pytest.raises(FileNotFoundError):
        scanner.scan(tmp_path / "does_not_exist")

    file_path = tmp_path / "single.txt"
    _write_bytes(file_path, 42)
    with pytest.raises(NotADirectoryError):
        scanner.scan(file_path)


def test_scanner_avoids_symlink_infinite_loop(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _write_bytes(sub / "data.bin", 512)

    loop_link = sub / "loop_back"
    try:
        os.symlink(tmp_path, loop_link, target_is_directory=True)
        real_symlink_created = True
    except OSError:
        # Windows without Developer Mode / elevated symlink privilege
        real_symlink_created = False

    scanner = StorageScanner(ScanConfig(follow_symlinks=False))
    if real_symlink_created:
        result = scanner.scan(tmp_path)
        assert result.root.size == 512
        assert result.stats.total_files == 1
        assert result.stats.symlinks_skipped >= 1
    else:
        # Simulate a directory entry whose symlink flag is True pointing back to root
        real_scandir = os.scandir

        class _FakeLinkEntry:
            name = "loop_back"
            path = str(loop_link)

            def is_symlink(self) -> bool:
                return True

            def is_junction(self) -> bool:
                return False

            def is_dir(self, follow_symlinks: bool = False) -> bool:
                return follow_symlinks

            def is_file(self, follow_symlinks: bool = False) -> bool:
                return False

            def stat(self, follow_symlinks: bool = False):
                return SimpleNamespace(
                    st_size=0,
                    st_mtime=0.0,
                    st_dev=1,
                    st_ino=999,
                    st_nlink=1,
                    st_file_attributes=0,
                )

        class _WrappedScandir:
            def __init__(self, p: str) -> None:
                self._inner = real_scandir(p)
                self._inject = os.path.abspath(p) == os.path.abspath(str(sub))

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                if self._inject:
                    self._inject = False
                    return _FakeLinkEntry()
                return next(self._inner)

        with patch("storage_view.core.scanner.os.scandir", side_effect=_WrappedScandir):
            result = scanner.scan(tmp_path)

        assert result.root.size == 512
        assert result.stats.total_files == 1
        assert result.stats.symlinks_skipped == 1


def test_scanner_handles_permission_errors_resiliently(tmp_path: Path) -> None:
    _write_bytes(tmp_path / "public" / "ok.bin", 1024)
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    _write_bytes(secret_dir / "hidden.bin", 9999)

    real_scandir = os.scandir

    def _guarded_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(str(secret_dir)):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    with patch("storage_view.core.scanner.os.scandir", side_effect=_guarded_scandir):
        result = StorageScanner().scan(tmp_path)

    assert result.root.size == 1024
    assert result.stats.total_files == 1
    assert result.stats.inaccessible_count == 1
    assert os.path.abspath(str(secret_dir)) in result.stats.inaccessible_paths

    secret_node = next(c for c in result.root.children if c.name == "secret")
    assert isinstance(secret_node, DirNode)
    assert secret_node.inaccessible is True
    assert secret_node.error is not None


def test_scanner_cancellation_and_async(tmp_path: Path) -> None:
    for i in range(15):
        _write_bytes(tmp_path / f"dir_{i}" / "file.dat", 256)

    scanner = StorageScanner(ScanConfig(max_workers=2, progress_interval=0.001))
    cancel_event = threading.Event()
    callbacks: list[ScanStats] = []

    def _on_progress(stats: ScanStats) -> None:
        callbacks.append(stats)
        if stats.total_dirs >= 1:
            cancel_event.set()

    cancelled_res = scanner.scan(
        tmp_path, progress_callback=_on_progress, cancel_event=cancel_event
    )
    assert cancelled_res.stats.cancelled is True

    # Full async scan should complete accurately
    async_progress: list[int] = []

    async def _run_async() -> None:
        async def _async_cb(stats: ScanStats) -> None:
            async_progress.append(stats.total_files)

        res = await StorageScanner().scan_async(tmp_path, progress_callback=_async_cb)
        assert res.stats.cancelled is False
        assert res.stats.total_files == 15
        assert res.root.size == 15 * 256

    asyncio.run(_run_async())
    assert len(async_progress) >= 1


def test_filters_and_exclusions(tmp_path: Path) -> None:
    _write_bytes(tmp_path / "keep.log", 2048)
    _write_bytes(tmp_path / "small.log", 100)
    _write_bytes(tmp_path / ".hidden_file", 4096)
    _write_bytes(tmp_path / "node_modules" / "pkg.js", 8192)

    scanner = StorageScanner(
        ScanConfig(include_hidden=False, exclude_patterns=("node_modules",))
    )
    result = scanner.scan(tmp_path)
    assert result.root.size == 2148
    assert result.stats.total_files == 2

    filtered = filter_nodes(
        result.root,
        min_size=1024,
        node_type="files",
        extensions=[".log"],
        top_n=10,
    )
    assert len(filtered) == 1
    assert filtered[0].name == "keep.log"


def test_json_and_csv_serialization(tmp_path: Path) -> None:
    _write_bytes(tmp_path / "folder" / "alpha.bin", 1500)
    _write_bytes(tmp_path / "beta.txt", 500)

    result = StorageScanner().scan(tmp_path)
    json_out = tmp_path / "out" / "scan.json"
    csv_out = tmp_path / "out" / "scan.csv"

    json_text = export_to_json(result, json_out)
    parsed_json = json.loads(json_text)
    assert parsed_json["stats"]["total_size"] == 2000
    assert parsed_json["tree"]["size"] == 2000
    assert json_out.exists()

    csv_text = export_to_csv(result, csv_out)
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0][0] == "type"
    assert any(r[1] == "alpha.bin" and r[3] == "1500" for r in rows[1:])
    assert csv_out.exists()


# ---------------------------------------------------------------------------
# CLI, TUI, and GUI Layout Tests
# ---------------------------------------------------------------------------


def test_cli_headless_top_min_size_and_json_csv(tmp_path: Path) -> None:
    _write_bytes(tmp_path / "big.iso", 4096)
    _write_bytes(tmp_path / "tiny.txt", 64)

    runner = CliRunner()

    res_table = runner.invoke(
        cli_app,
        [str(tmp_path), "--top", "5", "--min-size", "1K", "--quiet"],
    )
    assert res_table.exit_code == 0
    assert "big.iso" in res_table.output
    assert "tiny.txt" not in res_table.output

    res_json = runner.invoke(cli_app, [str(tmp_path), "--json", "--quiet"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["stats"]["total_size"] == 4160

    res_csv = runner.invoke(cli_app, [str(tmp_path), "--csv", "--quiet"])
    assert res_csv.exit_code == 0
    csv_rows = list(csv.reader(io.StringIO(res_csv.stdout)))
    assert csv_rows[0][0] == "type"
    assert any(row[1] == "big.iso" for row in csv_rows[1:])


def test_tui_navigation_and_sorting(tmp_path: Path) -> None:
    from storage_view.cli.tui_app import StorageTUIApp

    _write_bytes(tmp_path / "dir_a" / "inside.bin", 2048)
    _write_bytes(tmp_path / "small.txt", 128)

    async def _drive_tui() -> None:
        tui = StorageTUIApp(tmp_path)
        async with tui.run_test() as pilot:
            # Wait briefly for background worker scan to populate rows
            for _ in range(40):
                if tui._scan_result is not None and not tui._is_scanning:
                    break
                await pilot.pause(0.05)

            assert tui._scan_result is not None
            assert tui._current_dir is tui._scan_result.root
            assert len(tui._row_nodes) == 2

            # First row sorted by size desc is dir_a; drill down with 'l'
            await pilot.press("l")
            assert tui._current_dir.name == "dir_a"

            # Go back up with 'h'
            await pilot.press("h")
            assert tui._current_dir is tui._scan_result.root

            # Cycle sort order with 's'
            await pilot.press("s")
            assert tui._SORT_MODES[tui._sort_idx] == "name"

            await pilot.press("q")

    asyncio.run(_drive_tui())


def test_gui_squarified_treemap_layout(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor
    from storage_view.gui.widgets import _squarify_layout

    items = [
        (FileNode(name="a", path="/a", size=600), "a", 600, QColor("#2563eb")),
        (FileNode(name="b", path="/b", size=300), "b", 300, QColor("#7c3aed")),
        (FileNode(name="c", path="/c", size=100), "c", 100, QColor("#0d9488")),
    ]
    bounds = QRectF(0.0, 0.0, 500.0, 400.0)
    tiles = _squarify_layout(items, bounds, total_dir_size=1000)

    assert len(tiles) == 3
    total_area = sum(t.rect.width() * t.rect.height() for t in tiles)
    assert pytest.approx(total_area, rel=1e-3) == 500.0 * 400.0
    for tile in tiles:
        assert tile.rect.left() >= -0.01
        assert tile.rect.top() >= -0.01
        assert tile.rect.right() <= 500.01
        assert tile.rect.bottom() <= 400.01


def test_gui_window_offscreen_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication
    from storage_view.gui.app import DARK_STYLESHEET, StorageViewWindow

    _write_bytes(tmp_path / "videos" / "sample.mp4", 4096)
    _write_bytes(tmp_path / "notes.md", 512)

    qapp = QApplication.instance() or QApplication([])
    qapp.setStyleSheet(DARK_STYLESHEET)

    window = StorageViewWindow()
    result = StorageScanner().scan(tmp_path)
    window._on_scan_finished(result)

    assert window.tree_widget.topLevelItemCount() == 2
    assert len(window.treemap_widget._tiles) == 2

    # Drill down into 'videos' and navigate back up
    videos_dir = next(c for c in result.root.children if c.name == "videos")
    assert isinstance(videos_dir, DirNode)
    window._navigate_to_dir(videos_dir)
    assert window.tree_widget.topLevelItemCount() == 1
    assert window.up_btn.isEnabled() is True

    window._on_go_up()
    assert window._current_dir is result.root
    assert window.up_btn.isEnabled() is False
    window.close()
