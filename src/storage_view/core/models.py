"""Data structures representing scanned files, directories, and scan telemetry."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Union


@dataclass(slots=True)
class FileNode:
    """Represents a regular file (or symlink entry) in the scanned tree."""

    name: str
    path: str
    size: int = 0
    mtime: float = 0.0
    is_symlink: bool = False
    is_hardlink_duplicate: bool = False
    parent: Optional[DirNode] = field(default=None, repr=False, compare=False)

    @property
    def is_dir(self) -> bool:
        return False

    @property
    def extension(self) -> str:
        _, ext = os.path.splitext(self.name)
        return ext.lower()

    def percentage_of(self, total_size: int) -> float:
        if total_size <= 0:
            return 0.0
        return (self.size / total_size) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "file",
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "is_symlink": self.is_symlink,
            "is_hardlink_duplicate": self.is_hardlink_duplicate,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], parent: Optional[DirNode] = None
    ) -> FileNode:
        return cls(
            name=str(data["name"]),
            path=str(data["path"]),
            size=int(data.get("size", 0)),
            mtime=float(data.get("mtime", 0.0)),
            is_symlink=bool(data.get("is_symlink", False)),
            is_hardlink_duplicate=bool(data.get("is_hardlink_duplicate", False)),
            parent=parent,
        )


@dataclass(slots=True)
class DirNode:
    """Represents a directory and its aggregated recursive metrics."""

    name: str
    path: str
    size: int = 0
    own_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    mtime: float = 0.0
    is_symlink: bool = False
    inaccessible: bool = False
    error: Optional[str] = None
    children: list[TreeNode] = field(default_factory=list)
    parent: Optional[DirNode] = field(default=None, repr=False, compare=False)

    @property
    def is_dir(self) -> bool:
        return True

    def percentage_of(self, total_size: int) -> float:
        if total_size <= 0:
            return 0.0
        return (self.size / total_size) * 100.0

    def sort_children(
        self,
        key: str = "size",
        reverse: bool = True,
        recursive: bool = True,
    ) -> None:
        """Sort child nodes in place by size, name, files, or mtime."""
        if key == "name":
            sort_fn = lambda n: (not n.is_dir, n.name.lower())
        elif key == "files":
            sort_fn = lambda n: (n.file_count if isinstance(n, DirNode) else 1, n.size)
        elif key == "mtime":
            sort_fn = lambda n: (n.mtime, n.size)
        else:  # "size"
            sort_fn = lambda n: (n.size, n.name.lower())

        self.children.sort(key=sort_fn, reverse=reverse)
        if recursive:
            for child in self.children:
                if isinstance(child, DirNode):
                    child.sort_children(key=key, reverse=reverse, recursive=True)

    def remove_child(self, target: TreeNode) -> bool:
        """Remove a direct child and propagate size/count reductions upward."""
        idx = -1
        for i, child in enumerate(self.children):
            if child is target or child.path == target.path:
                idx = i
                break
        if idx < 0:
            return False

        removed = self.children.pop(idx)
        removed.parent = None

        delta_size = removed.size
        if isinstance(removed, DirNode):
            delta_files = removed.file_count
            delta_dirs = 1 + removed.dir_count
        else:
            delta_files = 1
            delta_dirs = 0
            self.own_size = max(0, self.own_size - delta_size)

        cursor: Optional[DirNode] = self
        while cursor is not None:
            cursor.size = max(0, cursor.size - delta_size)
            cursor.file_count = max(0, cursor.file_count - delta_files)
            cursor.dir_count = max(0, cursor.dir_count - delta_dirs)
            cursor = cursor.parent

        return True

    def iter_nodes(self, include_self: bool = False) -> Iterator[TreeNode]:
        """Depth-first iterator yielding all nodes in this subtree."""
        if include_self:
            yield self
        stack: list[TreeNode] = list(reversed(self.children))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, DirNode) and node.children:
                stack.extend(reversed(node.children))

    def to_dict(
        self, max_depth: Optional[int] = None, _depth: int = 0
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": "dir",
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "own_size": self.own_size,
            "file_count": self.file_count,
            "dir_count": self.dir_count,
            "mtime": self.mtime,
            "is_symlink": self.is_symlink,
            "inaccessible": self.inaccessible,
            "error": self.error,
        }
        if max_depth is None or _depth < max_depth:
            data["children"] = [
                child.to_dict(max_depth=max_depth, _depth=_depth + 1)
                if isinstance(child, DirNode)
                else child.to_dict()
                for child in self.children
            ]
        return data

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], parent: Optional[DirNode] = None
    ) -> DirNode:
        node = cls(
            name=str(data["name"]),
            path=str(data["path"]),
            size=int(data.get("size", 0)),
            own_size=int(data.get("own_size", 0)),
            file_count=int(data.get("file_count", 0)),
            dir_count=int(data.get("dir_count", 0)),
            mtime=float(data.get("mtime", 0.0)),
            is_symlink=bool(data.get("is_symlink", False)),
            inaccessible=bool(data.get("inaccessible", False)),
            error=data.get("error"),
            parent=parent,
        )
        for child_data in data.get("children", []):
            if child_data.get("type") == "dir":
                node.children.append(cls.from_dict(child_data, parent=node))
            else:
                node.children.append(FileNode.from_dict(child_data, parent=node))
        return node


TreeNode = Union[DirNode, FileNode]


@dataclass(slots=True)
class ScanStats:
    """Live and final telemetry metrics for a directory scan."""

    root_path: str
    total_size: int = 0
    total_files: int = 0
    total_dirs: int = 0
    symlinks_skipped: int = 0
    hardlinks_deduplicated: int = 0
    inaccessible_count: int = 0
    inaccessible_paths: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    current_path: str = ""

    @property
    def files_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.total_files / self.elapsed_seconds

    @property
    def items_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return (self.total_files + self.total_dirs) / self.elapsed_seconds

    @property
    def bytes_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.total_size / self.elapsed_seconds

    def snapshot(self) -> ScanStats:
        """Create a lightweight value copy safe to send across threads/UI."""
        return ScanStats(
            root_path=self.root_path,
            total_size=self.total_size,
            total_files=self.total_files,
            total_dirs=self.total_dirs,
            symlinks_skipped=self.symlinks_skipped,
            hardlinks_deduplicated=self.hardlinks_deduplicated,
            inaccessible_count=self.inaccessible_count,
            inaccessible_paths=list(self.inaccessible_paths),
            errors=list(self.errors),
            elapsed_seconds=self.elapsed_seconds,
            cancelled=self.cancelled,
            current_path=self.current_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_path": self.root_path,
            "total_size": self.total_size,
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
            "symlinks_skipped": self.symlinks_skipped,
            "hardlinks_deduplicated": self.hardlinks_deduplicated,
            "inaccessible_count": self.inaccessible_count,
            "inaccessible_paths": list(self.inaccessible_paths),
            "errors": [{"path": p, "message": m} for p, m in self.errors],
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "files_per_second": round(self.files_per_second, 2),
            "cancelled": self.cancelled,
        }


@dataclass(slots=True)
class ScanResult:
    """Complete result object returned by StorageScanner."""

    root: DirNode
    stats: ScanStats

    def to_dict(self, max_depth: Optional[int] = None) -> dict[str, Any]:
        return {
            "stats": self.stats.to_dict(),
            "tree": self.root.to_dict(max_depth=max_depth),
        }
