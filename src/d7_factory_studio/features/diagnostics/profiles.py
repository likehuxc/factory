from __future__ import annotations

from d7_factory_studio.core.evt import EvtConfig
from d7_factory_studio.core.models import CanMode

from .models import DiagnosticProfile, DiagnosticStage

FD_LENGTHS = (*range(9), 12, 16, 20, 24, 32, 48, 64)
CLASSIC_LENGTHS = tuple(range(9))


def reserved_low_bytes(evt: EvtConfig) -> frozenset[int]:
    values = {node.dev_id & 0xFF for node in evt.nodes}
    values.update(can_id & 0xFF for can_id in evt.broadcast.reserved_ids)
    values.add(evt.broadcast.request_id & 0xFF)
    return frozenset(values)


def is_safe_standard_id(evt: EvtConfig, can_id: int) -> bool:
    return 0 <= can_id <= 0x7FF and (can_id & 0xFF) not in reserved_low_bytes(evt)


def safe_random_ids(evt: EvtConfig) -> tuple[int, ...]:
    return tuple(can_id for can_id in range(0x800) if is_safe_standard_id(evt, can_id))


def validate_fixed_id(evt: EvtConfig, can_id: int) -> int:
    if not is_safe_standard_id(evt, can_id):
        raise ValueError(f"CAN ID 0x{can_id:03X} 与 EVT 节点或保留地址冲突")
    return can_id


def build_profile(
    evt: EvtConfig,
    interface: str,
    profile: DiagnosticProfile,
    *,
    fixed_can_id: int,
    duration_s: int = 60,
    gap_ms: int = 1,
    payload_length: int | None = None,
    random_frames: bool = False,
) -> tuple[DiagnosticStage, ...]:
    try:
        interface_config = evt.interfaces[interface]
    except KeyError as exc:
        raise ValueError(f"EVT 配置中不存在接口 {interface}") from exc
    fixed_can_id = validate_fixed_id(evt, fixed_can_id)
    is_fd = interface_config.mode is CanMode.FD
    maximum_length = 64 if is_fd else 8
    fixed_length = maximum_length if payload_length is None else payload_length
    if not 0 <= fixed_length <= maximum_length:
        raise ValueError(f"{interface} 的数据长度必须在 0..{maximum_length}")

    if profile is DiagnosticProfile.CUSTOM:
        if duration_s < 1 or gap_ms < 0:
            raise ValueError("自定义时长必须大于 0，帧间隔不能为负数")
        return (
            DiagnosticStage(
                "custom",
                "custom CAN FD+BRS traffic" if is_fd else "custom Classic CAN traffic",
                duration_s,
                gap_ms,
                None if random_frames else fixed_can_id,
                None if random_frames else fixed_length,
                random_frames,
            ),
        )

    durations = {
        DiagnosticProfile.QUICK: (30, 30, 30),
        DiagnosticProfile.STANDARD: (240, 240, 120),
        DiagnosticProfile.LONG: (600, 900, 300),
    }[profile]
    prefix = "fixed_64b" if is_fd else "fixed_8b"
    random_name = "random_length_fd_brs" if is_fd else "random_classic"
    kind = "CAN FD+BRS" if is_fd else "Classic CAN"
    return (
        DiagnosticStage(
            f"{prefix}_1ms",
            f"{kind} fixed ID, {maximum_length} bytes, 1 ms gap",
            durations[0],
            1,
            fixed_can_id,
            maximum_length,
        ),
        DiagnosticStage(
            f"{prefix}_0gap",
            f"{kind} fixed ID, {maximum_length} bytes, 0 gap",
            durations[1],
            0,
            fixed_can_id,
            maximum_length,
        ),
        DiagnosticStage(
            random_name, f"{kind} safe random ID/length/data, 0 gap", durations[2], 0, None, None, True
        ),
    )
