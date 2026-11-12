"""Asynchronous and multithreaded directory scanner built on os.scandir."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from storage_view.core.models import DirNode, FileNode, ScanResult, ScanStats
from storage_view.core.utils import is_hidden_entry, matches_exclude

ProgressCallback = Callable[[ScanStats], None]
AsyncProgressCallback = Callable[[ScanStats], Awaitable[None] | None]

_WIN_REPARSE_SYMLINK = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)
_WIN_REPARSE_MOUNT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


@dataclass(slots=True)
class ScanConfig:
    """Configuration options controlling StorageScanner traversal behavior."""

    max_workers: int = 0
    follow_symlinks: bool = False
    one_file_system: bool = False
    include_hidden: bool = True
    exclude_patterns: tuple[str, ...] = ()
    deduplicate_hardlinks: bool = True
    sort_results: bool = True
    progress_interval: float = 0.08

    def resolved_workers(self) -> int:
        if self.max_workers > 0:
            return self.max_workers
        return min(32, max(4, (os.cpu_count() or 4) * 2))


@dataclass(slots=True)
class _DirScanOutput:
    """Result returned by a single worker directory scan task."""

    dir_node: DirNode
    files: list[FileNode]
    child_dirs: list[tuple[DirNode, tuple[int, int]]]
    hardlinks: list[tuple[FileNode, tuple[int, int]]]
    own_size: int
    file_count: int
    symlinks_skipped: int
    errors: list[tuple[str, str]]
    inaccessible: bool
    error_message: Optional[str]


def _is_entry_reparse_link(
    entry: os.DirEntry[str],
    stat_result: Optional[os.stat_result] = None,
) -> bool:
    """Detect symlinks and Windows NTFS directory junctions safely."""
    if entry.is_symlink():
        return True
    is_junction_fn = getattr(entry, "is_junction", None)
    if callable(is_junction_fn):
        try:
            if is_junction_fn():
                return True
        except OSError:
            pass
    if stat_result is not None:
        tag = getattr(stat_result, "st_reparse_tag", 0)
        if tag in (_WIN_REPARSE_SYMLINK, _WIN_REPARSE_MOUNT):
            return True
    return False


def _scan_single_dir(
    dir_node: DirNode,
    config: ScanConfig,
    root_dev: int,
    cancel_event: threading.Event,
) -> _DirScanOutput:
    """Scan exactly one directory using os.scandir without recursing inside the worker."""
    files: list[FileNode] = []
    child_dirs: list[tuple[DirNode, tuple[int, int]]] = []
    hardlinks: list[tuple[FileNode, tuple[int, int]]] = []
    errors: list[tuple[str, str]] = []
    own_size = 0
    file_count = 0
    symlinks_skipped = 0

    if cancel_event.is_set():
        return _DirScanOutput(
            dir_node=dir_node,
            files=files,
            child_dirs=child_dirs,
            hardlinks=hardlinks,
            own_size=0,
            file_count=0,
            symlinks_skipped=0,
            errors=errors,
            inaccessible=False,
            error_message=None,
        )

    try:
        scandir_it = os.scandir(dir_node.path)
    except OSError as exc:
        return _DirScanOutput(
            dir_node=dir_node,
            files=files,
            child_dirs=child_dirs,
            hardlinks=hardlinks,
            own_size=0,
            file_count=0,
            symlinks_skipped=0,
            errors=[(dir_node.path, str(exc))],
            inaccessible=True,
            error_message=str(exc),
        )

    try:
        with scandir_it:
            while not cancel_event.is_set():
                try:
                    entry = next(scandir_it)
                except StopIteration:
                    break
                except OSError as exc:
                    errors.append((dir_node.path, str(exc)))
                    break

                name = entry.name
                entry_path = entry.path

                if matches_exclude(name, entry_path, config.exclude_patterns):
                    continue

                try:
                    lstat_res = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append((entry_path, str(exc)))
                    continue

                if not config.include_hidden and is_hidden_entry(name, lstat_res):
                    continue

                is_link = _is_entry_reparse_link(entry, lstat_res)

                if is_link and not config.follow_symlinks:
                    symlinks_skipped += 1
                    files.append(
                        FileNode(
                            name=name,
                            path=entry_path,
                            size=0,
                            mtime=float(lstat_res.st_mtime),
                            is_symlink=True,
                            parent=dir_node,
                        )
                    )
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=config.follow_symlinks)
                except OSError as exc:
                    errors.append((entry_path, str(exc)))
                    continue

                if is_dir:
                    target_stat = lstat_res
                    if is_link and config.follow_symlinks:
                        try:
                            target_stat = entry.stat(follow_symlinks=True)
                        except OSError as exc:
                            errors.append((entry_path, str(exc)))
                            continue

                    if config.one_file_system and target_stat.st_dev != root_dev:
                        continue

                    child_dir = DirNode(
                        name=name,
                        path=entry_path,
                        mtime=float(target_stat.st_mtime),
                        is_symlink=is_link,
                        parent=dir_node,
                    )
                    child_dirs.append(
                        (child_dir, (int(target_stat.st_dev), int(target_stat.st_ino)))
                    )
                    continue

                try:
                    is_file = entry.is_file(follow_symlinks=config.follow_symlinks)
                except OSError as exc:
                    errors.append((entry_path, str(exc)))
                    continue

                if not is_file:
                    continue

                file_stat = lstat_res
                if is_link and config.follow_symlinks:
                    try:
                        file_stat = entry.stat(follow_symlinks=True)
                    except OSError as exc:
                        errors.append((entry_path, str(exc)))
                        continue

                file_size = max(0, int(file_stat.st_size))
                fnode = FileNode(
                    name=name,
                    path=entry_path,
                    size=file_size,
                    mtime=float(file_stat.st_mtime),
                    is_symlink=is_link,
                    parent=dir_node,
                )

                if (
                    config.deduplicate_hardlinks
                    and file_stat.st_nlink > 1
                    and file_stat.st_ino != 0
                ):
                    hardlinks.append(
                        (fnode, (int(file_stat.st_dev), int(file_stat.st_ino)))
                    )
                else:
                    own_size += file_size
                    file_count += 1
                    files.append(fnode)
    except OSError as exc:
        errors.append((dir_node.path, str(exc)))

    return _DirScanOutput(
        dir_node=dir_node,
        files=files,
        child_dirs=child_dirs,
        hardlinks=hardlinks,
        own_size=own_size,
        file_count=file_count,
        symlinks_skipped=symlinks_skipped,
        errors=errors,
        inaccessible=False,
        error_message=None,
    )


def _aggregate_tree(root: DirNode) -> None:
    """Compute bottom-up recursive sizes, file counts, and dir counts iteratively."""
    post_order: list[DirNode] = []
    stack: list[DirNode] = [root]

    while stack:
        node = stack.pop()
        post_order.append(node)
        for child in node.children:
            if isinstance(child, DirNode):
                stack.append(child)

    for node in reversed(post_order):
        total_size = node.own_size
        total_files = 0
        total_dirs = 0

        for child in node.children:
            if isinstance(child, DirNode):
                total_size += child.size
                total_files += child.file_count
                total_dirs += 1 + child.dir_count
            elif not child.is_symlink:
                total_files += 1

        node.size = total_size
        node.file_count = total_files
        node.dir_count = total_dirs


class StorageScanner:
    """High-performance concurrent directory scanner using os.scandir."""

    def __init__(self, config: Optional[ScanConfig] = None) -> None:
        self.config = config or ScanConfig()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal any active scan to terminate cleanly as soon as possible."""
        self._cancel_event.set()

    def reset(self) -> None:
        """Reset internal cancellation state before starting a new scan."""
        self._cancel_event.clear()

    def scan(
        self,
        root_path: str | Path,
        *,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ScanResult:
        """Synchronously scan a directory tree using a bounded thread pool."""
        self._cancel_event.clear()
        active_cancel = cancel_event if cancel_event is not None else self._cancel_event

        resolved_path = os.path.abspath(os.fspath(root_path))
        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"Path does not exist: {resolved_path}")
        if not os.path.isdir(resolved_path):
            raise NotADirectoryError(f"Path is not a directory: {resolved_path}")

        root_stat = os.stat(resolved_path)
        root_dev = int(root_stat.st_dev)
        root_name = os.path.basename(resolved_path) or resolved_path

        root_node = DirNode(
            name=root_name,
            path=resolved_path,
            mtime=float(root_stat.st_mtime),
        )
        stats = ScanStats(root_path=resolved_path, current_path=resolved_path)

        visited_inodes: set[tuple[int, int]] = set()
        if root_stat.st_ino != 0:
            visited_inodes.add((int(root_stat.st_dev), int(root_stat.st_ino)))
        seen_hardlinks: set[tuple[int, int]] = set()
        seen_error_paths: set[str] = set()

        start_time = time.perf_counter()
        last_progress_time = start_time

        max_workers = self.config.resolved_workers()
        max_in_flight = max_workers * 4
        pending_dirs: deque[DirNode] = deque([root_node])
        in_flight: set[concurrent.futures.Future[_DirScanOutput]] = set()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sv-scan"
        ) as executor:
            while (pending_dirs or in_flight) and not active_cancel.is_set():
                while pending_dirs and len(in_flight) < max_in_flight:
                    next_dir = pending_dirs.popleft()
                    fut = executor.submit(
                        _scan_single_dir,
                        next_dir,
                        self.config,
                        root_dev,
                        active_cancel,
                    )
                    in_flight.add(fut)

                if not in_flight:
                    break

                done, _ = concurrent.futures.wait(
                    in_flight,
                    timeout=self.config.progress_interval,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                for fut in done:
                    in_flight.remove(fut)
                    out = fut.result()
                    dnode = out.dir_node

                    if out.inaccessible:
                        dnode.inaccessible = True
                        dnode.error = out.error_message
                        if dnode.path not in seen_error_paths:
                            seen_error_paths.add(dnode.path)
                            stats.inaccessible_count += 1
                            stats.inaccessible_paths.append(dnode.path)

                    for err_path, err_msg in out.errors:
                        stats.errors.append((err_path, err_msg))
                        if err_path not in seen_error_paths:
                            seen_error_paths.add(err_path)
                            stats.inaccessible_count += 1
                            stats.inaccessible_paths.append(err_path)

                    dnode.own_size += out.own_size
                    dnode.children.extend(out.files)

                    for fnode, hl_key in out.hardlinks:
                        if hl_key in seen_hardlinks:
                            fnode.is_hardlink_duplicate = True
                            fnode.size = 0
                            stats.hardlinks_deduplicated += 1
                        else:
                            seen_hardlinks.add(hl_key)
                            dnode.own_size += fnode.size
                            stats.total_size += fnode.size
                        stats.total_files += 1
                        dnode.children.append(fnode)

                    stats.total_size += out.own_size
                    stats.total_files += out.file_count
                    stats.symlinks_skipped += out.symlinks_skipped
                    stats.current_path = dnode.path

                    for child_dir, inode_key in out.child_dirs:
                        if inode_key[1] != 0:
                            if inode_key in visited_inodes:
                                stats.symlinks_skipped += 1
                                continue
                            visited_inodes.add(inode_key)
                        dnode.children.append(child_dir)
                        stats.total_dirs += 1
                        pending_dirs.append(child_dir)

                now = time.perf_counter()
                if progress_callback is not None and (
                    now - last_progress_time >= self.config.progress_interval
                ):
                    stats.elapsed_seconds = now - start_time
                    progress_callback(stats.snapshot())
                    last_progress_time = now

            if active_cancel.is_set():
                stats.cancelled = True
                for fut in in_flight:
                    fut.cancel()
                pending_dirs.clear()

        _aggregate_tree(root_node)
        if self.config.sort_results:
            root_node.sort_children(key="size", reverse=True, recursive=True)

        stats.total_size = root_node.size
        stats.total_files = root_node.file_count
        stats.total_dirs = root_node.dir_count
        stats.elapsed_seconds = time.perf_counter() - start_time

        if progress_callback is not None:
            progress_callback(stats.snapshot())

        return ScanResult(root=root_node, stats=stats)

    async def scan_async(
        self,
        root_path: str | Path,
        *,
        progress_callback: Optional[AsyncProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ScanResult:
        """Asynchronously scan a directory tree, bridging telemetry back to the event loop."""
        loop = asyncio.get_running_loop()

        def _thread_progress(snapshot: ScanStats) -> None:
            if progress_callback is None:
                return
            result = progress_callback(snapshot)
            if asyncio.iscoroutine(result):
                loop.create_task(result)

        def _dispatch_progress(snapshot: ScanStats) -> None:
            if progress_callback is None:
                return
            loop.call_soon_threadsafe(_thread_progress, snapshot)

        cb = _dispatch_progress if progress_callback is not None else None
        return await asyncio.to_thread(
            self.scan,
            root_path,
            progress_callback=cb,
            cancel_event=cancel_event,
        )
