"""Formatting, filtering, export, and OS helper utilities for Storage View."""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import os
import platform
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

from storage_view.core.models import DirNode, FileNode, ScanResult, TreeNode

_BINARY_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_IEC_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_DECIMAL_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib|p|pb|pib)?\s*$",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
    "pib": 1024**5,
}


def format_bytes(
    num_bytes: int | float,
    *,
    binary: bool = True,
    precision: int = 2,
    iec: bool = False,
) -> str:
    """Format a byte count into human-readable units (B, KB, MB, GB, TB, PB)."""
    sign = "-" if num_bytes < 0 else ""
    value = float(abs(num_bytes))
    base = 1024.0 if (binary or iec) else 1000.0
    units = _IEC_UNITS if iec else (_BINARY_UNITS if binary else _DECIMAL_UNITS)

    idx = 0
    while value >= base and idx < len(units) - 1:
        value /= base
        idx += 1

    if idx == 0:
        return f"{sign}{int(value)} {units[0]}"
    return f"{sign}{value:.{precision}f} {units[idx]}"


def parse_size(size_spec: str | int | float) -> int:
    """Parse a human-readable size string (e.g. '100M', '1.5GB', '512K') into bytes."""
    if isinstance(size_spec, (int, float)):
        if size_spec < 0:
            raise ValueError(f"Size must be non-negative, got {size_spec!r}")
        return int(size_spec)

    match = _SIZE_PATTERN.match(str(size_spec))
    if not match:
        raise ValueError(
            f"Invalid size specification: {size_spec!r}. "
            "Expected formats like '500K', '100M', '1.5G', '2TB', or '1024'."
        )

    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    multiplier = _UNIT_MULTIPLIERS[unit]
    return int(number * multiplier)


def format_duration(seconds: float) -> str:
    """Format an elapsed duration in seconds cleanly."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f} s"
    minutes, rem = divmod(seconds, 60.0)
    if minutes < 60.0:
        return f"{int(minutes)}m {rem:.1f}s"
    hours, minutes = divmod(minutes, 60.0)
    return f"{int(hours)}h {int(minutes)}m {rem:.0f}s"


def format_bar(
    fraction: float,
    width: int = 16,
    *,
    filled: str = "█",
    empty: str = "░",
) -> str:
    """Render a proportional Unicode progress/usage bar for terminal interfaces."""
    clamped = max(0.0, min(1.0, fraction))
    filled_len = int(round(clamped * width))
    return (filled * filled_len) + (empty * (width - filled_len))


def format_timestamp(mtime: float) -> str:
    """Format a POSIX mtime float as a compact local timestamp string."""
    if mtime <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def is_hidden_entry(name: str, stat_result: Optional[os.stat_result] = None) -> bool:
    """Detect whether an entry is hidden on Unix (dotfile) or Windows attributes."""
    if name.startswith(".") and name not in {".", ".."}:
        return True
    if stat_result is not None:
        attrs = getattr(stat_result, "st_file_attributes", 0)
        hidden_flag = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x02)
        if attrs & hidden_flag:
            return True
    return False


def matches_exclude(name: str, path: str, patterns: Sequence[str]) -> bool:
    """Return True if an entry name or full path matches any glob pattern."""
    if not patterns:
        return False
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern):
            return True
    return False


def filter_nodes(
    root: DirNode,
    *,
    min_size: int = 0,
    max_size: Optional[int] = None,
    node_type: str = "all",
    extensions: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
    max_depth: Optional[int] = None,
    sort_by: str = "size",
    reverse: bool = True,
    top_n: Optional[int] = None,
    include_root: bool = False,
) -> list[TreeNode]:
    """Collect, filter, sort, and slice tree nodes according to query constraints."""
    norm_exts = {
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in extensions
        if ext.strip()
    }
    target_type = node_type.lower()
    if target_type not in {"all", "files", "dirs"}:
        raise ValueError(f"Unsupported node_type: {node_type!r}")

    results: list[TreeNode] = []

    def _walk(node: DirNode, depth: int) -> None:
        if depth == 0 and include_root:
            if _matches(node):
                results.append(node)
        if max_depth is not None and depth >= max_depth:
            return
        for child in node.children:
            if matches_exclude(child.name, child.path, exclude_patterns):
                continue
            if _matches(child):
                results.append(child)
            if isinstance(child, DirNode):
                _walk(child, depth + 1)

    def _matches(node: TreeNode) -> bool:
        if node.size < min_size:
            return False
        if max_size is not None and node.size > max_size:
            return False
        if target_type == "files" and node.is_dir:
            return False
        if target_type == "dirs" and not node.is_dir:
            return False
        if norm_exts:
            if isinstance(node, DirNode):
                return False
            if node.extension not in norm_exts:
                return False
        return True

    _walk(root, 0)

    if sort_by == "name":
        key_fn = lambda n: (n.name.lower(), n.path.lower())
    elif sort_by == "files":
        key_fn = lambda n: (n.file_count if isinstance(n, DirNode) else 1, n.size)
    elif sort_by == "mtime":
        key_fn = lambda n: (n.mtime, n.size)
    else:
        key_fn = lambda n: (n.size, n.name.lower())

    results.sort(key=key_fn, reverse=reverse)
    if top_n is not None and top_n >= 0:
        return results[:top_n]
    return results


def export_to_json(
    result: ScanResult,
    destination: Optional[str | Path] = None,
    *,
    max_depth: Optional[int] = None,
    indent: int = 2,
) -> str:
    """Serialize a ScanResult to JSON and optionally write it to disk."""
    payload = json.dumps(result.to_dict(max_depth=max_depth), indent=indent, ensure_ascii=False)
    if destination is not None:
        out_path = Path(destination)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
    return payload


def export_to_csv(
    result: ScanResult,
    destination: Optional[str | Path] = None,
    *,
    include_dirs: bool = True,
    include_files: bool = True,
    nodes: Optional[Iterable[TreeNode]] = None,
) -> str:
    """Serialize scan nodes to CSV and optionally write to disk."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "type",
            "name",
            "path",
            "size_bytes",
            "size_human",
            "percent_of_root",
            "file_count",
            "dir_count",
            "modified",
            "is_symlink",
            "inaccessible",
            "error",
        ]
    )

    root_size = result.root.size
    iterable = nodes if nodes is not None else result.root.iter_nodes(include_self=True)

    for node in iterable:
        if isinstance(node, DirNode):
            if not include_dirs:
                continue
            writer.writerow(
                [
                    "dir",
                    node.name,
                    node.path,
                    node.size,
                    format_bytes(node.size),
                    f"{node.percentage_of(root_size):.2f}",
                    node.file_count,
                    node.dir_count,
                    format_timestamp(node.mtime),
                    node.is_symlink,
                    node.inaccessible,
                    node.error or "",
                ]
            )
        elif isinstance(node, FileNode):
            if not include_files:
                continue
            writer.writerow(
                [
                    "file",
                    node.name,
                    node.path,
                    node.size,
                    format_bytes(node.size),
                    f"{node.percentage_of(root_size):.2f}",
                    1,
                    0,
                    format_timestamp(node.mtime),
                    node.is_symlink,
                    False,
                    "",
                ]
            )

    csv_text = buffer.getvalue()
    if destination is not None:
        out_path = Path(destination)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(csv_text, encoding="utf-8")
    return csv_text


def reveal_in_file_manager(path: str | Path) -> None:
    """Open the native file explorer and highlight or open the target path."""
    norm_path = os.path.normpath(os.path.abspath(str(path)))
    system = platform.system()

    if system == "Windows":
        if os.path.isdir(norm_path) and os.path.dirname(norm_path) == norm_path:
            # Drive root (e.g., C:\) cannot be selected inside a parent directory.
            os.startfile(norm_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", "/select,", norm_path])
    elif system == "Darwin":
        subprocess.Popen(["open", "-R", norm_path])
    else:
        target = norm_path if os.path.isdir(norm_path) else os.path.dirname(norm_path)
        subprocess.Popen(["xdg-open", target or "."])


def move_to_trash(path: str | Path) -> None:
    """Move a file or directory to the system Trash / Recycle Bin."""
    from send2trash import send2trash

    norm_path = os.path.normpath(os.path.abspath(str(path)))
    send2trash(norm_path)
