from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer

from d7_factory_studio.app import create_application
from d7_factory_studio.core.ports import CancellationToken
from d7_factory_studio.ui.tasks import TaskManager


def test_task_manager_reports_progress_and_result() -> None:
    app = create_application([])
    manager = TaskManager()
    loop = QEventLoop()
    progress: list[tuple[int, str]] = []
    results: list[object] = []
    manager.progress.connect(lambda _task, value, message: progress.append((value, message)))
    manager.succeeded.connect(lambda _task, result: results.append(result))
    manager.finished.connect(lambda _task: loop.quit())

    manager.start("demo", lambda _token, report: (report(50, "half"), 42)[1])
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    app.processEvents()
    assert progress == [(50, "half")]
    assert results == [42]
    assert not manager.is_running("demo")


def test_task_manager_cancel_marks_token() -> None:
    manager = TaskManager()
    manager._tokens["task"] = token = CancellationToken()
    assert manager.cancel("task") is True
    assert token.is_cancelled is True
    assert manager.cancel("missing") is False
