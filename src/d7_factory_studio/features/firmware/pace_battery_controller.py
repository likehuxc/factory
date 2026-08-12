from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.core.ports import CancellationToken, CanTransport
from d7_factory_studio.features.firmware.controller import FirmwareUpgradeError
from d7_factory_studio.protocols.pace_bms_upgrade import (
    BLOCK_SIZE,
    COMMAND_DATA,
    COMMAND_ERASE,
    COMMAND_FINISH,
    COMMAND_PREPARE,
    PaceBmsUpgradeProtocol,
    firmware_identifier,
    padded_blocks,
)

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, str], None]
FrameCallback = Callable[[str, CanFrame], None]


class PaceAckTimeout(FirmwareUpgradeError):
    pass


class PaceNegativeAck(FirmwareUpgradeError):
    pass


@dataclass(frozen=True, slots=True)
class PaceUpgradeOptions:
    channel: int = 0
    can_mode: CanMode = CanMode.FD
    prepare_timeout_ms: int = 3_000
    erase_timeout_ms: int = 3_000
    block_timeout_ms: int = 3_000
    finish_timeout_ms: int = 3_000
    pre_upgrade_pause_ms: int = 2_000
    post_erase_pause_ms: int = 2_000
    command_retries: int = 1
    block_retries: int = 0
    frame_send_attempts: int = 10
    frame_retry_delay_ms: int = 1
    close_transport_on_finish: bool = False

    def __post_init__(self) -> None:
        values = (
            self.prepare_timeout_ms,
            self.erase_timeout_ms,
            self.block_timeout_ms,
            self.finish_timeout_ms,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Pace ACK 超时必须大于 0 ms")
        if self.pre_upgrade_pause_ms < 0 or self.post_erase_pause_ms < 0:
            raise ValueError("升级等待时间不能为负数")
        if self.command_retries < 0 or self.block_retries < 0:
            raise ValueError("重试次数不能为负数")
        if self.frame_send_attempts < 1 or self.frame_retry_delay_ms < 0:
            raise ValueError("物理帧发送次数必须大于 0，重试间隔不能为负数")


class PaceBatteryUpgradeController:
    def __init__(
        self,
        transport: CanTransport,
        protocol: PaceBmsUpgradeProtocol | None = None,
        *,
        default_options: PaceUpgradeOptions | None = None,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_frame: FrameCallback | None = None,
    ) -> None:
        self.transport = transport
        self.protocol = protocol or PaceBmsUpgradeProtocol()
        self.default_options = default_options or PaceUpgradeOptions()
        self.on_log = on_log or (lambda _message: None)
        self.on_progress = on_progress or (lambda _percent, _message: None)
        self.on_frame = on_frame or (lambda _direction, _frame: None)
        self._internal_token = CancellationToken()

    def cancel(self) -> None:
        self._internal_token.cancel()

    def upgrade_file(
        self,
        path: str | Path,
        options: PaceUpgradeOptions | None = None,
        token: CancellationToken | None = None,
    ) -> dict[str, int | str]:
        source = Path(path)
        if source.suffix.lower() != ".bin":
            raise ValueError("Pace V9213 升级只支持 .bin 固件")
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ValueError(f"无法读取固件文件: {exc}") from exc
        identifier = firmware_identifier(source)
        self.upgrade(data, identifier, options=options, token=token)
        return {
            "identifier": identifier,
            "size": len(data),
            "blocks": len(padded_blocks(data)),
        }

    def upgrade(
        self,
        data: bytes,
        identifier: str,
        options: PaceUpgradeOptions | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        opts = options or self.default_options
        external_token = token or CancellationToken()
        self._internal_token = CancellationToken()
        blocks = padded_blocks(data)
        opened_here = False
        self._progress(0, f"准备 Pace 电池升级，共 {len(blocks)} 块")
        try:
            self._check_cancelled(external_token)
            if not self.transport.is_open:
                self.transport.open(opts.channel, opts.can_mode)
                opened_here = True
            self._drain_receive_queue()
            if opts.pre_upgrade_pause_ms:
                self.on_log("已暂停普通电池通信，等待升级链路稳定")
                self._sleep(opts.pre_upgrade_pause_ms, external_token)

            identifier_frames = self.protocol.identifier_frames(identifier)
            self._command(
                identifier_frames,
                COMMAND_PREPARE,
                opts.prepare_timeout_ms,
                0,
                external_token,
                "发送固件标识",
                opts,
            )
            self._command(
                (self.protocol.prepare_frame(),),
                COMMAND_PREPARE,
                opts.prepare_timeout_ms,
                opts.command_retries,
                external_token,
                "进入升级模式",
                opts,
            )
            self._progress(1, "已进入 Pace 升级模式")
            self._command(
                (self.protocol.erase_frame(),),
                COMMAND_ERASE,
                opts.erase_timeout_ms,
                0,
                external_token,
                "擦除固件区",
                opts,
            )
            self._progress(2, "固件区擦除完成")
            if opts.post_erase_pause_ms:
                self._sleep(opts.post_erase_pause_ms, external_token)

            for index, block in enumerate(blocks, start=1):
                offset = (index - 1) * BLOCK_SIZE
                self._command(
                    self.protocol.data_block_frames(offset, block),
                    COMMAND_DATA,
                    opts.block_timeout_ms,
                    opts.block_retries,
                    external_token,
                    f"写入第 {index}/{len(blocks)} 块",
                    opts,
                )
                percent = min(99, 2 + int(index * 97 / len(blocks)))
                self._progress(percent, f"已写入 {index}/{len(blocks)} 块")

            self._command(
                (self.protocol.finish_frame(),),
                COMMAND_FINISH,
                opts.finish_timeout_ms,
                0,
                external_token,
                "结束升级",
                opts,
            )
            self._progress(100, "Pace 电池升级完成")
            self.on_log(f"Pace 电池升级完成：{len(data):,} bytes，{len(blocks)} 块")
        finally:
            if opts.close_transport_on_finish and (opened_here or self.transport.is_open):
                self.transport.close()
            self.on_log("电池升级任务结束，普通通信可恢复")

    def _command(
        self,
        frames: Iterable[CanFrame],
        command: int,
        timeout_ms: int,
        retries: int,
        token: CancellationToken,
        label: str,
        options: PaceUpgradeOptions,
    ) -> None:
        batch = tuple(frames)
        last_error: PaceAckTimeout | None = None
        for attempt in range(retries + 1):
            self._check_cancelled(token)
            self._drain_receive_queue()
            if attempt:
                self.on_log(f"{label} ACK 超时，重试 {attempt}/{retries}")
            for frame in batch:
                self._send(frame, token, options)
            try:
                self._wait_for_ack(command, timeout_ms, token)
                return
            except PaceAckTimeout as exc:
                last_error = exc
        raise FirmwareUpgradeError(str(last_error) if last_error else f"{label}失败")

    def _wait_for_ack(self, command: int, timeout_ms: int, token: CancellationToken) -> CanFrame:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            self._check_cancelled(token)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise PaceAckTimeout(
                    f"等待 Pace 0x{command:02X} ACK 超时（期望 ID 0x{self.protocol.rx_id(command):08X}）"
                )
            for frame in self.transport.receive(min(remaining_ms, 50)):
                self.on_frame("RX", frame)
                result = self.protocol.ack_result(frame, command)
                if result is None:
                    continue
                if result != 0x01:
                    raise PaceNegativeAck(f"Pace 0x{command:02X} 返回失败码 0x{result:02X}")
                if self.protocol.is_valid_ack(frame, command):
                    return frame

    def _drain_receive_queue(self) -> None:
        for _ in range(32):
            frames = self.transport.receive(0)
            if not frames:
                return
            for frame in frames:
                self.on_frame("RX", frame)

    def _send(self, frame: CanFrame, token: CancellationToken, options: PaceUpgradeOptions) -> None:
        last_error: Exception | None = None
        for attempt in range(1, options.frame_send_attempts + 1):
            self._check_cancelled(token)
            try:
                self.transport.send(frame)
                self.on_frame("TX", frame)
                return
            except Exception as exc:
                last_error = exc
                if attempt < options.frame_send_attempts and options.frame_retry_delay_ms:
                    self._sleep(options.frame_retry_delay_ms, token)
        raise FirmwareUpgradeError(
            f"Pace CAN 发送失败（已尝试 {options.frame_send_attempts} 次）: {last_error}"
        ) from last_error

    def _sleep(self, duration_ms: int, token: CancellationToken) -> None:
        deadline = time.monotonic() + max(0, duration_ms) / 1000
        while time.monotonic() < deadline:
            self._check_cancelled(token)
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _check_cancelled(self, token: CancellationToken) -> None:
        token.raise_if_cancelled()
        self._internal_token.raise_if_cancelled()

    def _progress(self, percent: int, message: str) -> None:
        self.on_progress(max(0, min(100, int(percent))), message)
