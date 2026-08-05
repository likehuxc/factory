from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IapDeviceProfile:
    name: str
    target_id: int
    can_id: int = 0x7FF
    pre_upgrade_wakeup_ms: int = 0
    disable_target_can_messages: bool = False
    ignore_validate_ack_failure: bool = False
    app_start_wait_ms: int = 1_000
    app_total_wait_ms: int = 5_000

    def __post_init__(self) -> None:
        if not 0 <= self.target_id <= 0xFF:
            raise ValueError("IAP protocol target ID must fit in one byte")
        if not 0 <= self.can_id <= 0x7FF:
            raise ValueError("IAP CAN ID must be a standard 11-bit ID")


PMU_PROFILE = IapDeviceProfile("PMU", 0x18)
BATTERY_QUICK_IDS = (0x41, 0x42, 0x43)
BATTERY_PROFILE = IapDeviceProfile(
    "电池",
    0x42,
    pre_upgrade_wakeup_ms=1_000,
    disable_target_can_messages=True,
    ignore_validate_ack_failure=True,
    app_start_wait_ms=10_000,
    app_total_wait_ms=25_000,
)


def battery_profile(target_id: int = 0x42, *, can_id: int = 0x7FF) -> IapDeviceProfile:
    """Build a battery profile; 0x41/42/43 are shortcuts and custom byte IDs are accepted."""
    return IapDeviceProfile(
        f"电池 0x{target_id:02X}",
        target_id,
        can_id,
        pre_upgrade_wakeup_ms=BATTERY_PROFILE.pre_upgrade_wakeup_ms,
        disable_target_can_messages=True,
        ignore_validate_ack_failure=True,
        app_start_wait_ms=BATTERY_PROFILE.app_start_wait_ms,
        app_total_wait_ms=BATTERY_PROFILE.app_total_wait_ms,
    )
