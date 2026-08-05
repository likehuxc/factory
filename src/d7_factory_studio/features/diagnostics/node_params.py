from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from d7_factory_studio.core.evt import EvtConfig, MotorNodeConfig
from d7_factory_studio.core.ports import RemoteCommandRequest


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
    binary = PurePosixPath(binary_path)
    if not binary.is_absolute() or not binary.name:
        raise ValueError("参数工具必须是远端绝对路径")
    flag = "-r" if operation == "read" else "-w"
    command = (
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
    binary_path: str,
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
