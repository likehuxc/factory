from __future__ import annotations

import re
import shlex

from d7_factory_studio.core.evt import EvtConfig, MotorNodeConfig
from d7_factory_studio.core.ports import RemoteCommandRequest

FRAME_RE = re.compile(
    r"\b(can\d+)\s+([0-9A-Fa-f]{3,8})(?:(?:##[0-9A-Fa-f]|#)[0-9A-Fa-f]*|\s+\[\s*\d+\s*\](?:\s+[0-9A-Fa-f]{2})*)"
)
DEFAULT_QUERY_PAYLOAD = "404040080452"


def broadcast_request(
    evt: EvtConfig, interface: str, duration_s: int, payload_hex: str = DEFAULT_QUERY_PAYLOAD
) -> RemoteCommandRequest:
    if interface not in evt.interfaces:
        raise ValueError(f"EVT 配置中不存在接口 {interface}")
    if not evt.nodes_for_bus(interface):
        raise ValueError(f"接口 {interface} 没有 EVT 节点")
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}){0,64}", payload_hex):
        raise ValueError("广播数据必须是偶数字节十六进制")
    duration = max(1, min(300, int(duration_s)))
    frame = f"{evt.broadcast.request_id:03X}##1{payload_hex.upper()}"
    script = """
set +e
iface="$1"; duration="$2"; frame="$3"; err_file="$(mktemp)"; dump_pid=""
cleanup(){
  if [ -n "$dump_pid" ]; then
    kill "$dump_pid" 2>/dev/null; wait "$dump_pid" 2>/dev/null
  fi
  rm -f "$err_file"
}
trap cleanup EXIT INT TERM
candump -L "$iface" & dump_pid=$!; sleep 0.2
end=$((SECONDS + duration))
while [ "$SECONDS" -lt "$end" ]; do cansend "$iface" "$frame" 2>>"$err_file"; sleep 0.01; done
sleep 0.5; kill "$dump_pid" 2>/dev/null; wait "$dump_pid" 2>/dev/null; dump_pid=""
echo "__SEND_ERROR_COUNT__:$(wc -l < "$err_file" | tr -d ' ')"
""".strip()
    command = (
        f"sh -c {shlex.quote(script)} d7-broadcast {shlex.quote(interface)} {duration} {shlex.quote(frame)}"
    )
    return RemoteCommandRequest(
        ("sh", "-lc", command),
        timeout_s=duration + 12,
        stream_output=True,
        evidence_label=f"broadcast-{interface}",
    )


def parse_broadcast_frame(line: str) -> tuple[str, int] | None:
    match = FRAME_RE.search(line)
    return (match.group(1), int(match.group(2), 16)) if match else None


def responding_node(evt: EvtConfig, interface: str, frame_id: int) -> MotorNodeConfig | None:
    if frame_id == evt.broadcast.request_id:
        return None
    return next(
        (
            node
            for node in evt.nodes_for_bus(interface)
            if frame_id == (evt.broadcast.response_base_id + node.dev_id) & 0x7FF
        ),
        None,
    )


def summarize_responses(evt: EvtConfig, interface: str, lines: tuple[str, ...]) -> dict[str, object]:
    responses: dict[int, dict[str, object]] = {}
    for line in lines:
        parsed = parse_broadcast_frame(line)
        if not parsed or parsed[0] != interface:
            continue
        node = responding_node(evt, interface, parsed[1])
        if node is None:
            continue
        item = responses.setdefault(node.logic_id, {"count": 0, "frame_id": f"0x{parsed[1]:03X}"})
        item["count"] = int(item["count"]) + 1
        item["last_frame"] = line
    missing = [node.logic_id for node in evt.nodes_for_bus(interface) if node.logic_id not in responses]
    return {
        "interface": interface,
        "responses": responses,
        "missing_logic_ids": missing,
        "response_frame_count": sum(int(item["count"]) for item in responses.values()),
    }
