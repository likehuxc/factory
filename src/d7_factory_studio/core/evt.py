from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from d7_factory_studio.core.models import CanMode


class EvtConfigError(ValueError):
    pass


CAN_REGIONS = ("head_torso", "left_arm", "right_arm", "chassis")
CAN_REGION_LABELS = {
    "head_torso": "躯干 & 头部",
    "left_arm": "左臂",
    "right_arm": "右臂",
    "chassis": "底盘",
}


def group_region(group: str) -> str:
    normalized = group.strip().upper()
    if normalized in {"HEAD_TORSO", "HEAD_WAIST"}:
        return "head_torso"
    if normalized in {"LEFT_ARM", "LEFT_ARM_HAND"}:
        return "left_arm"
    if normalized in {"RIGHT_ARM", "RIGHT_ARM_HAND"}:
        return "right_arm"
    if normalized == "CHASSIS":
        return "chassis"
    raise EvtConfigError(f"未知 D7 电机分组: {group}")


def group_label(group: str) -> str:
    return CAN_REGION_LABELS[group_region(group)]


def interface_role_label(role: str) -> str:
    labels = []
    for value in role.split("+"):
        normalized = value.strip().lower()
        if normalized == "ota":
            labels.append("升级")
        elif normalized in {"head_torso", "head_waist"}:
            labels.append(CAN_REGION_LABELS["head_torso"])
        elif normalized in {"left_arm", "left_arm_hand"}:
            labels.append(CAN_REGION_LABELS["left_arm"])
        elif normalized in {"right_arm", "right_arm_hand"}:
            labels.append(CAN_REGION_LABELS["right_arm"])
        elif normalized == "chassis":
            labels.append(CAN_REGION_LABELS["chassis"])
        else:
            labels.append(value.replace("_", " "))
    return " / ".join(labels)


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
    factory_name: str = ""
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
                factory_name=str(item.get("factory_name", "")),
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


def evt_can_mapping(config: EvtConfig) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for node in config.nodes:
        mapping.setdefault(group_region(node.group), node.bus)
    missing = set(CAN_REGIONS) - set(mapping)
    if missing:
        raise EvtConfigError(f"EVT 配置缺少区域: {sorted(missing)}")
    return mapping


def remap_evt_can(config: EvtConfig, mapping: dict[str, str]) -> EvtConfig:
    missing = set(CAN_REGIONS) - set(mapping)
    if missing:
        raise EvtConfigError(f"CAN 映射缺少区域: {sorted(missing)}")
    normalized = {region: str(mapping[region]).strip().lower() for region in CAN_REGIONS}
    if any(not bus.startswith("can") or not bus[3:].isdigit() for bus in normalized.values()):
        raise EvtConfigError("CAN 通道必须使用 can0..can7 格式")
    if any(not 0 <= int(bus[3:]) <= 7 for bus in normalized.values()):
        raise EvtConfigError("CAN 通道必须在 can0..can7 范围内")
    ota_buses = {item.name for item in config.interfaces.values() if item.role == "ota"}
    conflicts = ota_buses.intersection(normalized.values())
    if conflicts:
        raise EvtConfigError(f"电机 CAN 通道不能占用升级通道: {sorted(conflicts)}")

    nodes = tuple(replace(node, bus=normalized[group_region(node.group)]) for node in config.nodes)
    interface_regions: dict[str, list[str]] = {}
    for region in CAN_REGIONS:
        interface_regions.setdefault(normalized[region], []).append(region)

    interfaces: dict[str, CanInterfaceConfig] = {}
    for bus, regions in interface_regions.items():
        source = config.interfaces.get(bus)
        interfaces[bus] = CanInterfaceConfig(
            name=bus,
            role="+".join(regions),
            mode=CanMode.FD,
            bitrate=source.bitrate if source and source.mode is CanMode.FD else 1_000_000,
            dbitrate=source.dbitrate if source and source.mode is CanMode.FD else 5_000_000,
            sample_point=source.sample_point if source else None,
            data_sample_point=source.data_sample_point if source else None,
            restart_ms=source.restart_ms if source else 100,
        )
    for item in config.interfaces.values():
        if item.role == "ota":
            interfaces[item.name] = item
    return replace(config, interfaces=interfaces, nodes=nodes)


def agent_config_yaml(config: EvtConfig) -> str:
    """Build the Orin agent inventory from the active EVT configuration."""
    motors: list[dict[str, object]] = []
    for node in config.nodes:
        motor: dict[str, object] = {
            "logic_id": node.logic_id,
            "device_id": node.dev_id,
            "name": node.name,
            "bus": node.bus,
            "group": node.group,
        }
        if node.position_min_rad is not None and node.position_max_rad is not None:
            motor.update(
                position_min_rad=node.position_min_rad,
                position_max_rad=node.position_max_rad,
                limits_verified=True,
            )
        motors.append(motor)
    document = {
        "schema_version": 1,
        "robot_model": "D7",
        "agent": {"deadman_timeout_ms": 1000, "state_period_ms": 100},
        "motors": motors,
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
