from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class DiagnosticProfile(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    LONG = "long"
    CUSTOM = "custom"


class DiagnosticVerdict(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DiagnosticStage:
    name: str
    description: str
    duration_s: int
    gap_ms: int
    can_id: int | None
    payload_length: int | None
    random_frames: bool = False


@dataclass(frozen=True, slots=True)
class LinkSnapshot:
    state: str = "UNKNOWN"
    counters: dict[str, int] = field(default_factory=dict)
    raw: str = ""


@dataclass(frozen=True, slots=True)
class StageEvidence:
    interface: str
    stage: DiagnosticStage
    generator_returncode: int
    generator_stdout: str
    generator_stderr: str
    before: LinkSnapshot
    after: LinkSnapshot
    dmesg_before: str
    dmesg_after: str
    dmesg_new: str
    candump: str
    error_lines: tuple[str, ...]
    timed_out: bool = False
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiagnosticEvaluation:
    verdict: DiagnosticVerdict
    findings: tuple[str, ...]
    root_cause_hints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "findings": list(self.findings),
            "root_cause_hints": list(self.root_cause_hints),
        }
