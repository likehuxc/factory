from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from d7_factory_studio.core.evt import EvtConfig
from d7_factory_studio.core.ports import CancellationToken, RemoteSession

from .broadcast import broadcast_request, summarize_responses
from .commands import (
    clear_dmesg_request,
    configure_requests,
    environment_requests,
    parse_stage_sections,
    probe_request,
    stage_request,
)
from .models import DiagnosticProfile, StageEvidence
from .node_params import build_node_plan
from .parsers import dmesg_suffix, evaluate, extract_error_lines, parse_can_interfaces, parse_link_snapshot
from .profiles import build_profile


@dataclass(frozen=True, slots=True)
class DiagnosticOptions:
    profile: DiagnosticProfile
    fixed_can_id: int
    duration_s: int = 60
    gap_ms: int = 1
    payload_length: int | None = None
    random_frames: bool = False
    configure_interface: bool = True
    setup_can_script: str | None = None
    setup_each_stage: bool = False
    clear_dmesg: bool = False
    high_risk_dmesg_clear_confirmed: bool = False


class DiagnosticService:
    def __init__(self, evt: EvtConfig, session: RemoteSession) -> None:
        self.evt = evt
        self.session = session

    def probe_interfaces(self, token: CancellationToken) -> tuple[str, ...]:
        result = self.session.execute(probe_request(), token)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "SocketCAN 接口探测失败")
        detected = parse_can_interfaces(result.stdout)
        return tuple(interface for interface in detected if interface in self.evt.interfaces)

    def run(
        self,
        interfaces: tuple[str, ...],
        options: DiagnosticOptions,
        token: CancellationToken,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> dict[str, object]:
        unknown = set(interfaces) - set(self.evt.interfaces)
        if unknown:
            raise ValueError(f"接口不属于当前 {self.evt.variant}: {sorted(unknown)}")
        if options.clear_dmesg and not options.high_risk_dmesg_clear_confirmed:
            raise PermissionError("dmesg -C 必须显式确认高风险参数")
        started = datetime.now(UTC).isoformat()
        command_log: list[dict[str, object]] = []
        failures: list[str] = []
        evidence: list[StageEvidence] = []

        for request in environment_requests():
            self._execute_recorded(request, token, command_log, failures, on_output)
        if options.clear_dmesg:
            from d7_factory_studio.core.ports import RemoteCommandRequest

            self._execute_recorded(
                RemoteCommandRequest(
                    ("sudo", "dmesg", "--time-format", "iso"),
                    timeout_s=30,
                    evidence_label="dmesg-before-high-risk-clear",
                ),
                token,
                command_log,
                failures,
                on_output,
            )
            self._execute_recorded(
                clear_dmesg_request(high_risk_confirmed=True), token, command_log, failures, on_output
            )
        for interface in interfaces:
            config = self.evt.interfaces[interface]
            if options.setup_can_script and not options.setup_each_stage:
                self._run_setup(options.setup_can_script, token, command_log, failures, on_output)
            if options.configure_interface:
                for request in configure_requests(config):
                    self._execute_recorded(request, token, command_log, failures, on_output)
            stages = build_profile(
                self.evt,
                interface,
                options.profile,
                fixed_can_id=options.fixed_can_id,
                duration_s=options.duration_s,
                gap_ms=options.gap_ms,
                payload_length=options.payload_length,
                random_frames=options.random_frames,
            )
            for stage in stages:
                token.raise_if_cancelled()
                if options.setup_can_script and options.setup_each_stage:
                    self._run_setup(options.setup_can_script, token, command_log, failures, on_output)
                request = stage_request(self.evt, config, stage)
                result = self.session.execute(request, token, on_output)
                command_log.append(self._command_dict(stage.name, result, request.argv))
                sections, generator_rc = parse_stage_sections(result.stdout)
                before_dmesg, after_dmesg = sections.get("dmesg_before", ""), sections.get("dmesg_after", "")
                new_dmesg = dmesg_suffix(before_dmesg, after_dmesg)
                candump = sections.get("candump", "")
                errors = extract_error_lines(new_dmesg, interface) + extract_error_lines(candump, interface)
                evidence.append(
                    StageEvidence(
                        interface=interface,
                        stage=stage,
                        generator_returncode=generator_rc,
                        generator_stdout=sections.get("generator_out", ""),
                        generator_stderr=sections.get("generator_err", ""),
                        before=parse_link_snapshot(sections.get("ip_before", "")),
                        after=parse_link_snapshot(sections.get("ip_after", "")),
                        dmesg_before=before_dmesg,
                        dmesg_after=after_dmesg,
                        dmesg_new=new_dmesg,
                        candump=candump,
                        error_lines=errors,
                        timed_out=result.timed_out,
                        cancelled=result.cancelled,
                    )
                )
        assessment = evaluate(tuple(evidence), tuple(failures))
        return {
            "schema_version": 1,
            "kind": "can_diagnostic",
            "evt": {"variant": self.evt.variant, "robot_model": self.evt.robot_model},
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "interfaces": list(interfaces),
            "options": asdict(options),
            "evaluation": assessment.to_dict(),
            "stages": [item.to_dict() for item in evidence],
            "commands": command_log,
        }

    def run_node_parameters(
        self,
        binary_path: str,
        token: CancellationToken,
        *,
        buses: tuple[str, ...] = (),
        logic_ids: tuple[int, ...] = (),
        write_then_read: bool = False,
        timeout_s: int = 40,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> dict[str, object]:
        plan = build_node_plan(
            self.evt,
            binary_path,
            buses=buses,
            logic_ids=logic_ids,
            write_then_read=write_then_read,
            timeout_s=timeout_s,
        )
        nodes: dict[int, dict[str, object]] = {}
        for item in plan:
            token.raise_if_cancelled()
            result = self.session.execute(item.request, token, on_output)
            node_result = nodes.setdefault(
                item.node.logic_id,
                {
                    "logic_id": item.node.logic_id,
                    "dev_id": item.node.dev_id,
                    "name": item.node.name,
                    "label": item.node.label,
                    "bus": item.node.bus,
                    "operations": {},
                },
            )
            node_result["operations"][item.operation] = {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
            }
        required = ("write", "read") if write_then_read else ("read",)
        for node_result in nodes.values():
            operations = node_result["operations"]
            node_result["verdict"] = (
                "PASS"
                if all(
                    operation in operations and operations[operation]["returncode"] == 0
                    for operation in required
                )
                else "FAIL"
            )
        return {
            "schema_version": 1,
            "kind": "node_parameters",
            "evt": {"variant": self.evt.variant},
            "write_then_read": write_then_read,
            "nodes": list(nodes.values()),
            "verdict": "PASS"
            if nodes and all(item["verdict"] == "PASS" for item in nodes.values())
            else "FAIL",
        }

    def run_broadcast(
        self,
        interface: str,
        duration_s: int,
        token: CancellationToken,
        *,
        payload_hex: str = "404040080452",
        on_output: Callable[[str, bool], None] | None = None,
    ) -> dict[str, object]:
        request = broadcast_request(self.evt, interface, duration_s, payload_hex)
        result = self.session.execute(request, token, on_output)
        summary = summarize_responses(self.evt, interface, tuple(result.stdout.splitlines()))
        send_errors = 0
        for line in result.stdout.splitlines():
            if line.startswith("__SEND_ERROR_COUNT__:"):
                send_errors = int(line.rsplit(":", 1)[-1] or 0)
        return {
            "schema_version": 1,
            "kind": "broadcast",
            "evt": {"variant": self.evt.variant},
            **summary,
            "send_error_count": send_errors,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "cancelled": result.cancelled,
        }

    def _run_setup(self, script: str, token: CancellationToken, command_log, failures, on_output) -> None:
        if not script.startswith("/") and not script.startswith("~/"):
            raise ValueError("setup_can 脚本必须是绝对路径或 ~/ 路径")
        if any(character in script for character in ("\n", "\r", "\0")):
            raise ValueError("setup_can 脚本路径包含非法字符")
        from d7_factory_studio.core.ports import RemoteCommandRequest

        argv = ("sudo", script)
        if script.startswith("~/"):
            from pathlib import PurePosixPath

            relative = PurePosixPath(script[2:])
            if any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("setup_can 用户目录路径无效")
            argv = ("sh", "-lc", 'sudo -- "$HOME/$1"', "d7-setup", str(relative))
        self._execute_recorded(
            RemoteCommandRequest(argv, timeout_s=120, stream_output=True, evidence_label="setup-can"),
            token,
            command_log,
            failures,
            on_output,
        )

    def _execute_recorded(self, request, token, command_log, failures, on_output) -> None:
        result = self.session.execute(request, token, on_output)
        command_log.append(self._command_dict(request.evidence_label, result, request.argv))
        if result.returncode:
            failures.append(f"命令 {request.evidence_label} 失败 (RC={result.returncode})")

    @staticmethod
    def _command_dict(label, result, argv=()) -> dict[str, object]:
        return {
            "label": label,
            "argv": list(argv),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_s": result.duration_s,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
        }
