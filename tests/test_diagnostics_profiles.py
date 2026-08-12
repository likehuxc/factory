from __future__ import annotations

from dataclasses import replace

import pytest

from d7_factory_studio.core.evt import load_builtin_evt
from d7_factory_studio.core.models import CanMode
from d7_factory_studio.features.diagnostics.broadcast import (
    broadcast_request,
    responding_node,
    summarize_responses,
)
from d7_factory_studio.features.diagnostics.commands import (
    clear_dmesg_request,
    configure_requests,
    parse_stage_sections,
    required_tools_request,
    stage_request,
)
from d7_factory_studio.features.diagnostics.models import (
    DiagnosticProfile,
    DiagnosticStage,
    StageEvidence,
)
from d7_factory_studio.features.diagnostics.node_params import (
    DEFAULT_NODE_PARAMETER_BINARY,
    DEFAULT_NODE_PARAMETER_CONFIG,
    build_node_plan,
    validate_remote_absolute_path,
)
from d7_factory_studio.features.diagnostics.parsers import evaluate, extract_error_lines, parse_link_snapshot
from d7_factory_studio.features.diagnostics.profiles import (
    build_profile,
    is_safe_standard_id,
    safe_random_ids,
)
from d7_factory_studio.features.diagnostics.timing import best_candidate, tdcr


def test_profiles_follow_injected_evt_interfaces() -> None:
    evt1, evt2 = load_builtin_evt("EVT1"), load_builtin_evt("EVT2")
    evt1_classic = build_profile(evt1, "can3", DiagnosticProfile.QUICK, fixed_can_id=0x6A5)
    evt2_classic = build_profile(evt2, "can5", DiagnosticProfile.QUICK, fixed_can_id=0x6A5)
    evt2_fd = build_profile(evt2, "can4", DiagnosticProfile.STANDARD, fixed_can_id=0x6A5)
    assert evt1.interfaces["can3"].mode is CanMode.CLASSIC
    assert evt1_classic[0].payload_length == evt2_classic[0].payload_length == 8
    assert evt2_fd[0].payload_length == 64
    assert [stage.duration_s for stage in evt2_fd] == [240, 240, 120]
    assert build_profile(evt2, "can4", DiagnosticProfile.LONG, fixed_can_id=0x6A5)[1].duration_s == 900


def test_safe_ids_derive_from_evt_nodes_and_broadcast() -> None:
    evt = load_builtin_evt("EVT2")
    for can_id in (0x11, 0x151, evt.broadcast.request_id, 0x700):
        assert not is_safe_standard_id(evt, can_id)
    assert is_safe_standard_id(evt, 0x6A5)
    assert safe_random_ids(evt)
    assert all(is_safe_standard_id(evt, item) for item in safe_random_ids(evt))
    with pytest.raises(ValueError, match="冲突"):
        build_profile(evt, "can4", DiagnosticProfile.QUICK, fixed_can_id=0x300)


def test_custom_bounds_match_interface_mode() -> None:
    evt = load_builtin_evt("EVT2")
    classic = build_profile(
        evt,
        "can5",
        DiagnosticProfile.CUSTOM,
        fixed_can_id=0x6A5,
        duration_s=3,
        payload_length=8,
    )
    assert classic[0].description.startswith("custom Classic")
    with pytest.raises(ValueError, match="0..8"):
        build_profile(
            evt,
            "can5",
            DiagnosticProfile.CUSTOM,
            fixed_can_id=0x6A5,
            payload_length=9,
        )


def test_commands_use_evt_timing_and_high_risk_gate() -> None:
    evt = load_builtin_evt("EVT2")
    requests = configure_requests(evt.interfaces["can5"])
    assert "fd" in requests[1].argv
    assert requests[1].argv[requests[1].argv.index("fd") + 1] == "off"
    fd_request = configure_requests(evt.interfaces["can4"])[1]
    assert fd_request.argv[fd_request.argv.index("dbitrate") + 1] == "5000000"
    with pytest.raises(PermissionError, match="默认禁用"):
        clear_dmesg_request()
    assert clear_dmesg_request(high_risk_confirmed=True).argv == (
        "sudo",
        "--",
        "dmesg",
        "-C",
    )
    password_request = configure_requests(evt.interfaces["can4"], "secret")[0]
    assert password_request.argv[:5] == ("sudo", "-S", "-p", "", "--")
    assert password_request.stdin_secret == "secret"
    tools = required_tools_request()
    assert tools.evidence_label == "required-tools"
    assert {"cangen", "candump", "python3"}.issubset(tools.argv)


def test_random_stage_command_contains_only_evt_derived_ids() -> None:
    evt = load_builtin_evt("EVT2")
    stage = DiagnosticStage("random", "random", 1, 0, None, None, True)
    request = stage_request(evt, evt.interfaces["can4"], stage)
    script = request.argv[-1]
    assert "python3" in script
    assert "candump -e -x -t A can4" in script
    assert "__D7_SECTION_BEGIN__" in script


def test_stage_section_parser() -> None:
    stdout = """__D7_SECTION_BEGIN__:ip_before
before
__D7_SECTION_END__:ip_before
__D7_SECTION_BEGIN__:candump
error
__D7_SECTION_END__:candump
__D7_GENERATOR_RC__:124
"""
    sections, rc = parse_stage_sections(stdout)
    assert sections == {"ip_before": "before", "candump": "error"}
    assert rc == 124


def test_link_parser_and_error_evaluation() -> None:
    before = parse_link_snapshot(
        "can <FD> state ERROR-ACTIVE\nberr-counter tx 0 rx 0\n"
        "re-started bus-errors arbit-lost error-warn error-pass bus-off\n0 0 0 0 0 0\n"
    )
    after = parse_link_snapshot(
        "can <FD> state ERROR-PASSIVE\nberr-counter tx 8 rx 1\n"
        "re-started bus-errors arbit-lost error-warn error-pass bus-off\n0 2 0 1 1 0\n"
    )
    errors = extract_error_lines("mttcan can4: ACK error\nmttcan can0: CRC error", "can4")
    evidence = StageEvidence(
        "can4",
        DiagnosticStage("custom", "custom", 1, 0, 0x6A5, 64),
        0,
        "",
        "",
        before,
        after,
        "",
        "",
        "",
        "",
        errors,
    )
    result = evaluate((evidence,))
    assert result.verdict == "FAIL"
    assert any("ACK error" in hint for hint in result.root_cause_hints)
    assert any("error_pass +1" in finding for finding in result.findings)


def test_node_and_broadcast_plans_use_evt_catalog() -> None:
    evt = load_builtin_evt("EVT2")
    plan = build_node_plan(evt, logic_ids=(7,), write_then_read=True)
    assert [item.operation for item in plan] == ["write", "read"]
    assert all(item.node.bus == "can0" for item in plan)
    assert DEFAULT_NODE_PARAMETER_BINARY == "/opt/actuator_sdk/jihua_calib_param_factory"
    assert DEFAULT_NODE_PARAMETER_CONFIG == "/opt/actuator_sdk/config/calibration_info.yaml"
    assert f"test -x {DEFAULT_NODE_PARAMETER_BINARY}" in plan[0].request.argv[-1]
    assert " -w 7" in plan[0].request.argv[-1]
    assert " -r 7" in plan[1].request.argv[-1]
    request = broadcast_request(evt, "can2", 3)
    assert request.argv[:2] == ("bash", "-c")
    assert request.argv[3:6] == ("--", "can2", "3")
    assert request.argv[-1] == "300##1404040080452"
    assert request.timeout_s == 15
    assert responding_node(evt, "can2", evt.broadcast.response_base_id + 0x51).logic_id == 23
    assert responding_node(evt, "can2", 0x251).logic_id == 23
    summary = summarize_responses(
        evt,
        "can2",
        (
            "(1.0) can2 151##1AABB",
            "(1.1) can2 251##1CCDD",
            "(1.2) can2 358##1EEFF",
        ),
    )
    assert set(summary["responses"]) == {23, 30}
    assert summary["responses"][23]["count"] == 2
    assert summary["responses"][30]["count"] == 1
    assert summary["response_frame_count"] == 3

    evt1 = load_builtin_evt("EVT1")
    assert {node.group: node.bus for node in evt1.nodes} == {
        "HEAD_TORSO": "can7",
        "LEFT_ARM": "can1",
        "RIGHT_ARM": "can0",
        "CHASSIS": "can5",
    }


def test_node_parameter_remote_paths_must_be_absolute() -> None:
    assert validate_remote_absolute_path(DEFAULT_NODE_PARAMETER_CONFIG, "参数配置") == (
        DEFAULT_NODE_PARAMETER_CONFIG
    )
    with pytest.raises(ValueError, match="远端绝对路径"):
        validate_remote_absolute_path("~/config/calibration_info.yaml", "参数配置")
    with pytest.raises(ValueError, match="非法字符"):
        validate_remote_absolute_path("/opt/tool\ncommand", "参数工具")


def test_bit_timing_and_tdc_are_read_only_calculations() -> None:
    nominal = best_candidate(bitrate=1_000_000, sample_point=0.8)
    data = best_candidate(bitrate=5_000_000, sample_point=0.7, data_phase=True)
    assert nominal.exact_bitrate and nominal.actual_sample_point == 0.8
    assert data.exact_bitrate and data.actual_sample_point == 0.7
    assert tdcr(data)["tdcr_hex"] == f"0x{data.tseg1 << 8:x}"
    with pytest.raises(ValueError):
        tdcr(replace(data), 128)
