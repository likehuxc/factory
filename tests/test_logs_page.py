from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.ui.pages.logs import LogsPage


class MemorySettings:
    @property
    def report_directory(self):
        return "."


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_task_log_copies_selected_rows_to_clipboard() -> None:
    app = _app()
    page = LogsPage(ApplicationState(), MemorySettings())
    page.add_activity("10:38:50", "CAN 帧", "info|RX ch=1 id=0x127")
    page.add_activity("10:38:51", "任务", "error|等待最新位置反馈超时")

    selection = page.activity_table.selectionModel()
    selection.select(
        page.activity_table.model().index(0, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.select(
        page.activity_table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    page._copy_selected_activity()

    assert page.copy_activity_button.isEnabled()
    assert QApplication.clipboard().text() == (
        "时间\t级别\t来源\t内容\n"
        "10:38:50\tINFO\tCAN 帧\tRX ch=1 id=0x127\n"
        "10:38:51\tERROR\t任务\t等待最新位置反馈超时"
    )
    page.close()
    app.processEvents()
