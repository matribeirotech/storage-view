"""Desktop GUI package (PySide6) for Storage View."""

from __future__ import annotations

from typing import Optional


def run_gui(initial_path: Optional[str] = None) -> int:
    """Launch the Storage View desktop GUI."""
    from storage_view.gui.app import launch_gui

    return launch_gui(initial_path=initial_path)


__all__ = ["run_gui"]
