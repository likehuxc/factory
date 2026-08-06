from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.controls import D7DateTimeEdit as QDateTimeEdit
from d7_factory_studio.ui.controls import D7TableWidget as QTableWidget
from d7_factory_studio.ui.pages.base import InlineMessage, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader


class LogsPage(WorkbenchPage):
    def __init__(self, state: ApplicationState, settings: SettingsStore) -> None:
        super().__init__()
        self.state = state
        self.settings = settings
        self.layout.addWidget(
            PageHeader("日志与报告", "查看当前任务记录，或从 Orin 安全下载 /sdcard/pudu/log。")
        )
        tabs = QTabWidget()
        tabs.addTab(self._activity_tab(), "任务日志")
        tabs.addTab(self._device_tab(), "设备日志")
        tabs.addTab(self._reports_tab(), "诊断报告")
        self.layout.addWidget(tabs)
        state.activity_added.connect(self.add_activity)
        state.task_event.connect(self._on_task_event)

    def _activity_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        self.activity_table = QTableWidget(0, 4)
        self.activity_table.setHorizontalHeaderLabels(["时间", "级别", "来源", "内容"])
        self.activity_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.activity_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.activity_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.activity_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.activity_table.verticalHeader().setVisible(False)
        layout.addWidget(self.activity_table)
        return tab

    def _device_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(
            InlineMessage("设备日志功能只读取和下载，不提供删除、清理或移动远端文件的入口。", "success")
        )
        toolbar = QHBoxLayout()
        self.start_time = QDateTimeEdit(QDateTime.currentDateTime().addDays(-1))
        self.start_time.setCalendarPopup(True)
        self.end_time = QDateTimeEdit(QDateTime.currentDateTime())
        self.end_time.setCalendarPopup(True)
        refresh = QPushButton("查询日志")
        refresh.setProperty("primary", True)
        refresh.clicked.connect(self._refresh_logs)
        toolbar.addWidget(QLabel("开始"))
        toolbar.addWidget(self.start_time)
        toolbar.addWidget(QLabel("结束"))
        toolbar.addWidget(self.end_time)
        toolbar.addStretch(1)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)
        self.device_table = QTableWidget(0, 5)
        self.device_table.setHorizontalHeaderLabels(
            ["文件", "相对路径", "大小", "修改时间", "SHA-256 / 状态"]
        )
        self.device_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.device_table)
        actions = QHBoxLayout()
        choose = QPushButton("选择下载目录")
        choose.clicked.connect(self._choose_download_dir)
        self.download_dir = QLabel(str(Path.home() / "Downloads" / "D7-logs"))
        self.download_dir.setObjectName("Mono")
        download = QPushButton("下载所选并校验")
        download.setProperty("primary", True)
        download.clicked.connect(self._download_logs)
        actions.addWidget(choose)
        actions.addWidget(self.download_dir, 1)
        actions.addWidget(download)
        layout.addLayout(actions)
        return tab

    def _reports_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("报告目录", "每次运行保存自包含 HTML、JSON、manifest 和 raw 原始证据。")
        self.report_path = QLabel(str(self.settings.report_directory))
        self.report_path.setObjectName("Mono")
        open_folder = QPushButton("打开报告目录")
        open_folder.clicked.connect(self._open_reports)
        card.body.addWidget(self.report_path)
        card.body.addWidget(open_folder)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def add_activity(self, timestamp: str, source: str, payload: str) -> None:
        level, _, message = payload.partition("|")
        row = self.activity_table.rowCount()
        self.activity_table.insertRow(row)
        for column, value in enumerate((timestamp, level.upper(), source, message)):
            self.activity_table.setItem(row, column, QTableWidgetItem(value))
        self.activity_table.scrollToBottom()

    def _refresh_logs(self) -> None:
        if (
            self.state.connection_mode.value != "orin_remote"
            or self.state.link_state is not LinkState.CONNECTED
        ):
            QMessageBox.warning(self, "Orin 未连接", "切换到 Orin 远程并连接后才能查询设备日志。")
            return
        self.state.request(
            "device_logs.list",
            root="/sdcard/pudu/log",
            start_epoch=self.start_time.dateTime().toSecsSinceEpoch(),
            end_epoch=self.end_time.dateTime().toSecsSinceEpoch(),
        )

    def _choose_download_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择设备日志下载目录", self.download_dir.text())
        if path:
            self.download_dir.setText(path)

    def _download_logs(self) -> None:
        rows = sorted({index.row() for index in self.device_table.selectedIndexes()})
        if not rows:
            QMessageBox.warning(self, "未选择文件", "请先选择一个或多个设备日志文件。")
            return
        paths = [
            self.device_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            or self.device_table.item(row, 1).text()
            for row in rows
        ]
        self.state.request("device_logs.download", remote_paths=paths, destination=self.download_dir.text())

    def _open_reports(self) -> None:
        path = self.settings.report_directory
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(path.as_uri())

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "device_logs.list" and event == "succeeded" and isinstance(payload, dict):
            rows = payload.get("rows", [])
            self.device_table.setRowCount(0)
            for row_data in rows:
                if not isinstance(row_data, dict):
                    continue
                row = self.device_table.rowCount()
                self.device_table.insertRow(row)
                values = (
                    row_data.get("name", ""),
                    row_data.get("relative_path", ""),
                    f"{int(row_data.get('size_bytes', 0)):,}",
                    row_data.get("modified_iso", ""),
                    "待下载",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if column == 1:
                        item.setData(Qt.ItemDataRole.UserRole, row_data.get("remote_path", ""))
                    self.device_table.setItem(row, column, item)
        elif action == "device_logs.download" and event == "succeeded" and isinstance(payload, dict):
            QMessageBox.information(
                self,
                "下载完成",
                f"成功 {payload.get('downloaded_count', 0)}，失败 {payload.get('failed_count', 0)}。\n"
                f"Manifest: {payload.get('manifest_path', '')}",
            )
        elif action.startswith("device_logs.") and event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            QMessageBox.critical(self, "设备日志任务失败", str(error))
