from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from d7_factory_studio.core.evt import EvtConfig
from d7_factory_studio.core.ports import (
    CancellationToken,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteSession,
)

from .broadcast import (
    broadcast_request,
    live_response_event,
    parse_broadcast_frame,
    responding_node,
    summarize_responses,
)
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

ProgressCallback = Callable[[int, str], None]


def _artifact_component(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._") or "unknown"


def diagnostic_raw_artifacts(result: Mapping[str, object]) -> dict[str, str]:
    """Split structured diagnostic evidence into report-bundle raw files."""
    artifacts: dict[str, str] = {}
    stages = result.get("stages", [])
    if isinstance(stages, list):
        for index, item in enumerate(stages, 1):
            if not isinstance(item, dict):
                continue
            stage = item.get("stage", {})
            stage_name = stage.get("name", "stage") if isinstance(stage, dict) else "stage"
            prefix = (
                f"stages/{index:02d}-{_artifact_component(item.get('interface', 'can'))}-"
                f"{_artifact_component(stage_name)}"
            )
            for key in (
                "dmesg_before",
                "dmesg_after",
                "dmesg_new",
                "candump",
                "generator_stdout",
                "generator_stderr",
            ):
                artifacts[f"{prefix}/{key}.log"] = str(item.get(key, ""))
            for key in ("before", "after"):
                snapshot = item.get(key, {})
                raw = snapshot.get("raw", "") if isinstance(snapshot, dict) else ""
                artifacts[f"{prefix}/ip_{key}.log"] = str(raw)
    commands = result.get("commands", [])
    if isinstance(commands, list):
        for index, item in enumerate(commands, 1):
            if not isinstance(item, dict):
                continue
            prefix = f"commands/{index:02d}-{_artifact_component(item.get('label', 'command'))}"
            artifacts[f"{prefix}.stdout.log"] = str(item.get("stdout", ""))
            artifacts[f"{prefix}.stderr.log"] = str(item.get("stderr", ""))
    return artifacts


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
        on_progress: ProgressCallback | None = None,
        *,
        sudo_password: str | None = None,
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

        plans = {
            interface: build_profile(
                self.evt,
                interface,
                options.profile,
                fixed_can_id=options.fixed_can_id,
                duration_s=options.duration_s,
                gap_ms=options.gap_ms,
                payload_length=options.payload_length,
                random_frames=options.random_frames,
            )
            for interface in interfaces
        }
        total_stage_seconds = max(
            1,
            sum(stage.duration_s for stages in plans.values() for stage in stages),
        )
        planned_stage_count = sum(len(stages) for stages in plans.values())
        completed_stage_seconds = 0

        if options.clear_dmesg:
            self._progress(on_progress, 1, "正在清空内核日志")
            clear_result = self._execute_recorded(
                clear_dmesg_request(
                    high_risk_confirmed=True, sudo_password=sudo_password
                ),
                token,
                command_log,
                failures,
                on_output,
            )
            if clear_result.returncode != 0:
                self._progress(on_progress, 100, "dmesg -C 执行失败，链路测试已停止")
                return self._result(
                    interfaces,
                    options,
                    started,
                    evidence,
                    failures,
                    command_log,
                    planned_stage_count=planned_stage_count,
                )
        self._progress(on_progress, 2 if options.clear_dmesg else 1, "正在检查 Orin 诊断环境")
        for request in environment_requests():
            self._execute_recorded(request, token, command_log, failures, on_output)
        if any(item["label"] == "required-tools" and item["returncode"] for item in command_log):
            self._progress(on_progress, 100, "诊断环境缺少必要工具")
            return self._result(
                interfaces,
                options,
                started,
                evidence,
                failures,
                command_log,
                planned_stage_count=planned_stage_count,
            )
        if options.setup_can_script and not options.setup_each_stage:
            self._progress(on_progress, 3, "正在执行 setup_can.sh")
            setup_result = self._run_setup(
                options.setup_can_script,
                token,
                command_log,
                failures,
                on_output,
                sudo_password,
            )
            if setup_result.returncode != 0:
                failures.append("setup_can 失败，已停止链路测试")
                self._progress(on_progress, 100, "setup_can.sh 执行失败，链路测试已停止")
                return self._result(
                    interfaces,
                    options,
                    started,
                    evidence,
                    failures,
                    command_log,
                    planned_stage_count=planned_stage_count,
                )
        for interface in interfaces:
            config = self.evt.interfaces[interface]
            self._progress(
                on_progress,
                5 + round(90 * completed_stage_seconds / total_stage_seconds),
                f"{interface.upper()}：正在准备接口",
            )
            if options.configure_interface and not options.setup_each_stage:
                configuration_failed = False
                for request in configure_requests(config, sudo_password):
                    result = self._execute_recorded(
                        request, token, command_log, failures, on_output
                    )
                    configuration_failed = configuration_failed or result.returncode != 0
                if configuration_failed:
                    failures.append(f"{interface}: CAN 接口配置失败，已跳过流量测试")
                    completed_stage_seconds += sum(stage.duration_s for stage in plans[interface])
                    continue
            for stage_index, stage in enumerate(plans[interface]):
                token.raise_if_cancelled()
                if options.setup_can_script and options.setup_each_stage:
                    setup_result = self._run_setup(
                        options.setup_can_script,
                        token,
                        command_log,
                        failures,
                        on_output,
                        sudo_password,
                    )
                    if setup_result.returncode != 0:
                        failures.append(
                            f"{interface}/{stage.name}: setup_can 失败，已跳过剩余阶段"
                        )
                        completed_stage_seconds += sum(
                            item.duration_s for item in plans[interface][stage_index:]
                        )
                        break
                    if options.configure_interface:
                        configuration_failed = False
                        for request in configure_requests(config, sudo_password):
                            config_result = self._execute_recorded(
                                request, token, command_log, failures, on_output
                            )
                            configuration_failed = (
                                configuration_failed or config_result.returncode != 0
                            )
                        if configuration_failed:
                            failures.append(
                                f"{interface}/{stage.name}: setup_can 后 CAN 接口配置失败，"
                                "已跳过剩余阶段"
                            )
                            completed_stage_seconds += sum(
                                item.duration_s for item in plans[interface][stage_index:]
                            )
                            break
                request = stage_request(self.evt, config, stage)
                result = self._execute_stage(
                    request,
                    token,
                    on_output,
                    on_progress,
                    interface,
                    stage.name,
                    completed_stage_seconds,
                    stage.duration_s,
                    total_stage_seconds,
                )
                command_log.append(self._command_dict(stage.name, result, request.argv))
                sections, generator_rc = parse_stage_sections(result.stdout)
                if result.returncode != 0:
                    failures.append(
                        f"{interface}/{stage.name}: 远程阶段命令失败 (RC={result.returncode})"
                    )
                required_sections = {
                    "ip_before",
                    "ip_after",
                    "dmesg_before",
                    "dmesg_after",
                    "candump",
                    "generator_out",
                    "generator_err",
                }
                missing_sections = sorted(required_sections - sections.keys())
                if missing_sections:
                    failures.append(
                        f"{interface}/{stage.name}: 未收到完整阶段证据 "
                        f"({', '.join(missing_sections)})"
                    )
                before_dmesg, after_dmesg = sections.get("dmesg_before", ""), sections.get("dmesg_after", "")
                new_dmesg = dmesg_suffix(before_dmesg, after_dmesg)
                candump = sections.get("candump", "")
                errors = extract_error_lines(new_dmesg, interface) + extract_error_lines(candump, interface)
                self._emit_stage_summary(
                    on_output,
                    interface,
                    stage.name,
                    generator_rc,
                    candump,
                    errors,
                    result,
                    missing_sections,
                )
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
                completed_stage_seconds += stage.duration_s
        result = self._result(
            interfaces,
            options,
            started,
            evidence,
            failures,
            command_log,
            planned_stage_count=planned_stage_count,
        )
        verdict = str(result["evaluation"]["verdict"])
        self._progress(on_progress, 100, f"链路测试完成：{verdict}")
        return result

    def _result(
        self,
        interfaces,
        options: DiagnosticOptions,
        started: str,
        evidence: list[StageEvidence],
        failures: list[str],
        command_log: list[dict[str, object]],
        *,
        planned_stage_count: int,
    ) -> dict[str, object]:
        assessment = evaluate(tuple(evidence), tuple(failures))
        executed_stage_count = len(evidence)
        if executed_stage_count == planned_stage_count:
            execution_status = "complete"
        elif executed_stage_count:
            execution_status = "partial"
        else:
            execution_status = "not_started"
        return {
            "schema_version": 1,
            "kind": "can_diagnostic",
            "evt": {"variant": self.evt.variant, "robot_model": self.evt.robot_model},
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "interfaces": list(interfaces),
            "options": asdict(options),
            "evaluation": assessment.to_dict(),
            "execution": {
                "status": execution_status,
                "planned_stage_count": planned_stage_count,
                "executed_stage_count": executed_stage_count,
            },
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
        line_buffer = ""
        live_counts: dict[int, int] = {}

        def emit_line(line: str) -> None:
            if on_output is None:
                return
            parsed = parse_broadcast_frame(line)
            if parsed is None:
                if line:
                    on_output(line + "\n", False)
                return
            if parsed[0] != interface:
                return
            node = responding_node(self.evt, interface, parsed[1])
            if node is None:
                return
            count = live_counts.get(node.logic_id, 0) + 1
            live_counts[node.logic_id] = count
            on_output(live_response_event(interface, node.logic_id, count, parsed[1]), False)

        def emit_filtered(chunk: str, is_error: bool) -> None:
            nonlocal line_buffer
            if on_output is None:
                return
            if is_error:
                on_output(chunk, True)
                return
            line_buffer += chunk.replace("\r", "")
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                emit_line(line)

        result = self.session.execute(request, token, emit_filtered if on_output else None)
        if line_buffer:
            emit_line(line_buffer)
        summary = summarize_responses(self.evt, interface, tuple(result.stdout.splitlines()))
        send_errors = 0
        for line in result.stdout.splitlines():
            if line.startswith("__SEND_ERROR_COUNT__:"):
                send_errors = int(line.rsplit(":", 1)[-1] or 0)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"RC={result.returncode}"
            raise RuntimeError(f"{interface} 广播命令执行失败: {detail}")
        if send_errors:
            first_error = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in result.stdout.splitlines()
                    if line.startswith("__SEND_ERROR__:")
                ),
                "",
            )
            detail = f"：{first_error}" if first_error else ""
            raise RuntimeError(f"{interface} 广播发送失败 {send_errors} 次{detail}")
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

    def prepare_motor_can(
        self,
        token: CancellationToken,
        *,
        on_output: Callable[[str, bool], None] | None = None,
        sudo_password: str | None = None,
    ) -> RemoteCommandResult:
        result = self._run_setup(
            "~/setup_can.sh",
            token,
            [],
            [],
            on_output,
            sudo_password,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"RC={result.returncode}"
            raise RuntimeError(f"setup_can.sh 执行失败: {detail}")
        return result

    def _run_setup(
        self,
        script: str,
        token: CancellationToken,
        command_log,
        failures,
        on_output,
        sudo_password: str | None,
    ) -> RemoteCommandResult:
        if not script.startswith("/") and not script.startswith("~/"):
            raise ValueError("setup_can 脚本必须是绝对路径或 ~/ 路径")
        if any(character in script for character in ("\n", "\r", "\0")):
            raise ValueError("setup_can 脚本路径包含非法字符")
        from pathlib import PurePosixPath

        sudo = 'sudo -S -p "" --' if sudo_password is not None else "sudo --"
        if script.startswith("~/"):
            relative = PurePosixPath(script[2:])
            if any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("setup_can 用户目录路径无效")
            parent = "" if str(relative.parent) == "." else str(relative.parent)
            argv = (
                "sh",
                "-lc",
                f'cd -- "$HOME/$1" && {sudo} "./$2"',
                "d7-setup",
                parent,
                relative.name,
            )
        else:
            absolute = PurePosixPath(script)
            argv = (
                "sh",
                "-lc",
                f'cd -- "$1" && {sudo} "./$2"',
                "d7-setup",
                str(absolute.parent),
                absolute.name,
            )
        return self._execute_recorded(
            RemoteCommandRequest(
                argv,
                timeout_s=120,
                stream_output=True,
                stdin_secret=sudo_password,
                evidence_label="setup-can",
            ),
            token,
            command_log,
            failures,
            on_output,
        )

    def _execute_recorded(
        self, request, token, command_log, failures, on_output
    ) -> RemoteCommandResult:
        if on_output is not None:
            on_output(f"执行 {request.evidence_label}\n", False)
        result = self.session.execute(request, token, on_output)
        command_log.append(self._command_dict(request.evidence_label, result, request.argv))
        if result.returncode:
            failures.append(f"命令 {request.evidence_label} 失败 (RC={result.returncode})")
        if on_output is not None:
            on_output(
                f"{request.evidence_label} 结束：RC={result.returncode}，"
                f"耗时 {result.duration_s:.1f}s\n",
                result.returncode != 0,
            )
        return result

    def _execute_stage(
        self,
        request,
        token,
        on_output,
        on_progress,
        interface: str,
        stage_name: str,
        completed_seconds: int,
        stage_seconds: int,
        total_seconds: int,
    ) -> RemoteCommandResult:
        finished = threading.Event()

        def heartbeat() -> None:
            started = time.monotonic()
            while not finished.wait(1.0):
                elapsed = min(stage_seconds, int(time.monotonic() - started))
                percent = 5 + round(
                    90 * (completed_seconds + elapsed) / max(1, total_seconds)
                )
                self._progress(
                    on_progress,
                    percent,
                    f"{interface.upper()} · {stage_name} · {elapsed}/{stage_seconds}s",
                )

        self._progress(
            on_progress,
            5 + round(90 * completed_seconds / max(1, total_seconds)),
            f"{interface.upper()} · {stage_name} 开始",
        )
        thread = threading.Thread(target=heartbeat, name="d7-diagnostic-progress", daemon=True)
        thread.start()
        try:
            # The command stdout contains complete candump evidence. Keep it in
            # the result/report, but never stream raw frames into the UI.
            return self.session.execute(request, token, None)
        finally:
            finished.set()
            thread.join(timeout=0.2)

    @staticmethod
    def _emit_stage_summary(
        callback: Callable[[str, bool], None] | None,
        interface: str,
        stage_name: str,
        generator_rc: int,
        candump: str,
        errors: tuple[str, ...],
        result: RemoteCommandResult,
        missing_sections: list[str],
    ) -> None:
        if callback is None:
            return
        frame_count = sum(1 for line in candump.splitlines() if line.strip())
        failed = bool(result.returncode or generator_rc not in {0, 124} or errors or missing_sections)
        callback(
            f"{interface.upper()} · {stage_name} 完成："
            f"生成器 RC={generator_rc}，采集 {frame_count} 帧，错误 {len(errors)} 条",
            failed,
        )
        if result.returncode:
            callback(f"{interface.upper()} · {stage_name} 远端命令失败 (RC={result.returncode})", True)
        if missing_sections:
            callback(
                f"{interface.upper()} · {stage_name} 缺少证据段：{', '.join(missing_sections)}",
                True,
            )
        for line in dict.fromkeys(errors):
            callback(f"{interface.upper()} · {stage_name} · {line}", True)

    @staticmethod
    def _progress(callback: ProgressCallback | None, value: int, message: str) -> None:
        if callback is not None:
            callback(max(0, min(100, int(value))), message)

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
