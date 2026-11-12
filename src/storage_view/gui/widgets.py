"""Custom PySide6 widgets: Squarified Treemap, Usage Bar Delegate, Lazy File Tree, and Progress Panel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from storage_view.core.models import DirNode, FileNode, ScanStats, TreeNode
from storage_view.core.utils import format_bytes, format_duration, format_timestamp

_ROLE_SORT = Qt.ItemDataRole.UserRole
_ROLE_RATIO = Qt.ItemDataRole.UserRole + 1
_ROLE_LAZY_PENDING = Qt.ItemDataRole.UserRole + 2
_ROLE_NODE = Qt.ItemDataRole.UserRole + 3

_CATEGORY_COLORS: dict[str, str] = {
    "dir": "#2563eb",
    "video": "#7c3aed",
    "image": "#0d9488",
    "archive": "#d97706",
    "binary": "#e11d48",
    "code": "#0284c7",
    "other": "#475569",
    "bucket": "#334155",
}

_EXT_TO_CATEGORY: dict[str, str] = {
    ".mp4": "video",
    ".mkv": "video",
    ".avi": "video",
    ".mov": "video",
    ".wmv": "video",
    ".webm": "video",
    ".mp3": "video",
    ".flac": "video",
    ".wav": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".svg": "image",
    ".psd": "image",
    ".zip": "archive",
    ".7z": "archive",
    ".rar": "archive",
    ".tar": "archive",
    ".gz": "archive",
    ".xz": "archive",
    ".iso": "archive",
    ".vhd": "archive",
    ".vhdx": "archive",
    ".exe": "binary",
    ".msi": "binary",
    ".dll": "binary",
    ".so": "binary",
    ".bin": "binary",
    ".deb": "binary",
    ".whl": "binary",
    ".py": "code",
    ".rs": "code",
    ".js": "code",
    ".ts": "code",
    ".json": "code",
    ".md": "code",
    ".pdf": "code",
    ".log": "code",
    ".txt": "code",
    ".csv": "code",
}


def node_color(node: Optional[TreeNode], depth_hint: int = 0) -> QColor:
    """Choose a visually distinct dark-mode tile color based on node type/extension."""
    if node is None:
        return QColor(_CATEGORY_COLORS["bucket"])
    if isinstance(node, DirNode):
        if node.inaccessible:
            return QColor("#991b1b")
        base = QColor(_CATEGORY_COLORS["dir"])
        if depth_hint % 2 == 1:
            return base.darker(115)
        return base
    category = _EXT_TO_CATEGORY.get(node.extension, "other")
    return QColor(_CATEGORY_COLORS[category])


@dataclass(slots=True)
class _TreemapTile:
    rect: QRectF
    node: Optional[TreeNode]
    label: str
    size: int
    percent: float
    color: QColor


def _worst_aspect(areas: list[float], short_side: float) -> float:
    if not areas or short_side <= 0:
        return float("inf")
    total = sum(areas)
    if total <= 0:
        return float("inf")
    max_a = max(areas)
    min_a = min(areas)
    if min_a <= 0:
        return float("inf")
    s2 = short_side * short_side
    t2 = total * total
    return max((s2 * max_a) / t2, t2 / (s2 * min_a))


def _squarify_layout(
    items: list[tuple[Optional[TreeNode], str, int, QColor]],
    bounds: QRectF,
    total_dir_size: int,
) -> list[_TreemapTile]:
    """Compute a squarified treemap layout inside `bounds`."""
    if not items or bounds.width() < 2 or bounds.height() < 2:
        return []

    total_size = sum(item[2] for item in items)
    if total_size <= 0:
        return []

    total_area = bounds.width() * bounds.height()
    scale = total_area / total_size
    scaled: list[tuple[Optional[TreeNode], str, int, QColor, float]] = [
        (node, label, sz, color, sz * scale)
        for node, label, sz, color in items
        if sz > 0
    ]

    tiles: list[_TreemapTile] = []
    rx, ry, rw, rh = bounds.x(), bounds.y(), bounds.width(), bounds.height()
    row: list[tuple[Optional[TreeNode], str, int, QColor, float]] = []

    def _flush_row() -> None:
        nonlocal rx, ry, rw, rh, row
        if not row or rw <= 0 or rh <= 0:
            row = []
            return
        row_area = sum(entry[4] for entry in row)
        if row_area <= 0:
            row = []
            return

        if rw >= rh:
            strip_w = min(rw, row_area / rh)
            cur_y = ry
            for node, label, sz, color, area in row:
                tile_h = area / strip_w if strip_w > 0 else 0.0
                pct = (sz / total_dir_size * 100.0) if total_dir_size > 0 else 0.0
                tiles.append(
                    _TreemapTile(
                        rect=QRectF(rx, cur_y, strip_w, tile_h),
                        node=node,
                        label=label,
                        size=sz,
                        percent=pct,
                        color=color,
                    )
                )
                cur_y += tile_h
            rx += strip_w
            rw = max(0.0, rw - strip_w)
        else:
            strip_h = min(rh, row_area / rw)
            cur_x = rx
            for node, label, sz, color, area in row:
                tile_w = area / strip_h if strip_h > 0 else 0.0
                pct = (sz / total_dir_size * 100.0) if total_dir_size > 0 else 0.0
                tiles.append(
                    _TreemapTile(
                        rect=QRectF(cur_x, ry, tile_w, strip_h),
                        node=node,
                        label=label,
                        size=sz,
                        percent=pct,
                        color=color,
                    )
                )
                cur_x += tile_w
            ry += strip_h
            rh = max(0.0, rh - strip_h)
        row = []

    for entry in scaled:
        if rw < 1.0 or rh < 1.0:
            break
        short_side = min(rw, rh)
        if not row:
            row.append(entry)
            continue
        cur_areas = [r[4] for r in row]
        next_areas = cur_areas + [entry[4]]
        if _worst_aspect(next_areas, short_side) <= _worst_aspect(cur_areas, short_side):
            row.append(entry)
        else:
            _flush_row()
            row.append(entry)

    _flush_row()
    return tiles


class TreemapWidget(QWidget):
    """Interactive squarified treemap visualization synced with the active folder."""

    node_selected = Signal(object)
    directory_activated = Signal(object)
    context_menu_requested = Signal(object, QPoint)

    _MAX_INDIVIDUAL_TILES = 140

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(260, 220)
        self._current_dir: Optional[DirNode] = None
        self._filter_text: str = ""
        self._tiles: list[_TreemapTile] = []
        self._hovered_idx: int = -1
        self._selected_path: Optional[str] = None

    def set_directory(self, dir_node: Optional[DirNode], filter_text: str = "") -> None:
        self._current_dir = dir_node
        self._filter_text = filter_text.strip().lower()
        self._hovered_idx = -1
        self._rebuild_tiles()
        self.update()

    def set_selected_node(self, node: Optional[TreeNode]) -> None:
        self._selected_path = node.path if node is not None else None
        self.update()

    def _rebuild_tiles(self) -> None:
        self._tiles = []
        if self._current_dir is None:
            return

        children = [c for c in self._current_dir.children if c.size > 0]
        if self._filter_text:
            children = [c for c in children if self._filter_text in c.name.lower()]
        if not children:
            return

        children.sort(key=lambda n: n.size, reverse=True)
        total_size = sum(c.size for c in children)

        items: list[tuple[Optional[TreeNode], str, int, QColor]] = []
        for idx, child in enumerate(children[: self._MAX_INDIVIDUAL_TILES]):
            label = f"/{child.name}" if isinstance(child, DirNode) else child.name
            items.append((child, label, child.size, node_color(child, depth_hint=idx)))

        remainder = children[self._MAX_INDIVIDUAL_TILES :]
        if remainder:
            rem_size = sum(c.size for c in remainder)
            if rem_size > 0:
                items.append(
                    (
                        None,
                        f"Outros ({len(remainder)} itens)",
                        rem_size,
                        node_color(None),
                    )
                )

        margin = 6.0
        bounds = QRectF(
            margin,
            margin,
            max(0.0, self.width() - margin * 2),
            max(0.0, self.height() - margin * 2),
        )
        self._tiles = _squarify_layout(items, bounds, total_size)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._rebuild_tiles()

    def _tile_index_at(self, pos: QPoint) -> int:
        pt = QPoint(pos)
        for idx, tile in enumerate(self._tiles):
            if tile.rect.contains(pt.x(), pt.y()):
                return idx
        return -1

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        idx = self._tile_index_at(event.position().toPoint())
        if idx != self._hovered_idx:
            self._hovered_idx = idx
            if idx >= 0:
                tile = self._tiles[idx]
                node = tile.node
                if isinstance(node, DirNode):
                    extra = f"\nArquivos: {node.file_count:,} | Subpastas: {node.dir_count:,}"
                    path_line = f"\n{node.path}"
                elif isinstance(node, FileNode):
                    extra = ""
                    path_line = f"\n{node.path}"
                else:
                    extra = ""
                    path_line = ""
                self.setToolTip(
                    f"{tile.label}\n"
                    f"Tamanho: {format_bytes(tile.size)} ({tile.percent:.1f}%)"
                    f"{extra}{path_line}"
                )
            else:
                self.setToolTip("")
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._hovered_idx != -1:
            self._hovered_idx = -1
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        idx = self._tile_index_at(event.position().toPoint())
        if idx >= 0:
            node = self._tiles[idx].node
            if node is not None:
                self._selected_path = node.path
                self.node_selected.emit(node)
                self.update()
                if event.button() == Qt.MouseButton.RightButton:
                    self.context_menu_requested.emit(node, event.globalPosition().toPoint())
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._tile_index_at(event.position().toPoint())
            if idx >= 0:
                node = self._tiles[idx].node
                if isinstance(node, DirNode) and not node.inaccessible:
                    self.directory_activated.emit(node)
                    return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#0b1120"))

        if not self._tiles:
            painter.setPen(QColor("#64748b"))
            msg = (
                "Nenhum item com tamanho > 0 para exibir no Treemap."
                if self._current_dir is not None
                else "Selecione uma unidade ou pasta para iniciar a varredura."
            )
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, msg)
            return

        name_font = QFont(self.font())
        name_font.setPointSize(9)
        name_font.setBold(True)
        sub_font = QFont(self.font())
        sub_font.setPointSize(8)

        fm_name = QFontMetrics(name_font)
        fm_sub = QFontMetrics(sub_font)

        for idx, tile in enumerate(self._tiles):
            rect = tile.rect.adjusted(1.0, 1.0, -1.0, -1.0)
            if rect.width() < 2 or rect.height() < 2:
                continue

            is_hovered = idx == self._hovered_idx
            is_selected = (
                tile.node is not None and tile.node.path == self._selected_path
            )

            base_color = tile.color.lighter(118) if is_hovered else tile.color
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, base_color.lighter(112))
            grad.setColorAt(1.0, base_color.darker(120))

            painter.fillRect(rect, QBrush(grad))

            if is_selected:
                painter.setPen(QPen(QColor("#38bdf8"), 2.0))
            elif is_hovered:
                painter.setPen(QPen(QColor("#f8fafc"), 1.5))
            else:
                painter.setPen(QPen(QColor("#0f172a"), 1.0))
            painter.drawRect(rect)

            if rect.width() >= 54 and rect.height() >= 24:
                inner = rect.adjusted(6, 4, -6, -4)
                painter.setFont(name_font)
                painter.setPen(QColor("#f8fafc"))
                elided_name = fm_name.elidedText(
                    tile.label, Qt.TextElideMode.ElideRight, int(inner.width())
                )
                painter.drawText(
                    inner,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    elided_name,
                )

                if rect.height() >= 42:
                    painter.setFont(sub_font)
                    painter.setPen(QColor("#e2e8f0"))
                    size_line = f"{format_bytes(tile.size)} ({tile.percent:.1f}%)"
                    elided_sub = fm_sub.elidedText(
                        size_line, Qt.TextElideMode.ElideRight, int(inner.width())
                    )
                    sub_rect = inner.adjusted(0, fm_name.height() + 2, 0, 0)
                    painter.drawText(
                        sub_rect,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                        elided_sub,
                    )


class UsageBarDelegate(QStyledItemDelegate):
    """Paints a dark-mode percentage usage bar inside the tree table."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        super().paint(painter, option, index)
        ratio = index.data(_ROLE_RATIO)
        if ratio is None:
            return

        pct_float = max(0.0, min(1.0, float(ratio)))
        rect = option.rect.adjusted(6, 5, -6, -5)
        if rect.width() < 12 or rect.height() < 8:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1e293b"))
        painter.drawRoundedRect(rect, 4, 4)

        fill_w = max(0, int(rect.width() * pct_float))
        if fill_w > 0:
            fill_rect = rect.adjusted(0, 0, -(rect.width() - fill_w), 0)
            if pct_float >= 0.40:
                color = QColor("#f43f5e")
            elif pct_float >= 0.15:
                color = QColor("#f59e0b")
            else:
                color = QColor("#0ea5e9")
            painter.setBrush(color)
            painter.drawRoundedRect(fill_rect, 4, 4)

        painter.restore()


class NumericTreeWidgetItem(QTreeWidgetItem):
    """Tree item supporting accurate numeric sorting via UserRole metadata."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        lhs = self.data(col, _ROLE_SORT)
        rhs = other.data(col, _ROLE_SORT)
        if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
            return lhs < rhs
        return self.text(col).lower() < other.text(col).lower()


class FileTreeWidget(QTreeWidget):
    """Sortable hierarchical file tree with lazy child population."""

    directory_activated = Signal(object)
    node_selected = Signal(object)
    node_context_requested = Signal(object, QPoint)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setColumnCount(6)
        self.setHeaderLabels(["Nome", "Tamanho", "%", "Uso", "Arquivos", "Modificado"])
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setItemDelegateForColumn(3, UsageBarDelegate(self))

        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(1, 105)
        self.setColumnWidth(2, 68)
        self.setColumnWidth(3, 120)
        self.setColumnWidth(4, 85)
        self.setColumnWidth(5, 130)

        self._node_to_item: dict[str, QTreeWidgetItem] = {}
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_directory(self, dir_node: Optional[DirNode], filter_text: str = "") -> None:
        self.setSortingEnabled(False)
        self.clear()
        self._node_to_item.clear()

        if dir_node is None:
            self.setSortingEnabled(True)
            return

        needle = filter_text.strip().lower()
        parent_size = dir_node.size
        for child in dir_node.children:
            if needle and needle not in child.name.lower():
                continue
            item = self._create_item(child, parent_size)
            self.addTopLevelItem(item)
            self._node_to_item[child.path] = item

        self.setSortingEnabled(True)
        self.sortByColumn(self.sortColumn(), self.header().sortIndicatorOrder())

    def select_node_by_path(self, path: str) -> None:
        item = self._node_to_item.get(path)
        if item is not None:
            self.blockSignals(True)
            self.setCurrentItem(item)
            self.scrollToItem(item)
            self.blockSignals(False)

    def _create_item(self, node: TreeNode, parent_size: int) -> NumericTreeWidgetItem:
        pct = node.percentage_of(parent_size)
        ratio = pct / 100.0

        if isinstance(node, DirNode):
            prefix = "[!] " if node.inaccessible else ("[L] " if node.is_symlink else "📁 ")
            label = f"{prefix}{node.name}"
            files_text = f"{node.file_count:,}"
            files_sort = node.file_count
        else:
            prefix = "[L] " if node.is_symlink else "📄 "
            label = f"{prefix}{node.name}"
            files_text = "-"
            files_sort = 1

        item = NumericTreeWidgetItem(
            [
                label,
                format_bytes(node.size),
                f"{pct:.1f}%",
                "",
                files_text,
                format_timestamp(node.mtime),
            ]
        )

        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        item.setData(0, _ROLE_SORT, node.name.lower())
        item.setData(1, _ROLE_SORT, node.size)
        item.setData(2, _ROLE_SORT, pct)
        item.setData(3, _ROLE_SORT, ratio)
        item.setData(3, _ROLE_RATIO, ratio)
        item.setData(4, _ROLE_SORT, files_sort)
        item.setData(5, _ROLE_SORT, node.mtime)
        item.setData(0, _ROLE_NODE, node)

        if isinstance(node, DirNode) and node.inaccessible:
            item.setForeground(0, QColor("#f87171"))
            item.setToolTip(0, node.error or "Access Denied")
        elif isinstance(node, DirNode):
            item.setForeground(0, QColor("#93c5fd"))
            if node.children:
                placeholder = QTreeWidgetItem(["Carregando..."])
                placeholder.setData(0, _ROLE_LAZY_PENDING, True)
                item.addChild(placeholder)
        elif node.is_symlink:
            item.setForeground(0, QColor("#fbbf24"))

        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() != 1:
            return
        first = item.child(0)
        if not first.data(0, _ROLE_LAZY_PENDING):
            return

        node = item.data(0, _ROLE_NODE)
        if not isinstance(node, DirNode):
            return

        self.setSortingEnabled(False)
        item.takeChild(0)
        parent_size = node.size
        for child in node.children:
            child_item = self._create_item(child, parent_size)
            item.addChild(child_item)
            self._node_to_item[child.path] = child_item
        self.setSortingEnabled(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        node = item.data(0, _ROLE_NODE)
        if isinstance(node, DirNode) and not node.inaccessible:
            self.directory_activated.emit(node)

    def _on_current_item_changed(
        self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]
    ) -> None:
        if current is None:
            return
        node = current.data(0, _ROLE_NODE)
        if node is not None:
            self.node_selected.emit(node)

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        node = item.data(0, _ROLE_NODE)
        if node is not None:
            self.node_context_requested.emit(node, self.viewport().mapToGlobal(pos))


class ScanProgressPanel(QWidget):
    """Real-time progress and throughput banner for active and completed scans."""

    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("progressPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        self.status_label = QLabel("Pronto para escanear")
        self.status_label.setObjectName("statusHeadline")
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("speedMetrics")
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)

        top_row.addWidget(self.status_label, 1)
        top_row.addWidget(self.speed_label)
        top_row.addWidget(self.cancel_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        layout.addLayout(top_row)
        layout.addWidget(self.progress_bar)

    def set_scanning(self, root_path: str) -> None:
        self.status_label.setText(f"Escaneando: {root_path}")
        self.speed_label.setText("0 arquivos/s | 0 B")
        self.progress_bar.setRange(0, 0)  # Indeterminate pulse while walking tree
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)

    def update_stats(self, stats: ScanStats) -> None:
        cur = stats.current_path
        if len(cur) > 65:
            cur = "..." + cur[-62:]
        self.status_label.setText(f"Escaneando: {cur}")
        self.speed_label.setText(
            f"{format_bytes(stats.total_size)}  |  "
            f"{stats.total_files:,} arquivos  |  "
            f"{stats.files_per_second:,.0f} arquivos/s"
        )

    def set_finished(self, stats: ScanStats) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_btn.setVisible(False)
        suffix = " (Cancelado - parcial)" if stats.cancelled else ""
        self.status_label.setText(
            f"Concluído{suffix}: {stats.root_path}"
        )
        self.speed_label.setText(
            f"{format_bytes(stats.total_size)}  |  "
            f"{stats.total_files:,} arquivos  |  "
            f"{stats.total_dirs:,} pastas  |  "
            f"{format_duration(stats.elapsed_seconds)} ({stats.files_per_second:,.0f} arq/s)"
        )

    def set_error(self, message: str) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_btn.setVisible(False)
        self.status_label.setText(f"Erro: {message}")
        self.speed_label.setText("")
