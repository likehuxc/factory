from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from d7_factory_studio.core.models import CanFrame, CanMode


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise OperationCancelled("操作已取消")


class OperationCancelled(RuntimeError):
    pass


class CanTransport(ABC):
    @abstractmethod
    def open(self, channel: int, mode: CanMode) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send(self, frame: CanFrame) -> None: ...

    @abstractmethod
    def receive(self, timeout_ms: int = 50) -> list[CanFrame]: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RemoteCommandRequest:
    argv: tuple[str, ...]
    timeout_s: float = 30.0
    stream_output: bool = False
    stdin_secret: str | None = None
    evidence_label: str = ""


@dataclass(frozen=True, slots=True)
class RemoteCommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class RemoteFileInfo:
    remote_path: str
    relative_path: str
    name: str
    size_bytes: int
    modified_epoch: int
    is_directory: bool = False
    is_symlink: bool = False


class RemoteSession(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def execute(
        self,
        request: RemoteCommandRequest,
        token: CancellationToken,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> RemoteCommandResult: ...

    @abstractmethod
    def list_files(self, remote_root: str) -> list[RemoteFileInfo]: ...

    @abstractmethod
    def download_files(
        self,
        entries: Iterable[RemoteFileInfo],
        destination: Path,
        token: CancellationToken,
    ) -> list[dict[str, object]]: ...
