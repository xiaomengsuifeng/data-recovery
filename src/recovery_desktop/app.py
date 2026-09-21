from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from datetime import datetime
import uuid

sys.dont_write_bytecode = True

from PySide6.QtCore import Qt, QUrl, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QComboBox, QStackedWidget, QTableView,
    QHeaderView, QAbstractItemView, QSplitter, QPlainTextEdit, QProgressBar, QFrame,
    QMessageBox, QCheckBox, QScrollArea, QSpinBox)

from recovery_core import __version__
from recovery_core.common import RecoveryError, read_json
from recovery_core.partitions import inspect_image
from recovery_core.service import scan, resume_scan, recover, _validate_candidates
from recovery_core.acquisition import acquire, resume_acquisition
from recovery_core.preview import preview
from recovery_core.tsk import Tsk
from recovery_core.windows import VolumeSource, DiskSource, list_volumes, list_disks, elevate
from .model import CandidateModel, CandidateFilter, filename, format_size
from .worker import Worker


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'PingFang SC', sans-serif; font-size: 13px; color: #22323b; }
QMainWindow, QWidget#canvas { background: #f4f6f8; }
QFrame#sidebar { background: #142c35; border: none; }
QFrame#sidebar QLabel { color: #c8d7db; background: transparent; }
QFrame#sidebar QPushButton { text-align: left; color: #d9e4e7; background: transparent; border: none; padding: 13px 16px; }
QFrame#sidebar QPushButton:hover { background: #24444e; }
QFrame#sidebar QPushButton:checked { background: #24525a; color: white; }
QFrame#card { background: white; border: 1px solid #e2e8ec; border-radius: 12px; }
QLabel#hero { font-size: 28px; font-weight: 650; color: #162f38; }
QLabel#title { font-size: 20px; font-weight: 650; }
QLabel#muted { color: #6b7d87; }
QLabel#eyebrow { color: #148270; font-weight: 650; font-size: 12px; }
QLabel#notice { background: #edf5f3; color: #326258; padding: 14px; border-radius: 8px; }
QPushButton { background: white; border: 1px solid #ced9de; border-radius: 7px; padding: 9px 15px; font-weight: 550; }
QPushButton:hover { background: #edf5f3; border-color: #9bbab3; }
QPushButton:disabled { color: #a5afb4; background: #f0f3f4; border-color: #e2e8ea; }
QPushButton#primary { background: #168170; border: 1px solid #168170; color: white; }
QPushButton#primary:hover { background: #126e5f; }
QPushButton#primary:disabled { background: #a5c7c0; border-color: #a5c7c0; color: #f5f8f7; }
QLineEdit, QComboBox { background: white; border: 1px solid #cfdadf; border-radius: 6px; padding: 10px; selection-background-color: #168170; }
QLineEdit:focus, QComboBox:focus { border-color: #168170; }
QComboBox::drop-down { border: none; width: 24px; }
QTableView { background: white; border: 1px solid #e1e8eb; border-radius: 7px; gridline-color: #edf1f3; selection-background-color: #e1f1ec; selection-color: #183b34; }
QHeaderView::section { background: #f0f5f6; color: #627780; border: none; border-bottom: 1px solid #e0e8eb; padding: 11px 8px; font-size: 12px; font-weight: 600; }
QTableView::item { padding: 9px; border-bottom: 1px solid #f0f3f5; }
QPlainTextEdit { background: #f7f9fa; border: 1px solid #e2e9ec; border-radius: 7px; padding: 10px; }
QProgressBar { border: none; border-radius: 3px; background: #e0e9e8; height: 6px; text-align: center; }
QProgressBar::chunk { background: #168170; border-radius: 3px; }
QSplitter::handle { background: transparent; width: 14px; }
QCheckBox { spacing: 8px; }
"""


def label(text, kind=None, wrap=False):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    if kind:
        value.setObjectName(kind)
    value.setWordWrap(wrap)
    return value


def button(text, action, primary=False):
    value = QPushButton(text)
    if primary:
        value.setObjectName("primary")
    value.clicked.connect(action)
    return value


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(16)
    return frame, layout


def bundled_backend() -> Path | None:
    configured = os.environ.get("RECOVERY_TSK_BIN")
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parents[2]
    for folder in (root / "vendor/tsk/bin", root / "vendor/tsk"):
        if (folder / ("fls.exe" if os.name == "nt" else "fls")).is_file():
            return folder
    return None


class RecoveryWindow(QMainWindow):
    def __init__(self, tsk_bin=None):
        super().__init__()
        self.tsk_bin = tsk_bin or bundled_backend()
        self.worker = None
        self.session = None
        self.report = None
        self.last_output = None
        self.last_acquisition = None
        self.sources = []
        self._closing = False
        self.setWindowTitle("拾回 · 免费文件恢复")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self._compact = bool(available and (available.width() < 1200 or available.height() < 800))
        self._narrow = bool(available and available.width() < 1000)
        self.setMinimumSize(min(820, available.width() - 24) if available else 820,
                            min(480, available.height() - 48) if available else 480)
        self.resize(min(1240, available.width() - 24) if available else 1240,
                    min(830, available.height() - 48) if available else 830)
        self._build()

    def backend(self):
        return Tsk(self.tsk_bin, timeout=600)

    def _build(self):
        outer = QWidget()
        outer.setObjectName("canvas")
        layout = QHBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(168 if self._compact else 204)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(12 if self._compact else 22, 24, 12 if self._compact else 22, 20)
        nav.setSpacing(10)
        brand = label("拾回", "title")
        brand.setStyleSheet("font-size: 28px; color: white;")
        nav.addWidget(brand)
        if not self._narrow:
            nav.addWidget(label("让重要的文件回到身边"))
        nav.addSpacing(12 if self._narrow else 42)
        self.home_button = button("＋  开始恢复", self.go_home)
        self.open_button = button("↗  打开扫描记录", self.open_session)
        self.resume_button = button("↻  继续中断任务", self.open_progress)
        self.help_button = button("?   使用说明", self.show_help)
        for b in (self.home_button, self.open_button, self.resume_button, self.help_button):
            nav.addWidget(b)
        nav.addStretch()
        if not self._narrow:
            nav.addWidget(label("本地处理 · 无需账号"))
            nav.addWidget(label("核心恢复永久免费"))
            nav.addSpacing(14)
        nav.addWidget(label(f"候选版  {__version__}", wrap=True))
        layout.addWidget(sidebar)
        body = QVBoxLayout()
        body.setContentsMargins(16 if self._compact else 32, 20, 16 if self._compact else 32, 16)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        self.task_frame = QFrame()
        tl = QVBoxLayout(self.task_frame)
        tl.setContentsMargins(0, 14, 0, 0)
        top = QHBoxLayout()
        self.task_label = label("准备就绪", "muted", True)
        self.cancel_button = button("取消任务", self.cancel_task)
        self.cancel_button.setEnabled(False)
        top.addWidget(self.task_label, 1)
        top.addWidget(self.cancel_button)
        tl.addLayout(top)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        tl.addWidget(self.progress_bar)
        body.addWidget(self.task_frame)
        layout.addLayout(body, 1)
        # Page contents scroll independently; navigation and cancellation stay
        # visible even on a small logical desktop at 200% scaling.
        self.setCentralWidget(outer)
        self._home_page()
        self._results_page()
        self._complete_page()
        self._acquisition_page()

    def _home_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(18)
        v.addWidget(label("FILE RECOVERY  /  NTFS · exFAT", "eyebrow"))
        v.addWidget(label("找回误删的重要文件", "hero"))
        v.addWidget(label("清空回收站、直接删除或误删文件夹，从文件原来的位置开始查找。", "muted", True))
        v.addSpacing(6)
        frame, c = card()
        c.addWidget(label("1  选择文件原来所在的位置", "title"))
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["镜像文件", "Windows 卷", "整盘镜像采集"])
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.mode.setFixedWidth(180)
        row.addWidget(self.mode)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("选择 .img、.dd 或 .raw 镜像")
        self.source_edit.setReadOnly(True)
        row.addWidget(self.source_edit, 1)
        self.source_button = button("选择镜像", self.choose_source)
        row.addWidget(self.source_button)
        c.addLayout(row)
        self.partition = QComboBox()
        self.partition.addItem("选择来源后，自动识别 NTFS / exFAT 分区", None)
        c.addWidget(self.partition)
        self.deep_png = QCheckBox("深度查找 PNG 图片（仅镜像）")
        self.deep_png.setToolTip("较慢；原名与目录未知。额外检查未分配空间中的连续 PNG，最大 256 MiB，可能与普通结果重复。")
        c.addWidget(self.deep_png)
        self.deep_jpeg = QCheckBox("深度查找 JPEG 照片（仅镜像）")
        self.deep_jpeg.setToolTip("检查未分配空间中的 JPEG，最大 64 MiB / 4000 万像素；原名和目录未知。")
        c.addWidget(self.deep_jpeg)
        self.reassemble_jpeg = QCheckBox("尝试 JPEG 碎片重组（较慢，需逐张核对）")
        self.reassemble_jpeg.setToolTip("仅基线 JPEG，有界搜索最多 32 段或两段拼接，最大 8 MiB。存在歧义时跳过；不是所有碎片都能重组。")
        self.reassemble_jpeg.setEnabled(False)
        self.deep_jpeg.toggled.connect(lambda checked: self.reassemble_jpeg.setEnabled(checked and not self.worker))
        self.deep_jpeg.toggled.connect(lambda checked: self.reassemble_jpeg.setChecked(False) if not checked else None)
        c.addWidget(self.reassemble_jpeg)
        self.deep_log = QCheckBox("查找旧日志中的文件（实验功能，仅镜像）")
        self.deep_log.setToolTip("利用旧 NTFS 文件记录查找数据。仅支持部分日志格式；片段单独标记，历史名称和内容仍需核对。")
        c.addWidget(self.deep_log)
        c.addWidget(label("2  选择工作与保存位置", "title"))
        row = QHBoxLayout()
        self.workspace = QLineEdit()
        self.workspace.setPlaceholderText("选择另一块磁盘上的文件夹")
        self.workspace.setReadOnly(True)
        row.addWidget(self.workspace, 1)
        self.workspace_button = button("选择文件夹", self.choose_workspace)
        row.addWidget(self.workspace_button)
        c.addLayout(row)
        c.addWidget(label("扫描记录保存在此处。直接扫描磁盘时，程序、工作目录和恢复目标都需放在另一块物理磁盘。", "muted", True))
        self.live_notice = label("运行中的 Windows 会持续写入系统盘，部分删除内容可能已被覆盖或清理。恢复工具无法保证找回所有文件。", "notice", True)
        self.live_notice.hide()
        c.addWidget(self.live_notice)
        capture_options = QHBoxLayout()
        capture_options.addWidget(label("镜像采集：读取等待上限（秒）"))
        self.read_timeout = QSpinBox()
        self.read_timeout.setRange(1, 300)
        self.read_timeout.setValue(30)
        capture_options.addWidget(self.read_timeout)
        capture_options.addWidget(label("坏区重试次数"))
        self.read_retries = QSpinBox()
        self.read_retries.setRange(0, 10)
        self.read_retries.setValue(1)
        capture_options.addWidget(self.read_retries)
        capture_options.addStretch()
        c.addLayout(capture_options)
        actions = QHBoxLayout()
        self.admin_button = button("以管理员身份重新启动", self.restart_admin)
        self.admin_button.setVisible(os.name == "nt")
        actions.addWidget(self.admin_button)
        actions.addStretch()
        self.acquire_button = button("创建只读镜像", self.start_acquisition)
        actions.addWidget(self.acquire_button)
        self.scan_button = button("开始查找  →", self.start_scan, True)
        actions.addWidget(self.scan_button)
        c.addLayout(actions)
        v.addWidget(frame)
        tips, t = card()
        t.addWidget(label("恢复前，先减少对源磁盘的写入", "title"))
        t.addWidget(label("优先查找文档与照片。找到文件后可先预览，再选择需要保存的内容。\n请将恢复结果保存到其他磁盘，避免覆盖仍可恢复的数据。", "muted", True))
        v.addWidget(tips)
        v.addStretch()
        for control in (self.mode, self.source_edit, self.source_button, self.partition,
                        self.workspace, self.workspace_button, self.admin_button, self.scan_button):
            control.ensurePolished()
            control.setMinimumHeight(control.sizeHint().height())
        self.home_scroll = QScrollArea()
        self.home_scroll.setWidgetResizable(True)
        self.home_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.home_scroll.setWidget(page)
        self.pages.addWidget(self.home_scroll)

    def _results_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)
        v.addWidget(label("SCAN RESULTS", "eyebrow"))
        self.results_title = label("查找结果", "hero")
        v.addWidget(self.results_title)
        self.source_summary = label("", "muted", True)
        v.addWidget(self.source_summary)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名称或原路径…")
        self.search.textChanged.connect(self.filter_changed)
        toolbar.addWidget(self.search, 1)
        self.kind = QComboBox()
        self.kind.addItems(["全部类型", "文档", "图片", "影音", "其他"])
        self.kind.currentTextChanged.connect(self.filter_changed)
        toolbar.addWidget(self.kind)
        self.select_button = button("选择当前结果", self.select_visible)
        self.clear_button = button("清除选择", self.clear_selection)
        toolbar.addWidget(self.select_button)
        toolbar.addWidget(self.clear_button)
        v.addLayout(toolbar)
        split = QSplitter()
        self.model = CandidateModel()
        self.proxy = CandidateFilter()
        self.proxy.setSourceModel(self.model)
        self.model.selection_changed.connect(self.update_selection)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(43)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, size in enumerate((45, 205, 60, 85, 170, 115)):
            self.table.setColumnWidth(i, size)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().currentRowChanged.connect(self.current_changed)
        self.table.doubleClicked.connect(self.request_preview)
        split.addWidget(self.table)
        preview_frame, p = card()
        preview_frame.setMinimumWidth(265)
        self.preview_name = label("文件预览", "title", True)
        p.addWidget(self.preview_name)
        self.preview_info = label("选中一个文件，再点击预览。", "muted", True)
        p.addWidget(self.preview_info)
        self.preview_image = label("")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumHeight(140)
        self.preview_image.hide()
        p.addWidget(self.preview_image)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText("预览在本地生成，文件不会上传。")
        p.addWidget(self.preview_text, 1)
        self.preview_note = label("", "muted", True)
        p.addWidget(self.preview_note)
        self.preview_button = button("预览选中文件", self.request_preview)
        p.addWidget(self.preview_button)
        # A small window must scroll the preview instead of squeezing the
        # image and its evidence note into overlapping minimum geometries.
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        preview_scroll.setMinimumWidth(265)
        preview_scroll.setWidget(preview_frame)
        split.addWidget(preview_scroll)
        split.setSizes([650, 290])
        v.addWidget(split, 1)
        self.empty_label = label("没有找到符合条件的文件。尝试更换关键词或类型。", "notice", True)
        self.empty_label.hide()
        v.addWidget(self.empty_label)
        foot = QHBoxLayout()
        self.selected_label = label("尚未选择文件", "muted")
        self.details_button = button("查看扫描说明", self.scan_details)
        if self._narrow:
            v.addWidget(self.selected_label)
        else:
            foot.addWidget(self.selected_label, 1)
        foot.addWidget(self.details_button)
        self.recover_button = button("保存选中的文件  →", self.start_recover, True)
        self.recover_button.setEnabled(False)
        foot.addWidget(self.recover_button)
        v.addLayout(foot)
        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setWidget(page)
        self.pages.addWidget(self.results_scroll)

    def _complete_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(20)
        v.addWidget(label("RECOVERY REPORT", "eyebrow"))
        self.complete_title = label("保存结果", "hero")
        v.addWidget(self.complete_title)
        frame, c = card()
        self.complete_counts = label("", "title", True)
        c.addWidget(self.complete_counts)
        self.complete_info = label("", "muted", True)
        c.addWidget(self.complete_info)
        self.complete_text = QPlainTextEdit()
        self.complete_text.setReadOnly(True)
        c.addWidget(self.complete_text, 1)
        c.addWidget(label("文件已保存不代表内容一定完整。请检查重要文件；逐文件状态和校验摘要保存在恢复报告中。", "notice", True))
        row = QHBoxLayout()
        self.back_results = button("返回查找结果", lambda: self.pages.setCurrentIndex(1))
        row.addWidget(self.back_results)
        row.addStretch()
        row.addWidget(button("打开保存位置", self.open_output, True))
        c.addLayout(row)
        v.addWidget(frame, 1)
        self.complete_scroll = QScrollArea()
        self.complete_scroll.setWidgetResizable(True)
        self.complete_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.complete_scroll.setWidget(page)
        self.pages.addWidget(self.complete_scroll)

    def _acquisition_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.acquisition_title = label("镜像采集结果", "hero")
        layout.addWidget(self.acquisition_title)
        self.acquisition_info = label("", "notice", True)
        layout.addWidget(self.acquisition_info)
        self.acquisition_text = QPlainTextEdit()
        self.acquisition_text.setReadOnly(True)
        layout.addWidget(self.acquisition_text, 1)
        row = QHBoxLayout()
        self.retry_acquisition_button = button("继续采集 / 重试坏区", self.retry_acquisition)
        self.load_acquired_button = button("查找此镜像中的文件", self.load_acquired, True)
        row.addWidget(self.retry_acquisition_button)
        row.addWidget(self.load_acquired_button)
        layout.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self.pages.addWidget(scroll)

    def run_task(self, message, operation, success):
        if self.worker is not None:
            return
        self.task_label.setText(message)
        self.progress_bar.setRange(0, 0)
        self.worker = Worker(operation, self)
        self.worker.changed.connect(self.on_progress)
        def delivered(result):
            try:
                success(result)
            except Exception as exc:
                self.show_error(str(exc))
        self.worker.result_ready.connect(delivered)
        self.worker.failed.connect(self.show_error)
        self.worker.cancelled.connect(lambda: self.task_label.setText("任务已取消，已有记录和结果保留。"))
        self.worker.finished.connect(self.task_finished)
        self.set_busy(True)
        self.worker.start()

    def set_busy(self, busy):
        for w in (self.home_button, self.open_button, self.resume_button, self.source_button, self.workspace_button,
                  self.scan_button, self.mode, self.partition, self.preview_button, self.recover_button,
                  self.admin_button, self.back_results, self.acquire_button, self.read_timeout, self.read_retries,
                  self.retry_acquisition_button, self.load_acquired_button):
            w.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.deep_png.setEnabled(not busy and self.mode.currentIndex() == 0)
        self.deep_log.setEnabled(not busy and self.mode.currentIndex() == 0)
        self.deep_jpeg.setEnabled(not busy and self.mode.currentIndex() == 0)
        self.reassemble_jpeg.setEnabled(not busy and self.mode.currentIndex() == 0 and self.deep_jpeg.isChecked())
        self.scan_button.setEnabled(not busy and self.mode.currentIndex() != 2)
        if not busy:
            self.update_selection()

    def task_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if self._closing:
            self.close()

    def on_progress(self, event):
        names = {"hash": "正在校验文件数据", "source": "正在核对来源", "scan": "正在查找删除记录",
                 "carving": "正在深度查找 PNG", "jpeg": "正在深度查找 JPEG",
                 "acquisition": "正在只读采集镜像", "acquisition_verify": "正在核对采集进度",
                 "directories": "正在查找已删除目录", "recycle": "正在关联回收站记录", "export": "正在保存文件"}
        text = names.get(event["phase"], "正在处理")
        if event.get("message"):
            text += " · " + event["message"][-65:]
        self.task_label.setText(text)
        if event["total"]:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(1000 * event["completed"] / event["total"])))
        else:
            self.progress_bar.setRange(0, 0)

    def cancel_task(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.task_label.setText("正在取消并保存任务状态…")

    def show_error(self, message):
        self.task_label.setText("未完成 · 请查看提示")
        QMessageBox.warning(self, "未能完成操作", str(message))

    def go_home(self):
        if not self.worker:
            self.pages.setCurrentIndex(0)

    def mode_changed(self):
        self.source_edit.clear()
        self.partition.clear()
        self.partition.addItem("请选择来源", None)
        live = self.mode.currentIndex() != 0
        if live:
            self.deep_png.setChecked(False)
            self.deep_log.setChecked(False)
            self.deep_jpeg.setChecked(False)
        self.deep_png.setEnabled(not live)
        self.deep_log.setEnabled(not live)
        self.deep_jpeg.setEnabled(not live)
        self.scan_button.setEnabled(self.mode.currentIndex() != 2)
        self.live_notice.setVisible(live)
        self.source_button.setText("刷新磁盘" if live else "选择镜像")
        self.source_edit.setPlaceholderText("选择下方的源设备" if live else "选择 .img、.dd 或 .raw 镜像")
        if live:
            self.choose_source()

    def choose_source(self):
        if self.mode.currentIndex() != 0:
            if os.name != "nt":
                self.show_error("当前为非 Windows 系统，可使用镜像恢复。直接磁盘扫描在 Windows 版提供。")
                return
            def loaded(rows):
                self.partition.clear()
                for row in rows:
                    identifier = row.get("mount") or f"磁盘 {row['number']}"
                    self.partition.addItem(f"{identifier}  {row['label'] or '本地磁盘'}  ·  {format_size(row['size'])}" +
                                           (" · 系统盘" if row.get("system") else ""), row)
                self.task_label.setText(f"找到 {len(rows)} 个来源")
                if not rows:
                    self.partition.addItem("没有可用来源", None)
            self.run_task("正在识别磁盘…", list_disks if self.mode.currentIndex() == 2 else list_volumes, loaded)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择 raw 镜像", "", "Raw 镜像 (*.img *.raw *.dd)")
            if path:
                self.load_image(Path(path))

    def load_image(self, path):
        self.source_edit.setText(str(path))
        self.partition.clear()
        def loaded(rows):
            for row in rows:
                self.partition.addItem(f"{row['label']} · {format_size(row['size'])}", row)
            self.task_label.setText(f"已识别 {len(rows)} 个 NTFS / exFAT 分区")
        self.run_task("正在识别镜像分区…", lambda: inspect_image(path), loaded)

    def choose_workspace(self):
        path = QFileDialog.getExistingDirectory(self, "选择工作文件夹")
        if path:
            self.workspace.setText(path)

    def new_task_folder(self, parent, prefix):
        return Path(parent) / (prefix + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])

    def start_scan(self):
        choice = self.partition.currentData()
        work = self.workspace.text()
        if not choice or not work or self.mode.currentIndex() == 2:
            self.show_error("请选择 NTFS / exFAT 来源和工作文件夹。整盘来源请先创建镜像。")
            return
        source = VolumeSource(choice["mount"]) if self.mode.currentIndex() == 1 else Path(self.source_edit.text())
        destination = self.new_task_folder(work, "scan")
        offset = choice.get("offset", 0)
        sector = choice.get("sector_size", 512)
        deep_png = self.deep_png.isChecked()
        deep_log = self.deep_log.isChecked()
        deep_jpeg, reassemble = self.deep_jpeg.isChecked(), self.reassemble_jpeg.isChecked()
        if deep_log and choice.get("filesystem", "").lower() == "exfat":
            self.show_error("exFAT 没有 NTFS 旧日志，请关闭旧日志选项。")
            return
        def operation():
            return scan(source, destination, backend=self.backend(), offset=offset, sector_size=sector,
                        max_candidates=100000, deep_png=deep_png, deep_log=deep_log,
                        deep_jpeg=deep_jpeg, reassemble_jpeg=reassemble)
        def loaded(report):
            self.load_report(destination, report)
            self.task_label.setText("查找完成，请选择需要保存的文件。")
        self.run_task("正在开始查找…", operation, loaded)

    def load_report(self, session, report):
        _validate_candidates(report.get("candidates"))
        if report.get("status") != "scanned":
            raise RecoveryError("扫描记录尚未完成。")
        self.session, self.report = Path(session), report
        self.search.clear()
        self.kind.setCurrentIndex(0)
        self.model.reset(report["candidates"])
        self.source_summary.setText(f"来源：{report['source'].get('mount') or report['source']['path']}\n找到的是候选文件。预览和保存后可进一步检查内容。")
        self.pages.setCurrentIndex(1)
        self.filter_changed()

    def open_session(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开扫描记录", self.workspace.text(), "扫描记录 (session.json)")
        if path:
            def operation():
                report = read_json(Path(path))
                _validate_candidates(report.get("candidates"))
                return report
            self.run_task("正在读取扫描记录…", operation, lambda report: self.load_report(Path(path).parent, report))

    def open_progress(self):
        path, _ = QFileDialog.getOpenFileName(self, "继续中断任务", self.workspace.text(),
                                             "任务进度 (scan-progress.json acquisition.json)")
        if path:
            self.resume_progress(Path(path))

    def resume_progress(self, path):
        directory = path.parent
        if path.name == "acquisition.json":
            self.run_task("正在核对采集数据…", lambda: resume_acquisition(directory), self.show_acquisition)
        elif path.name == "scan-progress.json":
            self.run_task("正在核对镜像并继续查找…", lambda: resume_scan(directory, backend=self.backend()),
                          lambda report: self.load_report(directory, report))
        else:
            self.show_error("请选择 scan-progress.json 或 acquisition.json。")

    def start_acquisition(self):
        work, choice = self.workspace.text(), self.partition.currentData()
        if not work or (self.mode.currentIndex() == 0 and not self.source_edit.text()) or (self.mode.currentIndex() != 0 and not choice):
            self.show_error("请先选择采集来源和另一块磁盘上的工作文件夹。")
            return
        source = (Path(self.source_edit.text()) if self.mode.currentIndex() == 0 else
                  VolumeSource(choice["mount"]) if self.mode.currentIndex() == 1 else DiskSource(choice["number"]))
        destination = self.new_task_folder(work, "image")
        timeout, retries = self.read_timeout.value(), self.read_retries.value()
        self.run_task("正在准备只读采集…", lambda: acquire(source, destination, timeout=timeout, retries=retries), self.show_acquisition)

    def show_acquisition(self, report):
        self.last_acquisition = report
        completed = report["status"] == "completed"
        self.acquisition_title.setText("镜像采集完成" if completed else "镜像采集尚有未读数据")
        self.acquisition_info.setText(
            f"已读取 {format_size(report.get('good_bytes', 0))} · 坏区 {format_size(report.get('bad_bytes', 0))} · "
            f"待读取 {format_size(report.get('pending_bytes', 0))}\n"
            "未读区域以零占位；它们不是恢复出的数据。可继续采集或重试坏区。")
        lines = [f"镜像：{report['image']}", f"状态：{report['status']}", report.get("error", ""),
                 f"SHA-256：{report.get('image_sha256', '尚未完成')}", "", "尚未读出的区域（位置 / 字节数 / 状态）："]
        missing = [r for r in report["ranges"] if r["status"] != "good"]
        lines.extend(f"{r['offset']:,} / {r['size']:,} / {r['status']}" for r in missing[:500])
        if len(missing) > 500:
            lines.append("其余区域见同目录 acquisition.json。")
        self.acquisition_text.setPlainText("\n".join(lines))
        self.pages.setCurrentIndex(3)
        self.task_label.setText("采集记录已保存，可从“继续中断任务”重新打开。")

    def retry_acquisition(self):
        if self.last_acquisition:
            self.resume_progress(Path(self.last_acquisition["image"]).parent / "acquisition.json")

    def load_acquired(self):
        if self.last_acquisition:
            self.mode.setCurrentIndex(0)
            self.go_home()
            self.load_image(Path(self.last_acquisition["image"]))

    def filter_changed(self, *_):
        if not hasattr(self, "proxy"):
            return
        self.proxy.configure(self.search.text(), self.kind.currentText())
        self.results_title.setText(f"找到 {len(self.model.items):,} 个候选文件")
        self.empty_label.setVisible(self.proxy.rowCount() == 0)
        self.update_selection()

    def update_selection(self):
        if not hasattr(self, "model"):
            return
        count = len(self.model.checked)
        size = sum(c["size"] for c in self.model.items if c["id"] in self.model.checked)
        self.selected_label.setText(f"显示 {self.proxy.rowCount():,} 个 · 已选择 {count:,} 个 · {format_size(size)}")
        self.recover_button.setEnabled(bool(count) and not self.worker)

    def select_visible(self):
        self.model.blockSignals(True)
        for row in range(self.proxy.rowCount()):
            index = self.proxy.mapToSource(self.proxy.index(row, 0))
            self.model.setData(index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        self.model.blockSignals(False)
        if self.model.items:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(len(self.model.items) - 1, 0))
        self.update_selection()

    def clear_selection(self):
        self.model.checked.clear()
        if self.model.items:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(len(self.model.items) - 1, 0))
        self.update_selection()

    def current_item(self):
        index = self.table.currentIndex()
        if index.isValid():
            return self.model.items[self.proxy.mapToSource(index).row()]
        return None

    def current_changed(self, *_):
        item = self.current_item()
        self.preview_image.hide()
        self.preview_text.show()
        self.preview_text.clear()
        self.preview_note.clear()
        if item:
            self.preview_name.setText(filename(item))
            self.preview_info.setText(format_size(item["size"]) + "\n" + (item.get("original_path") or "原名称与目录暂未确认"))

    def request_preview(self, *_):
        item = self.current_item()
        if not item or self.worker:
            return
        self.run_task("正在生成本地预览…", lambda: preview(self.session, item["id"], self.backend()), self.display_preview)

    def display_preview(self, result):
        if not self.current_item() or result["candidate_id"] != self.current_item()["id"]:
            return
        self.preview_note.setText(result["note"])
        if result["kind"] == "image":
            buffer = QBuffer()
            buffer.setData(QByteArray(result["data"]))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            reader.setAllocationLimit(64)
            size = reader.size()
            if not size.isValid() or size.width() * size.height() > 40_000_000:
                self.preview_text.setPlainText("图像尺寸超出预览上限，或图像头部已损坏。")
                return
            reader.setAutoTransform(True)
            reader.setScaledSize(size.scaled(680, 480, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if image.isNull():
                self.preview_text.setPlainText("图像未能解码。可以尝试保存后检查。")
                return
            self.preview_text.hide()
            self.preview_image.show()
            self.preview_image.setPixmap(QPixmap.fromImage(image).scaled(max(200, self.preview_image.width()), 340,
                                                                         Qt.AspectRatioMode.KeepAspectRatio,
                                                                         Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap if result.get("format") == "hex"
                                             else QPlainTextEdit.LineWrapMode.WidgetWidth)
            self.preview_text.setPlainText(result["text"])
        self.task_label.setText("预览完成")

    def start_recover(self):
        ids = sorted(self.model.checked)
        if not ids:
            return
        parent = QFileDialog.getExistingDirectory(self, "选择恢复结果的保存位置", self.workspace.text())
        if parent:
            self.export_to(Path(parent), ids)

    def export_to(self, parent, ids):
        destination = self.new_task_folder(parent, "recovered")
        def operation():
            return recover(self.session, destination, backend=self.backend(), candidate_ids=ids, max_file_bytes=(1 << 63) - 1)
        def done(report):
            self.last_output = destination
            self.show_recovery(report)
        self.run_task("正在保存选中文件…", operation, done)

    def show_recovery(self, report):
        status = report["status"]
        incomplete = report['partial_count'] + report['failed_count'] + report['skipped_count']
        pending = max(0, report.get('selected_count', 0) - report.get('processed_count', 0))
        title = ("保存已取消" if status == "cancelled" else
                 "来源变化或无法读取" if status == "source_changed" else
                 "保存未全部完成" if incomplete or pending else "保存完成")
        self.complete_title.setText(title)
        self.complete_counts.setText(f"已导出 {report['exported_unverified_count']} 个   ·   部分 {report['partial_count']} 个   ·   未完成 {report['failed_count'] + report['skipped_count']} 个")
        self.complete_info.setText(str(self.last_output))
        states = {"exported_unverified": "已导出 · 待检查内容", "partial": "部分结果", "failed": "未完成", "skipped": "已跳过"}
        lines = []
        if report.get("source_error"):
            lines.append("来源检查：" + report["source_error"])
        if report.get("report_warning"):
            lines.append(report["report_warning"] + "\n报告：" + report["report_path"])
        if pending:
            lines.append(f"还有 {pending} 个选中文件未处理。已有结果保留，可重新选择这些文件保存到新目录。")
        for item in report["results"][:500]:
            lines.append(f"{states[item['status']]}  |  {item['original_path'] or item['observed_path']}\n    {item.get('saved_path') or '未保存'}")
            if item['status'] != 'exported_unverified' and item.get('warnings'):
                lines.append("    原因：" + "；".join(str(w) for w in item['warnings'])[:4000])
        if len(report["results"]) > 500:
            lines.append("界面显示前 500 项，完整结果见 recovery.json。")
        self.complete_text.setPlainText("\n\n".join(lines))
        self.pages.setCurrentIndex(2)
        self.task_label.setText("恢复报告已保存在扫描记录目录" if report.get("report_path") else "恢复报告已保存在结果文件夹")

    def open_output(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output.resolve())))

    def scan_details(self):
        if self.report:
            box = QMessageBox(self)
            box.setWindowTitle("扫描说明")
            box.setText("扫描记录保留了文件来源、名称证据和未能处理的条目。缺失的元数据或已覆盖内容可能影响结果。")
            box.setDetailedText("\n".join(self.report.get("limitations", []) + self.report.get("warnings", [])))
            box.exec()

    def show_help(self):
        QMessageBox.information(self, "使用拾回", "1. 选择 NTFS / exFAT 卷或 raw 镜像。\n2. 选择其他磁盘上的工作文件夹。\n3. 可先创建只读镜像，再查找、预览和保存文件。\n\n镜像支持 PNG / JPEG 深度查找和有界 JPEG 碎片重组。碎片结果可能拼接错误，需逐张核对。\n采集坏区会明确记录并以零占位，可重试读取。\n中断的镜像扫描与采集可从“继续中断任务”打开进度文件。\n直接读取设备需要 Windows 管理员权限。\n\n不支持 BitLocker/EFS 解密、硬件维修、恢复已覆盖数据或逆转 SSD TRIM。软件不会修复、格式化或写入源设备。\n\n开源许可见 LICENSE 与 THIRD_PARTY_NOTICES.md。")

    def restart_admin(self):
        try:
            if sys.argv[0].endswith("launch.pyw"):
                args = ["-B", str(Path(sys.argv[0]).resolve())]
            else:
                args = ["-B", "-m", "recovery_desktop"]
            if self.tsk_bin:
                args += ["--tsk-bin", str(Path(self.tsk_bin).resolve())]
            if self.session:
                args += ["--session", str(self.session.resolve())]
            if self.workspace.text():
                args += ["--workspace", str(Path(self.workspace.text()).resolve())]
            elevate(args)
            self.close()
        except Exception as exc:
            self.show_error(str(exc))

    def closeEvent(self, event):
        if self.worker:
            self._closing = True
            self.cancel_task()
            event.ignore()
        else:
            event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="拾回：免费 NTFS / exFAT 文件恢复")
    parser.add_argument("--tsk-bin", type=Path)
    parser.add_argument("--session", type=Path)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("拾回")
    app.setOrganizationName("DataRecovery")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = RecoveryWindow(args.tsk_bin)
    if args.workspace:
        window.workspace.setText(str(args.workspace))
    if args.session:
        try:
            window.load_report(args.session, read_json(args.session / "session.json"))
        except Exception as exc:
            window.show_error(str(exc))
    window.show()
    return app.exec()
