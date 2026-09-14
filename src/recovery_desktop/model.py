from pathlib import PureWindowsPath

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel, Signal
from PySide6.QtGui import QColor


def format_size(value):
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024


def filename(item):
    return PureWindowsPath(item.get("original_path") or item["observed_path"]).name


def category(item):
    suffix = PureWindowsPath(filename(item)).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".heic", ".raw", ".cr2"}:
        return "图片"
    if suffix in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt", ".md", ".csv", ".odt"}:
        return "文档"
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a"}:
        return "影音"
    return "其他"


class CandidateModel(QAbstractTableModel):
    selection_changed = Signal()
    headers = ("", "文件名称", "类型", "大小", "原位置", "名称信息")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.checked = set()

    def reset(self, candidates):
        self.beginResetModel()
        self.items = [dict(c) for c in candidates if c["kind"] == "file"]
        self.checked = set()
        self.endResetModel()
        self.selection_changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item, column = self.items[index.row()], index.column()
        if role == Qt.ItemDataRole.UserRole:
            return item["size"] if column == 3 else str(self.data(index) or "").casefold()
        if column == 0 and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if item["id"] in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            path = item.get("original_path")
            evidence = "原名待确认" if not path else ("回收站记录" if "recycle_metadata" in item.get("path_evidence", "") else "文件记录")
            if item.get("recovery_method") == "png_carving":
                evidence = "深度扫描·生成名称"
            display_name = filename(item)
            if item.get("recovery_method") == "ntfs_log":
                evidence = "旧日志·历史名称" if path else "旧日志·原目录未知"
                if item.get("content_status") == "fragment":
                    evidence = "旧日志·不完整片段"
                    display_name += "（片段）"
            values = ("", display_name, category(item), format_size(item["size"]),
                      str(PureWindowsPath(path).parent) if path else "原目录未知", evidence)
            return values[column]
        if role == Qt.ItemDataRole.ToolTipRole:
            if item.get("recovery_method") == "ntfs_log":
                return (item.get("original_path") or item["observed_path"]) + "\n" + "\n".join(item.get("warnings", []))
            if item.get("recovery_method") == "png_carving":
                return "PNG 内容扫描：显示名称由程序生成，原名与目录未知。\n块结构与 CRC 通过，仍需预览和保存后检查。"
            return (item.get("original_path") or item["observed_path"]) + "\n文件内容需通过预览或保存后检查。"
        if role == Qt.ItemDataRole.ForegroundRole and column == 5:
            return QColor("#9b681a" if not item.get("original_path") else "#18786c")
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsUserCheckable if index.column() == 0 else base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.isValid() and index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole:
            identifier = self.items[index.row()]["id"]
            if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
                self.checked.add(identifier)
            else:
                self.checked.discard(identifier)
            self.dataChanged.emit(index, index, [role])
            self.selection_changed.emit()
            return True
        return False


class CandidateFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.kind = "全部类型"
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def configure(self, query, kind):
        self.query, self.kind = query.casefold().strip(), kind
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        item = self.sourceModel().items[row]
        text = (item.get("original_path") or item["observed_path"]).casefold()
        return (not self.query or self.query in text) and (self.kind == "全部类型" or category(item) == self.kind)
