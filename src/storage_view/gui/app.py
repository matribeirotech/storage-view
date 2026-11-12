"""Main Windows Desktop GUI (PySide6) for Storage View."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QStorageInfo, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from storage_view.core.models import DirNode, ScanResult, ScanStats, TreeNode
from storage_view.core.scanner import ScanConfig, StorageScanner
from storage_view.core.utils import (
    export_to_csv,
    export_to_json,
    format_bytes,
    move_to_trash,
    reveal_in_file_manager,
)
from storage_view.gui.widgets import FileTreeWidget, ScanProgressPanel, TreemapWidget

DARK_STYLESHEET = """
QMainWindow, QDialog {
    background-color: #0b1120;
    color: #e2e8f0;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
}
QWidget#topBar, QWidget#progressPanel, QWidget#breadcrumbBar {
    background-color: #111827;
    border: 1px solid #1e293b;
    border-radius: 8px;
}
QLabel#statusHeadline {
    color: #f8fafc;
    font-weight: 600;
}
QLabel#speedMetrics {
    color: #38bdf8;
    font-weight: 600;
}
QLineEdit, QComboBox {
    background-color: #0f172a;
    color: #f1f5f9;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 24px;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #38bdf8;
}
QPushButton {
    background-color: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #475569;
}
QPushButton#primaryBtn {
    background-color: #0284c7;
    border-color: #0ea5e9;
    color: #ffffff;
}
QPushButton#primaryBtn:hover {
    background-color: #0ea5e9;
}
QPushButton#dangerBtn {
    background-color: #991b1b;
    border-color: #dc2626;
    color: #ffffff;
}
QPushButton#dangerBtn:hover {
    background-color: #dc2626;
}
QPushButton#crumbBtn {
    background-color: transparent;
    border: none;
    color: #93c5fd;
    padding: 2px 6px;
    font-weight: 600;
}
QPushButton#crumbBtn:hover {
    background-color: #1e293b;
    border-radius: 4px;
    color: #38bdf8;
}
QTreeWidget {
    background-color: #0f172a;
    alternate-background-color: #131c31;
    color: #e2e8f0;
    border: 1px solid #1e293b;
    border-radius: 8px;
    outline: 0;
}
QTreeWidget::item {
    padding: 4px 2px;
}
QTreeWidget::item:selected {
    background-color: #1d4ed8;
    color: #ffffff;
}
QTreeWidget::item:hover:!selected {
    background-color: #1e293b;
}
QHeaderView::section {
    background-color: #111827;
    color: #94a3b8;
    border: none;
    border-bottom: 1px solid #334155;
    border-right: 1px solid #1e293b;
    padding: 6px 8px;
    font-weight: 600;
}
QProgressBar {
    background-color: #1e293b;
    border: none;
    border-radius: 3px;
}
QProgressBar::chunk {
    background-color: #38bdf8;
    border-radius: 3px;
}
QMenu {
    background-color: #111827;
    color: #e2e8f0;
    border: 1px solid #334155;
    padding: 4px;
}
QMenu::item {
    padding: 6px 20px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #0284c7;
    color: #ffffff;
}
QStatusBar {
    background-color: #111827;
    color: #94a3b8;
    border-top: 1px solid #1e293b;
}
QSplitter::handle {
    background-color: #1e293b;
    width: 4px;
}
"""


class _ScanWorker(QObject):
    """Qt worker executing StorageScanner inside a background QThread."""

    progress_updated = Signal(object)
    scan_finished = Signal(object)
    scan_failed = Signal(str)

    def __init__(self, target_path: str, config: ScanConfig) -> None:
        super().__init__()
        self.target_path = target_path
        self.scanner = StorageScanner(config=config)

    @Slot()
    def run(self) -> None:
        try:
            result = self.scanner.scan(
                self.target_path,
                progress_callback=lambda stats: self.progress_updated.emit(stats),
            )
            self.scan_finished.emit(result)
        except Exception as exc:
            self.scan_failed.emit(str(exc))

    def cancel(self) -> None:
        self.scanner.cancel()


class _InaccessibleDialog(QDialog):
    """Modal dialog listing folders/files skipped due to permissions or OS errors."""

    def __init__(self, stats: ScanStats, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Caminhos Inacessíveis")
        self.resize(680, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"Total de caminhos inacessíveis ignorados sem interromper a varredura: "
                f"{stats.inaccessible_count:,}"
            )
        )
        list_widget = QListWidget()
        for err_path, err_msg in stats.errors[:500]:
            list_widget.addItem(f"{err_path}  —  {err_msg}")
        layout.addWidget(list_widget, 1)

        close_btn = QPushButton("Fechar")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)


class StorageViewWindow(QMainWindow):
    """Main desktop window combining hierarchical tree, treemap, and native actions."""

    def __init__(self, initial_path: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("Storage View — Analisador de Disco")
        self.resize(1320, 820)

        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[_ScanWorker] = None
        self._scan_result: Optional[ScanResult] = None
        self._current_dir: Optional[DirNode] = None

        self._build_ui()
        self._populate_drives()

        if initial_path:
            resolved = os.path.abspath(initial_path)
            self.path_input.setText(resolved)
            self.start_scan(resolved)

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 8)
        root_layout.setSpacing(8)

        # Top control bar
        top_bar = QWidget()
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)

        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(220)
        self.drive_combo.activated.connect(self._on_drive_selected)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Caminho da pasta ou unidade (ex: C:\\)")
        self.path_input.returnPressed.connect(self._on_scan_clicked)

        self.browse_btn = QPushButton("Pasta...")
        self.browse_btn.clicked.connect(self._on_browse_clicked)

        self.scan_btn = QPushButton("Escanear")
        self.scan_btn.setObjectName("primaryBtn")
        self.scan_btn.clicked.connect(self._on_scan_clicked)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filtrar nome/extensão...")
        self.filter_input.setMaximumWidth(210)
        self.filter_input.textChanged.connect(self._on_filter_changed)

        self.export_json_btn = QPushButton("JSON")
        self.export_json_btn.setToolTip("Exportar resultado da varredura em JSON")
        self.export_json_btn.clicked.connect(self._on_export_json)

        self.export_csv_btn = QPushButton("CSV")
        self.export_csv_btn.setToolTip("Exportar resultado da varredura em CSV")
        self.export_csv_btn.clicked.connect(self._on_export_csv)

        top_layout.addWidget(QLabel("Unidade:"))
        top_layout.addWidget(self.drive_combo)
        top_layout.addWidget(self.path_input, 1)
        top_layout.addWidget(self.browse_btn)
        top_layout.addWidget(self.scan_btn)
        top_layout.addWidget(self.filter_input)
        top_layout.addWidget(self.export_json_btn)
        top_layout.addWidget(self.export_csv_btn)

        # Progress telemetry panel
        self.progress_panel = ScanProgressPanel()
        self.progress_panel.cancel_requested.connect(self._on_cancel_scan)

        # Navigation breadcrumb bar
        crumb_bar = QWidget()
        crumb_bar.setObjectName("breadcrumbBar")
        self.crumb_layout = QHBoxLayout(crumb_bar)
        self.crumb_layout.setContentsMargins(10, 5, 10, 5)
        self.crumb_layout.setSpacing(4)

        self.up_btn = QPushButton("⬆ Subir")
        self.up_btn.setEnabled(False)
        self.up_btn.clicked.connect(self._on_go_up)
        self.crumb_layout.addWidget(self.up_btn)
        self.crumb_items_container = QHBoxLayout()
        self.crumb_items_container.setSpacing(2)
        self.crumb_layout.addLayout(self.crumb_items_container)
        self.crumb_layout.addStretch(1)

        # Main split view: Tree table + Treemap
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree_widget = FileTreeWidget()
        self.treemap_widget = TreemapWidget()

        self.tree_widget.directory_activated.connect(self._navigate_to_dir)
        self.tree_widget.node_selected.connect(self.treemap_widget.set_selected_node)
        self.tree_widget.node_context_requested.connect(self._show_context_menu)

        self.treemap_widget.directory_activated.connect(self._navigate_to_dir)
        self.treemap_widget.node_selected.connect(self._on_treemap_node_selected)
        self.treemap_widget.context_menu_requested.connect(self._show_context_menu)

        splitter.addWidget(self.tree_widget)
        splitter.addWidget(self.treemap_widget)
        splitter.setSizes([720, 580])

        root_layout.addWidget(top_bar)
        root_layout.addWidget(self.progress_panel)
        root_layout.addWidget(crumb_bar)
        root_layout.addWidget(splitter, 1)

        self.setCentralWidget(central)

        # Status bar
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.errors_btn = QPushButton("")
        self.errors_btn.setVisible(False)
        self.errors_btn.clicked.connect(self._show_inaccessible_dialog)
        status_bar.addPermanentWidget(self.errors_btn)

    def _populate_drives(self) -> None:
        self.drive_combo.clear()
        volumes = QStorageInfo.mountedVolumes()
        for vol in volumes:
            if not vol.isValid() or not vol.isReady():
                continue
            root = vol.rootPath()
            total = vol.bytesTotal()
            free = vol.bytesAvailable()
            used = max(0, total - free)
            label = f"{root}  ({format_bytes(used)} / {format_bytes(total)})"
            self.drive_combo.addItem(label, root)

        if self.drive_combo.count() > 0 and not self.path_input.text():
            self.path_input.setText(self.drive_combo.currentData())

    def _on_drive_selected(self, index: int) -> None:
        path = self.drive_combo.itemData(index)
        if path:
            self.path_input.setText(str(path))
            self.start_scan(str(path))

    def _on_browse_clicked(self) -> None:
        start_dir = self.path_input.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Selecionar Pasta para Análise", start_dir
        )
        if chosen:
            self.path_input.setText(chosen)
            self.start_scan(chosen)

    def _on_scan_clicked(self) -> None:
        target = self.path_input.text().strip()
        if not target:
            QMessageBox.warning(self, "Caminho inválido", "Informe um diretório válido.")
            return
        self.start_scan(target)

    def start_scan(self, target_path: str) -> None:
        if not os.path.exists(target_path):
            QMessageBox.warning(
                self, "Não encontrado", f"O caminho não existe:\n{target_path}"
            )
            return

        self._stop_active_scan()

        self.scan_btn.setEnabled(False)
        self.errors_btn.setVisible(False)
        self.progress_panel.set_scanning(target_path)

        thread = QThread(self)
        worker = _ScanWorker(target_path, ScanConfig())
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress_updated.connect(self.progress_panel.update_stats)
        worker.scan_finished.connect(self._on_scan_finished)
        worker.scan_failed.connect(self._on_scan_failed)
        worker.scan_finished.connect(thread.quit)
        worker.scan_failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._scan_thread = thread
        self._scan_worker = worker
        thread.start()

    def _stop_active_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
        self._scan_worker = None
        self._scan_thread = None

    @Slot()
    def _on_cancel_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()
            self.progress_panel.cancel_btn.setEnabled(False)

    @Slot(object)
    def _on_scan_finished(self, result: ScanResult) -> None:
        self._scan_result = result
        self._scan_worker = None
        self._scan_thread = None
        self.scan_btn.setEnabled(True)

        self.progress_panel.set_finished(result.stats)
        if result.stats.inaccessible_count > 0:
            self.errors_btn.setText(
                f"⚠ {result.stats.inaccessible_count:,} pastas inacessíveis"
            )
            self.errors_btn.setVisible(True)
        else:
            self.errors_btn.setVisible(False)

        self._navigate_to_dir(result.root)

    @Slot(str)
    def _on_scan_failed(self, message: str) -> None:
        self._scan_worker = None
        self._scan_thread = None
        self.scan_btn.setEnabled(True)
        self.progress_panel.set_error(message)
        QMessageBox.critical(self, "Erro na varredura", message)

    def _navigate_to_dir(self, dir_node: DirNode) -> None:
        self._current_dir = dir_node
        self._refresh_views()
        self._rebuild_breadcrumbs()

    def _refresh_views(self) -> None:
        needle = self.filter_input.text()
        self.tree_widget.set_directory(self._current_dir, filter_text=needle)
        self.treemap_widget.set_directory(self._current_dir, filter_text=needle)
        self.up_btn.setEnabled(
            self._current_dir is not None and self._current_dir.parent is not None
        )
        if self._current_dir is not None:
            self.statusBar().showMessage(
                f"Pasta atual: {self._current_dir.path}  |  "
                f"Tamanho: {format_bytes(self._current_dir.size)}  |  "
                f"Itens diretos: {len(self._current_dir.children):,}"
            )

    def _rebuild_breadcrumbs(self) -> None:
        while self.crumb_items_container.count():
            item = self.crumb_items_container.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if self._current_dir is None:
            return

        chain: list[DirNode] = []
        cursor: Optional[DirNode] = self._current_dir
        while cursor is not None:
            chain.append(cursor)
            cursor = cursor.parent
        chain.reverse()

        for idx, node in enumerate(chain):
            if idx > 0:
                sep = QLabel("›")
                sep.setStyleSheet("color: #64748b;")
                self.crumb_items_container.addWidget(sep)
            btn = QPushButton(node.name)
            btn.setObjectName("crumbBtn")
            btn.clicked.connect(lambda _checked=False, n=node: self._navigate_to_dir(n))
            self.crumb_items_container.addWidget(btn)

    def _on_go_up(self) -> None:
        if self._current_dir is not None and self._current_dir.parent is not None:
            self._navigate_to_dir(self._current_dir.parent)

    def _on_filter_changed(self, _text: str) -> None:
        self._refresh_views()

    def _on_treemap_node_selected(self, node: TreeNode) -> None:
        self.tree_widget.select_node_by_path(node.path)

    def _show_context_menu(self, node: TreeNode, global_pos: QPoint) -> None:
        menu = QMenu(self)

        if isinstance(node, DirNode) and not node.inaccessible:
            open_dir_act = QAction("Explorar Pasta", self)
            open_dir_act.triggered.connect(lambda: self._navigate_to_dir(node))
            menu.addAction(open_dir_act)
            menu.addSeparator()

        explorer_act = QAction("Abrir no Explorer", self)
        explorer_act.triggered.connect(lambda: self._open_in_explorer(node))
        menu.addAction(explorer_act)

        copy_act = QAction("Copiar Caminho", self)
        copy_act.triggered.connect(lambda: self._copy_path(node))
        menu.addAction(copy_act)

        menu.addSeparator()

        trash_act = QAction("Mover para a Lixeira", self)
        trash_act.triggered.connect(lambda: self._trash_node(node))
        menu.addAction(trash_act)

        menu.exec(global_pos)

    def _open_in_explorer(self, node: TreeNode) -> None:
        try:
            reveal_in_file_manager(node.path)
        except Exception as exc:
            QMessageBox.warning(self, "Erro", f"Não foi possível abrir o Explorer:\n{exc}")

    def _copy_path(self, node: TreeNode) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(node.path)
            self.statusBar().showMessage(f"Caminho copiado: {node.path}", 4000)

    def _trash_node(self, node: TreeNode) -> None:
        reply = QMessageBox.question(
            self,
            "Mover para a Lixeira",
            f"Deseja mover '{node.name}' ({format_bytes(node.size)}) para a Lixeira?\n\n"
            f"Caminho: {node.path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            move_to_trash(node.path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Erro ao mover para Lixeira", f"Falha ao mover item:\n{exc}"
            )
            return

        if node.parent is not None:
            node.parent.remove_child(node)
        elif self._current_dir is not None:
            self._current_dir.remove_child(node)
        if self._scan_result is not None:
            self._scan_result.stats.total_size = self._scan_result.root.size
            self._scan_result.stats.total_files = self._scan_result.root.file_count
            self._scan_result.stats.total_dirs = self._scan_result.root.dir_count
            self.progress_panel.set_finished(self._scan_result.stats)

        self._refresh_views()
        self.statusBar().showMessage(
            f"'{node.name}' movido para a Lixeira ({format_bytes(node.size)} liberados).",
            5000,
        )

    def _on_export_json(self) -> None:
        if self._scan_result is None:
            QMessageBox.information(self, "Sem dados", "Execute uma varredura antes de exportar.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Exportar JSON", "storage-view-report.json", "JSON (*.json)"
        )
        if file_path:
            export_to_json(self._scan_result, file_path)
            self.statusBar().showMessage(f"Exportado para {file_path}", 5000)

    def _on_export_csv(self) -> None:
        if self._scan_result is None:
            QMessageBox.information(self, "Sem dados", "Execute uma varredura antes de exportar.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Exportar CSV", "storage-view-report.csv", "CSV (*.csv)"
        )
        if file_path:
            export_to_csv(self._scan_result, file_path)
            self.statusBar().showMessage(f"Exportado para {file_path}", 5000)

    def _show_inaccessible_dialog(self) -> None:
        if self._scan_result is not None:
            dlg = _InaccessibleDialog(self._scan_result.stats, self)
            dlg.exec()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_active_scan()
        super().closeEvent(event)


def launch_gui(initial_path: Optional[str] = None) -> int:
    """Create the QApplication, apply Dark Mode styling, and start the event loop."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    window = StorageViewWindow(initial_path=initial_path)
    window.show()
    return int(app.exec())
