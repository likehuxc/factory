from __future__ import annotations

from pathlib import Path

import pytest

from d7_factory_studio.core.evt import (
    EvtConfigError,
    agent_config_yaml,
    evt_can_mapping,
    group_label,
    interface_role_label,
    load_builtin_evt,
    load_evt_config,
    remap_evt_can,
)
from d7_factory_studio.core.models import CanMode


def test_evt1_catalog_and_routes() -> None:
    config = load_builtin_evt("EVT1")
    assert len(config.nodes) == 30
    assert [node.logic_id for node in config.nodes] == list(range(1, 31))
    assert config.nodes_for_bus("can7")[0].name == "neck_yaw_joint"
    assert config.nodes_for_bus("can5")[-1].dev_id == 0x58
    assert config.interfaces["can3"].mode is CanMode.CLASSIC


def test_evt2_is_default_layout() -> None:
    config = load_builtin_evt()
    assert config.variant == "EVT2"
    assert config.nodes_for_bus("can4")[0].logic_id == 1
    assert config.nodes_for_bus("can0")[0].logic_id == 7
    assert config.nodes_for_bus("can1")[0].logic_id == 15
    assert config.nodes_for_bus("can2")[0].logic_id == 23
    assert config.interfaces["can5"].role == "ota"


def test_agent_config_is_generated_from_evt_without_mandatory_limits() -> None:
    document = agent_config_yaml(load_builtin_evt("EVT2"))
    assert "robot_model: D7" in document
    assert "device_id: 17" in document
    assert "bus: can4" in document
    assert "position_min_rad" not in document
    assert document.count("logic_id:") == 30


def test_group_and_interface_roles_have_chinese_operator_labels() -> None:
    assert group_label("LEFT_ARM_HAND") == "左臂"
    assert group_label("RIGHT_ARM") == "右臂"
    assert group_label("HEAD_WAIST") == "躯干 & 头部"
    assert interface_role_label("chassis") == "底盘"


def test_evt_can_mapping_can_be_remapped_per_region() -> None:
    config = load_builtin_evt("EVT2")
    mapping = evt_can_mapping(config)
    mapping.update(head_torso="can7", left_arm="can6", right_arm="can3", chassis="can2")
    remapped = remap_evt_can(config, mapping)
    assert remapped.nodes_for_bus("can7")[0].logic_id == 1
    assert remapped.nodes_for_bus("can6")[0].logic_id == 7
    assert remapped.nodes_for_bus("can3")[0].logic_id == 15
    assert interface_role_label(remapped.interfaces["can6"].role) == "左臂"


def test_evt_can_mapping_rejects_ota_channel_conflict() -> None:
    config = load_builtin_evt("EVT2")
    mapping = evt_can_mapping(config)
    mapping["left_arm"] = "can5"
    with pytest.raises(EvtConfigError, match="升级通道"):
        remap_evt_can(config, mapping)


def test_rejects_non_d7_config(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 1\nrobot_model: D5\n", encoding="utf-8")
    with pytest.raises(EvtConfigError, match="D7"):
        load_evt_config(path)
