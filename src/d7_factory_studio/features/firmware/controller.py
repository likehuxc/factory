from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.core.ports import CancellationToken, CanTransport
from d7_factory_studio.features.firmware.image import FirmwareImage
from d7_factory_studio.features.firmware.profiles import PMU_PROFILE, IapDeviceProfile
from d7_factory_studio.protocols.iap import (
    CMD_ENABLE_CAN,
    CMD_FILL_SEGMENT_DATA,
    CMD_GET_RUN_ROLE,
    CMD_GET_SOFT_VERSION,
    CMD_SET_FIRMWARE_SIZE,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    PROTOCOL_HEAD,
    RUN_ROLE_APP,
    RUN_ROLE_BOOTLOADER,
    IapAck,
    IapProtocol,
)

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, str], None]
FrameCallback = Callable[[str, CanFrame], None]


class FirmwareUpgradeError(RuntimeError):
    pass


class IapAckTimeout(FirmwareUpgradeError):
    pass


@dataclass(frozen=True, slots=True)
class UpgradeOptions:
    channel: int = 0
    can_mode: CanMode = CanMode.FD
    ack_timeout_ms: int = 500
    erase_timeout_ms: int = 3_000
    write_timeout_ms: int = 3_000
    boot_wait_ms: int = 3_000
    boot_total_wait_ms: int = 10_000
    app_start_wait_ms: int = 1_000
    app_total_wait_ms: int = 5_000
    pre_upgrade_wakeup_ms: int = 0
    disable_target_can_messages: bool = False
    data_frame_delay_ms: int = 2
    wait_data_frame_ack: bool = False
    data_frame_ack_timeout_ms: int = 200
    ignore_validate_ack_failure: bool = False
    set_firmware_retries: int = 1
    set_segment_retries: int = 2
    validate_segment_retries: int = 2
    close_transport_on_finish: bool = False

    @classmethod
    def for_profile(cls, profile: IapDeviceProfile, **overrides: object) -> UpgradeOptions:
        options = cls(
            pre_upgrade_wakeup_ms=profile.pre_upgrade_wakeup_ms,
            disable_target_can_messages=profile.disable_target_can_messages,
            ignore_validate_ack_failure=profile.ignore_validate_ack_failure,
            app_start_wait_ms=profile.app_start_wait_ms,
            app_total_wait_ms=profile.app_total_wait_ms,
        )
        return replace(options, **overrides)


def build_upgrade_preview_frames(
    protocol: IapProtocol,
    image: FirmwareImage,
    options: UpgradeOptions | None = None,
    *,
    max_data_frames: int = 10,
) -> list[tuple[str, CanFrame]]:
    """Build the same initial command sequence as a real upgrade without I/O."""
    if max_data_frames < 0:
        raise ValueError("max_data_frames must be non-negative")
    opts = options or UpgradeOptions()
    frames: list[tuple[str, CanFrame]] = []
    if opts.pre_upgrade_wakeup_ms:
        frames.append(("预唤醒查询角色", protocol.query_role()))
    frames.extend(
        (
            ("查询当前角色", protocol.query_role()),
            ("重启进入 BOOT", protocol.reboot_to_bootloader()),
            ("重启后查询角色", protocol.query_role()),
        )
    )
    if opts.disable_target_can_messages:
        frames.append(("关闭目标 CAN 消息", protocol.set_can_messages_enabled(False)))
    frames.append((f"设置固件大小 {image.size}", protocol.set_firmware_size(image.size)))
    first_section = image.sections()[0]
    frames.append(
        (
            f"首段信息 section={first_section.number} size={len(first_section.data)}",
            protocol.set_segment_info(first_section.number, len(first_section.data)),
        )
    )
    frames.extend(
        (f"首段数据包 #{index}", frame)
        for index, frame in enumerate(
            protocol.segment_data_frames(first_section.data)[:max_data_frames], start=1
        )
    )
    return frames


class FirmwareUpgradeController:
    def __init__(
        self,
        transport: CanTransport,
        protocol: IapProtocol | None = None,
        *,
        default_options: UpgradeOptions | None = None,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_frame: FrameCallback | None = None,
    ) -> None:
        self.transport = transport
        self.protocol = protocol or IapProtocol(PMU_PROFILE.target_id, PMU_PROFILE.can_id)
        self.default_options = default_options or UpgradeOptions()
        self.on_log = on_log or (lambda _message: None)
        self.on_progress = on_progress or (lambda _percent, _message: None)
        self.on_frame = on_frame or (lambda _direction, _frame: None)
        self._internal_token = CancellationToken()

    @classmethod
    def for_profile(
        cls,
        transport: CanTransport,
        profile: IapDeviceProfile,
        **callbacks: object,
    ) -> FirmwareUpgradeController:
        return cls(
            transport,
            IapProtocol(profile.target_id, profile.can_id),
            default_options=UpgradeOptions.for_profile(profile),
            **callbacks,
        )

    def cancel(self) -> None:
        self._internal_token.cancel()

    def upgrade(
        self,
        image: FirmwareImage,
        options: UpgradeOptions | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        opts = options or self.default_options
        external_token = token or CancellationToken()
        self._internal_token = CancellationToken()
        opened_here = False
        self._progress(0, "准备升级")
        try:
            self._check_cancelled(external_token)
            if not self.transport.is_open:
                self.transport.open(opts.channel, opts.can_mode)
                opened_here = True
            if opts.pre_upgrade_wakeup_ms:
                self._send(self.protocol.query_role(), external_token)
                self._sleep(opts.pre_upgrade_wakeup_ms, external_token)
            for warning in image.warnings:
                self.on_log(f"警告：{warning}")
            role = self.query_role(opts.ack_timeout_ms, external_token)
            if role == RUN_ROLE_APP:
                self.on_log("当前角色 APP，重启进入 BOOT")
                self._send(self.protocol.reboot_to_bootloader(), external_token)
                self._sleep(opts.boot_wait_ms, external_token)
                role = self._wait_for_role(RUN_ROLE_BOOTLOADER, opts.boot_total_wait_ms, opts, external_token)
            if role != RUN_ROLE_BOOTLOADER:
                raise FirmwareUpgradeError("设备未进入 BOOT，升级停止")
            if opts.disable_target_can_messages:
                self._retry_command(
                    lambda: self.protocol.set_can_messages_enabled(False),
                    CMD_ENABLE_CAN,
                    opts.ack_timeout_ms,
                    opts.set_segment_retries,
                    external_token,
                )
            self._retry_command(
                lambda: self.protocol.set_firmware_size(image.size),
                CMD_SET_FIRMWARE_SIZE,
                opts.erase_timeout_ms,
                opts.set_firmware_retries,
                external_token,
            )
            sent_bytes = 0
            for section in image.sections():
                self._send_section(section.number, section.data, opts, external_token)
                sent_bytes += len(section.data)
                self._progress(min(99, int(sent_bytes * 100 / image.size)), f"写入分段 {section.number}")
            self._send(self.protocol.jump_to_app(), external_token)
            self._sleep(opts.app_start_wait_ms, external_token)
            role = self._wait_for_role(RUN_ROLE_APP, opts.app_total_wait_ms, opts, external_token)
            if role != RUN_ROLE_APP:
                raise FirmwareUpgradeError("等待 APP 启动超时")
            self._progress(100, "升级完成")
            self.on_log("固件升级完成，设备已进入 APP")
        finally:
            if opts.close_transport_on_finish and (opened_here or self.transport.is_open):
                self.transport.close()

    def query_role(self, timeout_ms: int = 500, token: CancellationToken | None = None) -> str:
        if token is None:
            self._internal_token = CancellationToken()
            token = self._internal_token
        ack = self._command_with_ack(self.protocol.query_role(), CMD_GET_RUN_ROLE, timeout_ms, token)
        return self.protocol.ack_role(ack)

    def query_software_version(self, timeout_ms: int = 500, token: CancellationToken | None = None) -> str:
        if token is None:
            self._internal_token = CancellationToken()
            token = self._internal_token
        ack = self._command_with_ack(
            self.protocol.get_software_version(),
            CMD_GET_SOFT_VERSION,
            timeout_ms,
            token,
        )
        return self.protocol.ack_software_version(ack)

    def _send_section(
        self,
        section_number: int,
        data: bytes,
        options: UpgradeOptions,
        token: CancellationToken,
    ) -> None:
        self._retry_command(
            lambda: self.protocol.set_segment_info(section_number, len(data)),
            CMD_SET_SEGMENT_INFO,
            options.ack_timeout_ms,
            options.set_segment_retries,
            token,
        )
        for frame in self.protocol.segment_data_frames(data):
            if options.wait_data_frame_ack:
                self._command_with_ack(
                    frame,
                    CMD_FILL_SEGMENT_DATA,
                    options.data_frame_ack_timeout_ms,
                    token,
                    accept_tx_can_id=False,
                )
            else:
                self._send(frame, token)
            if options.data_frame_delay_ms:
                self._sleep(options.data_frame_delay_ms, token)
        self._validate_section(section_number, data, options, token)

    def _validate_section(
        self,
        section_number: int,
        data: bytes,
        options: UpgradeOptions,
        token: CancellationToken,
    ) -> None:
        attempts = 1 if options.ignore_validate_ack_failure else options.validate_segment_retries + 1
        timeout = options.ack_timeout_ms if options.ignore_validate_ack_failure else options.write_timeout_ms
        last_error = ""
        for _ in range(attempts):
            try:
                ack = self._command_with_ack(
                    self.protocol.validate_segment(section_number, data),
                    CMD_VALIDATE_SEGMENT_DATA,
                    timeout,
                    token,
                )
            except FirmwareUpgradeError as exc:
                last_error = str(exc)
                if options.ignore_validate_ack_failure:
                    self.on_log(f"忽略分段 {section_number} 校验 ACK：{exc}")
                    return
                continue
            if ack.params[0] == 1:
                return
            last_error = f"分段 {section_number} 校验失败，ACK status={ack.params[0]}"
            if options.ignore_validate_ack_failure:
                self.on_log(last_error)
                return
        raise FirmwareUpgradeError(last_error or f"分段 {section_number} 校验失败")

    def _wait_for_role(
        self,
        expected: str,
        total_timeout_ms: int,
        options: UpgradeOptions,
        token: CancellationToken,
    ) -> str:
        deadline = time.monotonic() + total_timeout_ms / 1000
        last_role = RUN_ROLE_APP if expected == RUN_ROLE_BOOTLOADER else RUN_ROLE_BOOTLOADER
        while time.monotonic() < deadline:
            self._check_cancelled(token)
            try:
                last_role = self.query_role(options.ack_timeout_ms, token)
            except IapAckTimeout:
                self._sleep(200, token)
                continue
            if last_role == expected:
                return last_role
            self._sleep(200, token)
        return last_role

    def _retry_command(
        self,
        frame_factory: Callable[[], CanFrame],
        expected_command: int,
        timeout_ms: int,
        retries: int,
        token: CancellationToken,
    ) -> IapAck:
        last_error: FirmwareUpgradeError | None = None
        for _ in range(retries + 1):
            self._check_cancelled(token)
            try:
                return self._command_with_ack(frame_factory(), expected_command, timeout_ms, token)
            except FirmwareUpgradeError as exc:
                last_error = exc
        raise FirmwareUpgradeError(str(last_error) if last_error else "IAP command retry failed")

    def _command_with_ack(
        self,
        frame: CanFrame,
        expected_command: int,
        timeout_ms: int,
        token: CancellationToken,
        *,
        accept_tx_can_id: bool = True,
    ) -> IapAck:
        self._send(frame, token)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            self._check_cancelled(token)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise IapAckTimeout(f"等待 0x{expected_command:02X} ACK 超时")
            for received in self.transport.receive(min(remaining_ms, 50)):
                self.on_frame("RX", received)
                accepted_ids = {self.protocol.target_id}
                if accept_tx_can_id:
                    accepted_ids.add(self.protocol.can_id)
                data = received.data
                if received.arbitration_id not in accepted_ids:
                    continue
                if len(data) < 3 or data[:3] != bytes(
                    (PROTOCOL_HEAD, self.protocol.target_id, expected_command)
                ):
                    continue
                try:
                    return self.protocol.parse_ack(data, expected_command)
                except ValueError as exc:
                    raise FirmwareUpgradeError(f"ACK 解析失败: {exc}") from exc

    def _send(self, frame: CanFrame, token: CancellationToken) -> None:
        self._check_cancelled(token)
        try:
            self.transport.send(frame)
        except Exception as exc:
            raise FirmwareUpgradeError(f"CAN 发送失败: {exc}") from exc
        self.on_frame("TX", frame)

    def _sleep(self, duration_ms: int, token: CancellationToken) -> None:
        deadline = time.monotonic() + max(duration_ms, 0) / 1000
        while time.monotonic() < deadline:
            self._check_cancelled(token)
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _check_cancelled(self, token: CancellationToken) -> None:
        token.raise_if_cancelled()
        self._internal_token.raise_if_cancelled()

    def _progress(self, percent: int, message: str) -> None:
        self.on_progress(percent, message)
