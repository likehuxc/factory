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


def test_failed_task_can_be_retried_from_failure_handler() -> None:
    app = create_application([])
    manager = TaskManager()
    loop = QEventLoop()
    results: list[object] = []

    def retry(task_id: str, _error: str, _traceback: str) -> None:
        assert not manager.is_running(task_id)
        manager.start(task_id, lambda _token, _report: "retried")

    manager.failed.connect(retry)
    manager.succeeded.connect(lambda _task, result: results.append(result))
    manager.finished.connect(lambda _task: loop.quit() if results else None)

    def fail(_token: CancellationToken, _report) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("open failed")

    manager.start("serial485.connection", fail)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    app.processEvents()

    assert results == ["retried"]
    assert not manager.is_running("serial485.connection")
