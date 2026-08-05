# The self-contained report deliberately keeps its embedded HTML/CSS literal intact.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

SECRET_PATTERNS = (
    re.compile(r"(?i)(password|passwd|token|secret|authorization)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(sshpass\s+-p\s+)(\S+)"),
)


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    result = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "***REDACTED***")
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (
                match.group(1) + (match.group(2) if match.lastindex == 3 else "") + "***REDACTED***"
            ),
            result,
        )
    return result


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, secrets)
            for key, item in value.items()
            if str(key).lower() not in {"stdin_secret", "password"}
        }
    if isinstance(value, tuple | list):
        return [_redact(item, secrets) for item in value]
    return value


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    return _redact(value, tuple(secrets))


def _safe_artifact_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"原始证据路径无效: {name}")
    return path


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def render_html(result: dict[str, Any], manifest: dict[str, Any]) -> str:
    evaluation = result.get("evaluation", {}) if isinstance(result.get("evaluation"), dict) else {}
    verdict = str(evaluation.get("verdict", result.get("verdict", "UNKNOWN"))).upper()
    verdict_class = verdict.lower() if verdict in {"PASS", "WARN", "FAIL"} else "unknown"
    findings = evaluation.get("findings", []) or ["没有提供结论摘要"]
    facts = {
        "报告时间": manifest.get("created_at", ""),
        "EVT": result.get("evt", {}).get("variant", "") if isinstance(result.get("evt"), dict) else "",
        "接口": ", ".join(result.get("interfaces", [])) if isinstance(result.get("interfaces"), list) else "",
        "类型": result.get("kind", "diagnostic"),
    }
    facts_html = "".join(
        f"<div><span>{html.escape(str(key))}</span><strong>{html.escape(str(value))}</strong></div>"
        for key, value in facts.items()
    )
    findings_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in findings)
    stages = result.get("stages", []) if isinstance(result.get("stages"), list) else []
    rows: list[str] = []
    for item in stages:
        if not isinstance(item, dict):
            continue
        stage = item.get("stage", {}) if isinstance(item.get("stage"), dict) else {}
        after = item.get("after", {}) if isinstance(item.get("after"), dict) else {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('interface', '')))}</td>"
            f"<td>{html.escape(str(stage.get('name', '')))}</td>"
            f"<td>{html.escape(str(stage.get('duration_s', '')))}</td>"
            f"<td>{html.escape(str(item.get('generator_returncode', '')))}</td>"
            f"<td>{html.escape(str(after.get('state', 'UNKNOWN')))}</td>"
            f"<td>{len(item.get('error_lines', []))}</td>"
            "</tr>"
        )
    stage_html = (
        "<table><thead><tr><th>接口</th><th>阶段</th><th>计划(s)</th><th>RC</th><th>结束状态</th><th>错误行</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else "<p>无阶段记录。</p>"
    )
    files = list(manifest.get("artifacts", []))
    source_manifest = manifest.get("source_manifest", {})
    if isinstance(source_manifest, dict) and isinstance(source_manifest.get("files"), list):
        files.extend(source_manifest["files"])
    file_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('path') or item.get('relative_path') or item.get('remote_path', '')))}</td>"
        f"<td>{html.escape(str(item.get('size_bytes', '')))}</td>"
        f"<td><code>{html.escape(str(item.get('sha256', '')))}</code></td>"
        "</tr>"
        for item in files
        if isinstance(item, dict)
    )
    details = html.escape(_json_text(result))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>D7 Factory Studio 诊断报告</title><style>
:root{{--ink:#172033;--muted:#64748b;--line:#dbe3ed;--paper:#f3f6fa;--panel:#fff;--pass:#08785e;--warn:#a86108;--fail:#ba3948}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}}
header{{padding:26px 32px;color:#fff;background:#172033;border-bottom:5px solid #245fbd}}header div{{max-width:1120px;margin:auto;display:flex;justify-content:space-between;align-items:center}}
h1{{margin:0;font-size:25px}}.verdict{{padding:8px 18px;border-radius:5px;font:bold 20px Consolas;background:#536174}}.verdict.pass{{background:var(--pass)}}.verdict.warn{{background:var(--warn)}}.verdict.fail{{background:var(--fail)}}
main{{width:min(1120px,calc(100% - 28px));margin:24px auto 48px}}section{{margin-top:16px;padding:20px 22px;background:var(--panel);border:1px solid var(--line);border-radius:8px}}h2{{margin:0 0 14px;font-size:17px}}
.facts{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;padding:0;overflow:hidden;background:var(--line)}}.facts div{{padding:15px;background:#fff}}.facts span{{display:block;color:var(--muted);font-size:12px}}.facts strong{{overflow-wrap:anywhere}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--muted);background:#f8fafc;font-size:12px}}code,pre{{font-family:Consolas,monospace}}pre{{max-height:620px;overflow:auto;padding:14px;color:#dbe7f5;background:#111827;border-radius:6px;white-space:pre-wrap}}
@media(max-width:700px){{.facts{{grid-template-columns:1fr 1fr}}section{{overflow-x:auto}}}}@media print{{body{{background:#fff}}main{{width:100%;margin:0}}section{{break-inside:avoid}}}}
</style></head><body><header><div><h1>D7 远程诊断与设备日志报告</h1><span class="verdict {verdict_class}">{html.escape(verdict)}</span></div></header><main>
<section class="facts">{facts_html}</section><section><h2>结论摘要</h2><ul>{findings_html}</ul></section>
<section><h2>测试阶段</h2>{stage_html}</section><section><h2>证据清单</h2><table><thead><tr><th>路径</th><th>大小</th><th>SHA-256</th></tr></thead><tbody>{file_rows}</tbody></table></section>
<section><h2>结构化详情</h2><pre>{details}</pre></section></main></body></html>"""


class ReportBundleWriter:
    def write(
        self,
        destination: Path,
        result: Mapping[str, Any],
        *,
        raw_artifacts: Mapping[str, str | bytes] | None = None,
        source_manifest: Mapping[str, Any] | None = None,
        secrets: Iterable[str] = (),
    ) -> dict[str, object]:
        destination.mkdir(parents=True, exist_ok=True)
        raw_dir = destination / "raw"
        raw_dir.mkdir(exist_ok=True)
        safe_result = redact(dict(result), secrets)
        artifacts: list[dict[str, object]] = []
        for name, content in (raw_artifacts or {}).items():
            relative = _safe_artifact_name(name)
            path = raw_dir.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = redact_text(content, secrets).encode("utf-8") if isinstance(content, str) else content
            path.write_bytes(payload)
            artifacts.append(
                {
                    "path": f"raw/{relative.as_posix()}",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest = {
            "schema_version": 1,
            "kind": "d7_factory_report_bundle",
            "created_at": datetime.now(UTC).isoformat(),
            "artifacts": artifacts,
            "source_manifest": redact(dict(source_manifest or {}), secrets),
        }
        result_path, manifest_path, html_path = (
            destination / "result.json",
            destination / "manifest.json",
            destination / "report.html",
        )
        result_path.write_text(_json_text(safe_result), encoding="utf-8")
        manifest_path.write_text(_json_text(manifest), encoding="utf-8")
        html_path.write_text(render_html(safe_result, manifest), encoding="utf-8")
        return {
            "report_html": str(html_path),
            "result_json": str(result_path),
            "manifest_json": str(manifest_path),
            "raw_dir": str(raw_dir),
        }
