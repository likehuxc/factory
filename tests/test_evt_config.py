from __future__ import annotations

from pathlib import Path

import pytest

from d7_factory_studio.core.evt import EvtConfigError, load_builtin_evt, load_evt_config
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


def test_rejects_non_d7_config(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 1\nrobot_model: D5\n", encoding="utf-8")
    with pytest.raises(EvtConfigError, match="D7"):
        load_evt_config(path)
