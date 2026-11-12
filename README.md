# Storage View

[![Maintained by LYPSYOS Open Source Community](https://img.shields.io/badge/Maintained%20by-LYPSYOS%20OSS%20Community-2457F5?style=for-the-badge)](https://lypsyos.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-10B981?style=for-the-badge)](LICENSE)

**Storage View** is an open-source, high-performance disk space analyzer with a decoupled scanning core (`storage_view.core`), a dark-mode desktop **GUI for Windows** (`PySide6`), and an interactive **TUI** (`Textual`) + **Headless CLI** (`Rich`) for Linux servers and terminals.

> Part of the **[LYPSYOS Open Source Lab & Community](https://lypsyos.com)** ecosystem — lightweight, transparent tools for developers and infrastructure.

---

## Features

- **High-Throughput Core Engine (`storage_view.core`)**
  - Multithreaded / asynchronous directory traversal powered by `os.scandir` and bounded `ThreadPoolExecutor` pools (zero recursion deadlocks).
  - Recursive folder size aggregation, file/directory counting, and hardlink deduplication.
  - Symlink and NTFS junction detection + `(st_dev, st_ino)` cycle protection to prevent infinite loops.
  - Resilient handling of `PermissionError` / `Access Denied` and transient `OSError`s without aborting the scan.
  - Structured in-memory node tree (`DirNode`, `FileNode`, `ScanStats`) with JSON and CSV export.

- **Windows Desktop GUI (`storage_view.gui`)**
  - Modern Dark Mode desktop UI built with **PySide6 (Qt 6)**.
  - Interactive **Squarified Treemap** synchronized bidirectionally with a sortable, lazy-loading hierarchical file tree and usage progress bars.
  - Real-time scan telemetry showing scanned bytes, file count, and throughput (`files/s`).
  - Native context menu: **Abrir no Explorer**, **Copiar Caminho**, and **Mover para a Lixeira** (`send2trash`) with automatic tree recalculation.

- **Linux / Terminal TUI & Headless CLI (`storage_view.cli`)**
  - **Interactive TUI (`ncdu`-style):** built with **Textual** — Vim (`j/k/h/l`) and arrow-key navigation, drill-down, proportional Unicode usage bars, live filter (`/`), sorting (`s`), Trash action (`d`), and JSON export (`e`).
  - **Headless CLI:** built with **Rich** and **Typer** — ideal for SSH sessions, cron jobs, and scripts (`--top`, `--min-size`, `--json`, `--csv`).

---

## Project Structure

```text
storage-view/
├── README.md
├── LICENSE
├── pyproject.toml
├── src/
│   └── storage_view/
│       ├── __init__.py
│       ├── main.py          # Argument router (GUI vs TUI vs Headless CLI)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── scanner.py   # Concurrent os.scandir scanning engine
│       │   ├── models.py    # FileNode, DirNode, ScanStats, ScanResult
│       │   └── utils.py     # Byte formatting, filters, JSON/CSV export, OS helpers
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── tui_app.py   # Textual ncdu-style interactive TUI
│       │   └── reporter.py  # Rich headless terminal table reporter
│       └── gui/
│           ├── __init__.py
│           ├── app.py       # Main PySide6 Dark Mode window
│           └── widgets.py   # Squarified Treemap, UsageBarDelegate, FileTreeWidget
└── tests/
    └── test_scanner.py
```

---

## Installation

Requires **Python 3.11+**.

### Using `uv` (recommended for development)

```bash
uv sync --all-extras
uv run storage-view --help
```

### Using `pip` / `pipx` (Linux CLI/TUI)

```bash
pipx install .
# Or with optional GUI support on Linux desktop:
pip install ".[gui]"
```

### Using `pip` (Windows with GUI)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

---

## Usage

### Smart Mode Routing (`storage-view`)

- **Windows:** `storage-view` (or `storage-view C:\Users`) launches the **Desktop GUI** automatically unless CLI/TUI flags (`--tui`, `--headless`, `--top`, `--min-size`, `--json`, `--csv`) are supplied.
- **Linux / macOS:** `storage-view` (or `storage-view /var/log`) launches the **Interactive TUI** in a terminal, or falls back to **Headless CLI** when piped or given query flags.

### Examples

```bash
# Launch Desktop GUI (Windows, or Linux with [gui] extra)
storage-view --gui
storage-view C:\Projects --gui

# Launch ncdu-style interactive TUI
storage-view /var/log --tui

# Headless CLI for servers / SSH / scripts
storage-view /var/log --top 20 --min-size 100M
storage-view /home --type dirs --top 15 --one-file-system
storage-view . --ext .log --ext .tmp --exclude "*.git*" --top 30

# Export structured reports
storage-view /data --json > report.json
storage-view /data --csv --min-size 10M > large_items.csv
storage-view /data --export-json scan.json --export-csv scan.csv --top 25
```

### TUI Keyboard Shortcuts

| Key | Action |
| --- | --- |
| `↑` / `k`, `↓` / `j` | Move selection up / down |
| `Enter` / `→` / `l` | Open selected directory |
| `Backspace` / `←` / `h` | Go to parent directory |
| `g` / `G` | Jump to top / bottom |
| `s` | Cycle sort (`size`, `name`, `files`, `mtime`) |
| `/` | Filter current directory (`Esc` to clear) |
| `d` | Move selected item to Trash (with confirmation) |
| `e` | Export scan to `storage-view-export.json` |
| `i` | Show scan stats & inaccessible paths |
| `r` | Rescan |
| `q` | Quit |

---

## Packaging

### Windows Standalone Executable (`.exe`)

```powershell
pip install ".[dev]"
pyinstaller --noconfirm --onefile --windowed --name storage-view src/storage_view/main.py
```

---

## Running Tests

```bash
pytest
```

## Contributing & Community

Contributions are welcome! Check [`CONTRIBUTING.md`](CONTRIBUTING.md), our [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), and issues labeled `good first issue`.

- Discuss ideas in **GitHub Discussions** or leave feedback on [lypsyos.com/projetos](https://lypsyos.com/projetos?projeto=storage-view).

## License

Released under the [MIT License](LICENSE). Maintained by **[LYPSYOS Open Source Community](https://lypsyos.com)** (`@matribeirotech`).
