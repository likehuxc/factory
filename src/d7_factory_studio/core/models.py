from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar


class ConnectionMode(StrEnum):
    PC_DIRECT = "pc_direct"
    ORIN_REMOTE = "orin_remote"


class LinkState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAULT = "fault"


class TaskState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CanMode(StrEnum):
    CLASSIC = "classic"
    FD = "fd"


@dataclass(frozen=True, slots=True)
class CanFrame:
    arbitration_id: int
    data: bytes = b""
    is_extended: bool = False
    is_fd: bool = True
    bitrate_switch: bool = True
    timestamp: float | None = None

    def __post_init__(self) -> None:
        maximum = 0x1FFFFFFF if self.is_extended else 0x7FF
        if not 0 <= self.arbitration_id <= maximum:
            raise ValueError(f"CAN ID 超出范围: 0x{self.arbitration_id:X}")
        max_length = 64 if self.is_fd else 8
        if len(self.data) > max_length:
            raise ValueError(f"数据长度 {len(self.data)} 超过 {max_length}")
        if not self.is_fd and self.bitrate_switch:
            object.__setattr__(self, "bitrate_switch", False)


@dataclass(frozen=True, slots=True)
class TaskProgress:
    task_id: str
    state: TaskState
    progress: int = 0
    message: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not 0 <= self.progress <= 100:
            raise ValueError("progress 必须在 0..100")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class OperationResult(Generic[T]):
    ok: bool
    value: T | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, value: T | None = None, **details: Any) -> OperationResult[T]:
        return cls(ok=True, value=value, details=details)

    @classmethod
    def failure(cls, error: str, **details: Any) -> OperationResult[T]:
        return cls(ok=False, error=error, details=details)

