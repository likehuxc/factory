from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Signal

from d7_factory_studio.core.evt import EvtConfig, load_builtin_evt
from d7_factory_studio.core.models import ConnectionMode, LinkState


class ApplicationState(QObject):
    changed = Signal()
    activity_added = Signal(str, str, str)
    action_requested = Signal(str, object)
    task_event = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.connection_mode = ConnectionMode.PC_DIRECT
        self.link_state = LinkState.DISCONNECTED
        self.evt: EvtConfig = load_builtin_evt("EVT2")
        self.active_interface = self._default_interface()
        self.safety_locked = True
        self.fault_message = ""
        self.online_nodes = 0

    def _default_interface(self) -> str:
        return next(iter(self.evt.interfaces))

    def set_connection_mode(self, mode: ConnectionMode) -> None:
        if mode == self.connection_mode:
            return
        should_disconnect = self.link_state is not LinkState.DISCONNECTED
        self.connection_mode = mode
        self.link_state = LinkState.DISCONNECTED
        self.safety_locked = True
        self.online_nodes = 0
        self.fault_message = ""
        self.log(
            "连接", f"已切换到{'PC 直连' if mode is ConnectionMode.PC_DIRECT else 'Orin 远程'}，安全锁已恢复"
        )
        self.changed.emit()
        if should_disconnect:
            self.request("connection.disconnect")

    def set_evt(self, variant: str) -> None:
        config = load_builtin_evt(variant)
        if config.variant == self.evt.variant:
            return
        should_disconnect = self.link_state is not LinkState.DISCONNECTED
        self.evt = config
        self.active_interface = self._default_interface()
        self.link_state = LinkState.DISCONNECTED
        self.safety_locked = True
        self.online_nodes = 0
        self.fault_message = ""
        self.log("配置", f"已切换到 {config.variant}，连接已断开并恢复安全锁")
        self.changed.emit()
        if should_disconnect:
            self.request("connection.disconnect")

    def set_interface(self, interface: str) -> None:
        if interface not in self.evt.interfaces:
            raise ValueError(f"当前 {self.evt.variant} 不包含接口 {interface}")
        if interface == self.active_interface:
            return
        should_disconnect = self.link_state is not LinkState.DISCONNECTED
        self.active_interface = interface
        self.link_state = LinkState.DISCONNECTED
        self.online_nodes = 0
        self.safety_locked = True
        self.log("连接", f"当前接口切换为 {interface.upper()}，连接已断开并恢复安全锁")
        self.changed.emit()
        if should_disconnect:
            self.request("connection.disconnect")

    def set_link_state(self, state: LinkState, fault: str = "") -> None:
        self.link_state = state
        self.fault_message = fault
        if state is not LinkState.CONNECTED:
            self.safety_locked = True
            self.online_nodes = 0
        self.changed.emit()

    def unlock(self) -> None:
        if self.link_state is not LinkState.CONNECTED:
            raise RuntimeError("设备未连接，不能解除安全锁")
        self.safety_locked = False
        self.log("安全", "本次连接会话已解除安全锁", "warning")
        self.changed.emit()

    def lock(self, reason: str = "已手动锁定") -> None:
        self.safety_locked = True
        self.log("安全", reason)
        self.changed.emit()

    def report_fault(self, message: str) -> None:
        self.link_state = LinkState.FAULT
        self.fault_message = message
        self.safety_locked = True
        self.log("故障", message, "error")
        self.changed.emit()

    def request(self, action: str, **payload: Any) -> None:
        self.action_requested.emit(action, payload)

    def notify_task(self, action: str, event: str, payload: object = None) -> None:
        self.task_event.emit(action, event, payload)

    def log(self, source: str, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_added.emit(timestamp, source, f"{level}|{message}")
