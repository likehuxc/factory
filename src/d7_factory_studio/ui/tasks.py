from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from d7_factory_studio.core.ports import CancellationToken, OperationCancelled

ProgressCallback = Callable[[int, str], None]
TaskCallable = Callable[[CancellationToken, ProgressCallback], Any]


class TaskSignals(QObject):
    progress = Signal(str, int, str)
    succeeded = Signal(str, object)
    failed = Signal(str, str, str)
    cancelled = Signal(str)
    finished = Signal(str)


class TaskWorker(QRunnable):
    def __init__(self, task_id: str, operation: TaskCallable, token: CancellationToken) -> None:
        super().__init__()
        self.task_id = task_id
        self.operation = operation
        self.token = token
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation(self.token, self._report_progress)
            self.token.raise_if_cancelled()
        except OperationCancelled:
            self.signals.cancelled.emit(self.task_id)
        except Exception as exc:
            self.signals.failed.emit(self.task_id, str(exc), traceback.format_exc())
        else:
            self.signals.succeeded.emit(self.task_id, result)
        finally:
            self.signals.finished.emit(self.task_id)

    def _report_progress(self, progress: int, message: str = "") -> None:
        value = max(0, min(100, int(progress)))
        self.signals.progress.emit(self.task_id, value, message)


class TaskManager(QObject):
    progress = Signal(str, int, str)
    succeeded = Signal(str, object)
    failed = Signal(str, str, str)
    cancelled = Signal(str)
    finished = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self._tokens: dict[str, CancellationToken] = {}
        self._workers: dict[str, TaskWorker] = {}

    def start(self, task_id: str, operation: TaskCallable) -> None:
        if task_id in self._tokens:
            raise ValueError(f"任务已在运行: {task_id}")
        token = CancellationToken()
        worker = TaskWorker(task_id, operation, token)
        worker.signals.progress.connect(self.progress)
        worker.signals.succeeded.connect(self.succeeded)
        worker.signals.failed.connect(self.failed)
        worker.signals.cancelled.connect(self.cancelled)
        worker.signals.finished.connect(self._cleanup)
        worker.signals.finished.connect(self.finished)
        self._tokens[task_id] = token
        self._workers[task_id] = worker
        self.pool.start(worker)

    def cancel(self, task_id: str) -> bool:
        token = self._tokens.get(task_id)
        if token is None:
            return False
        token.cancel()
        return True

    def cancel_all(self, *, exclude: set[str] | None = None) -> None:
        excluded = exclude or set()
        for task_id, token in tuple(self._tokens.items()):
            if task_id in excluded:
                continue
            token.cancel()

    def is_running(self, task_id: str) -> bool:
        return task_id in self._tokens

    @Slot(str)
    def _cleanup(self, task_id: str) -> None:
        self._tokens.pop(task_id, None)
        self._workers.pop(task_id, None)
