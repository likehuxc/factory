from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from d7_factory_studio.core.evt import EvtConfig, MotorNodeConfig
from d7_factory_studio.core.ports import RemoteCommandRequest

DEFAULT_NODE_PARAMETER_BINARY = "/opt/actuator_sdk/jihua_calib_param_factory"
DEFAULT_NODE_PARAMETER_CONFIG = "/opt/actuator_sdk/config/calibration_info.yaml"


def validate_remote_absolute_path(path: str, label: str) -> str:
    value = path.strip()
    remote_path = PurePosixPath(value)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError(f"{label}包含非法字符")
    if not remote_path.is_absolute() or not remote_path.name:
        raise ValueError(f"{label}必须是远端绝对路径")
    return str(remote_path)


@dataclass(frozen=True, slots=True)
class NodeParameterOperation:
    node: MotorNodeConfig
    operation: str
    request: RemoteCommandRequest


def operation_request(
    binary_path: str, node: MotorNodeConfig, operation: str, timeout_s: int = 40
) -> RemoteCommandRequest:
    if operation not in {"read", "write"}:
        raise ValueError(f"不支持的节点参数操作: {operation}")
    binary = PurePosixPath(validate_remote_absolute_path(binary_path, "参数工具"))
    flag = "-r" if operation == "read" else "-w"
    binary_text = str(binary)
    unavailable_message = shlex.quote(f"参数工具不存在或不可执行: {binary_text}")
    command = (
        f"test -x {shlex.quote(binary_text)} || "
        f"{{ echo {unavailable_message} >&2; exit 126; }}; "
        f"cd {shlex.quote(str(binary.parent))} && "
        f"timeout --signal=TERM --kill-after=2s {int(timeout_s)}s "
        f"{shlex.quote('./' + binary.name)} {flag} {node.logic_id}"
    )
    return RemoteCommandRequest(
        ("sh", "-lc", command),
        timeout_s=timeout_s + 5,
        stream_output=True,
        evidence_label=f"node-{operation}-{node.logic_id}",
    )


def build_node_plan(
    evt: EvtConfig,
    binary_path: str = DEFAULT_NODE_PARAMETER_BINARY,
    *,
    buses: tuple[str, ...] = (),
    logic_ids: tuple[int, ...] = (),
    write_then_read: bool = False,
    timeout_s: int = 40,
) -> tuple[NodeParameterOperation, ...]:
    unknown_buses = set(buses) - set(evt.interfaces)
    if unknown_buses:
        raise ValueError(f"EVT 配置中不存在接口: {sorted(unknown_buses)}")
    selected = [
        node
        for node in evt.nodes
        if (not buses or node.bus in buses) and (not logic_ids or node.logic_id in logic_ids)
    ]
    unknown_ids = set(logic_ids) - {node.logic_id for node in evt.nodes}
    if unknown_ids:
        raise ValueError(f"EVT 配置中不存在节点: {sorted(unknown_ids)}")
    operations = ("write", "read") if write_then_read else ("read",)
    return tuple(
        NodeParameterOperation(node, operation, operation_request(binary_path, node, operation, timeout_s))
        for node in selected
        for operation in operations
    )
