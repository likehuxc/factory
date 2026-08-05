from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from d7_factory_studio.core.models import CanMode


class EvtConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CanInterfaceConfig:
    name: str
    role: str
    mode: CanMode
    bitrate: int
    dbitrate: int | None = None
    sample_point: float | None = None
    data_sample_point: float | None = None
    restart_ms: int = 100


@dataclass(frozen=True, slots=True)
class MotorNodeConfig:
    label: str
    name: str
    logic_id: int
    dev_id: int
    group: str
    bus: str
    direction: int = 1
    position_min_rad: float | None = None
    position_max_rad: float | None = None


@dataclass(frozen=True, slots=True)
class BroadcastConfig:
    request_id: int
    response_base_id: int
    reserved_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class EvtConfig:
    schema_version: int
    robot_model: str
    variant: str
    interfaces: dict[str, CanInterfaceConfig]
    nodes: tuple[MotorNodeConfig, ...]
    fixed_groups: dict[str, tuple[int, ...]]
    broadcast: BroadcastConfig

    def nodes_for_bus(self, bus: str) -> tuple[MotorNodeConfig, ...]:
        return tuple(node for node in self.nodes if node.bus == bus)

    def group_nodes(self, group: str) -> tuple[MotorNodeConfig, ...]:
        ids = set(self.fixed_groups.get(group, ()))
        return tuple(node for node in self.nodes if node.logic_id in ids)


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise EvtConfigError(f"{field_name} 必须是整数")
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise EvtConfigError(f"{field_name} 必须是整数") from exc


def load_evt_config(path: str | Path) -> EvtConfig:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvtConfigError(f"无法读取 EVT 配置: {exc}") from exc
    if not isinstance(raw, dict):
        raise EvtConfigError("EVT 配置根节点必须是对象")
    if raw.get("robot_model") != "D7":
        raise EvtConfigError("仅支持 robot_model: D7")
    if _as_int(raw.get("schema_version", 0), "schema_version") != 1:
        raise EvtConfigError("不支持的 EVT schema_version")

    raw_interfaces = raw.get("interfaces")
    if not isinstance(raw_interfaces, dict) or not raw_interfaces:
        raise EvtConfigError("interfaces 不能为空")
    interfaces: dict[str, CanInterfaceConfig] = {}
    for name, item in raw_interfaces.items():
        if not isinstance(item, dict):
            raise EvtConfigError(f"接口 {name} 配置无效")
        try:
            mode = CanMode(str(item["mode"]))
            bitrate = _as_int(item["bitrate"], f"{name}.bitrate")
        except (KeyError, ValueError) as exc:
            raise EvtConfigError(f"接口 {name} 缺少有效 mode/bitrate") from exc
        dbitrate = item.get("dbitrate")
        if mode is CanMode.FD and dbitrate is None:
            raise EvtConfigError(f"CAN FD 接口 {name} 缺少 dbitrate")
        interfaces[str(name)] = CanInterfaceConfig(
            name=str(name),
            role=str(item.get("role", "general")),
            mode=mode,
            bitrate=bitrate,
            dbitrate=_as_int(dbitrate, f"{name}.dbitrate") if dbitrate is not None else None,
            sample_point=float(item["sample_point"]) if "sample_point" in item else None,
            data_sample_point=float(item["data_sample_point"]) if "data_sample_point" in item else None,
            restart_ms=_as_int(item.get("restart_ms", 100), f"{name}.restart_ms"),
        )

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise EvtConfigError("nodes 不能为空")
    nodes: list[MotorNodeConfig] = []
    logic_ids: set[int] = set()
    bus_device_ids: set[tuple[str, int]] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise EvtConfigError("节点配置必须是对象")
        try:
            logic_id = _as_int(item["logic_id"], "logic_id")
            dev_id = _as_int(item["dev_id"], "dev_id")
            bus = str(item["bus"])
        except KeyError as exc:
            raise EvtConfigError(f"节点缺少字段: {exc.args[0]}") from exc
        if bus not in interfaces:
            raise EvtConfigError(f"节点 {logic_id} 使用未知接口 {bus}")
        if logic_id in logic_ids:
            raise EvtConfigError(f"重复 logic_id: {logic_id}")
        if (bus, dev_id) in bus_device_ids:
            raise EvtConfigError(f"接口 {bus} 存在重复 dev_id: 0x{dev_id:X}")
        logic_ids.add(logic_id)
        bus_device_ids.add((bus, dev_id))
        nodes.append(
            MotorNodeConfig(
                label=str(item.get("label", item.get("name", logic_id))),
                name=str(item["name"]),
                logic_id=logic_id,
                dev_id=dev_id,
                group=str(item["group"]),
                bus=bus,
                direction=_as_int(item.get("direction", 1), "direction"),
                position_min_rad=float(item["position_min_rad"]) if "position_min_rad" in item else None,
                position_max_rad=float(item["position_max_rad"]) if "position_max_rad" in item else None,
            )
        )

    raw_groups = raw.get("fixed_groups", {})
    if not isinstance(raw_groups, dict):
        raise EvtConfigError("fixed_groups 必须是对象")
    groups: dict[str, tuple[int, ...]] = {}
    for name, values in raw_groups.items():
        if not isinstance(values, list) or not values:
            raise EvtConfigError(f"固定分组 {name} 不能为空")
        ids = tuple(_as_int(value, f"fixed_groups.{name}") for value in values)
        unknown = set(ids) - logic_ids
        if unknown:
            raise EvtConfigError(f"固定分组 {name} 包含未知节点: {sorted(unknown)}")
        groups[str(name)] = ids

    raw_broadcast = raw.get("broadcast", {})
    if not isinstance(raw_broadcast, dict):
        raise EvtConfigError("broadcast 必须是对象")
    broadcast = BroadcastConfig(
        request_id=_as_int(raw_broadcast.get("request_id", 0x300), "broadcast.request_id"),
        response_base_id=_as_int(raw_broadcast.get("response_base_id", 0x100), "broadcast.response_base_id"),
        reserved_ids=frozenset(
            _as_int(value, "broadcast.reserved_ids") for value in raw_broadcast.get("reserved_ids", [])
        ),
    )
    return EvtConfig(
        schema_version=1,
        robot_model="D7",
        variant=str(raw.get("variant", source.stem)).upper(),
        interfaces=interfaces,
        nodes=tuple(sorted(nodes, key=lambda node: node.logic_id)),
        fixed_groups=groups,
        broadcast=broadcast,
    )


def builtin_evt_path(variant: str = "EVT2") -> Path:
    normalized = variant.strip().lower()
    if normalized not in {"evt1", "evt2"}:
        raise EvtConfigError(f"未知 EVT 版本: {variant}")
    return Path(str(files("d7_factory_studio.config").joinpath(f"{normalized}.yaml")))


def load_builtin_evt(variant: str = "EVT2") -> EvtConfig:
    return load_evt_config(builtin_evt_path(variant))

