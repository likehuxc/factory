from __future__ import annotations

import json
from pathlib import Path

import pytest

from d7_factory_studio.reports import ReportBundleWriter, redact


def test_report_bundle_is_self_contained_hashed_and_redacted(tmp_path: Path) -> None:
    secret = "supersafe"
    result = {
        "kind": "can_diagnostic",
        "evt": {"variant": "EVT2"},
        "interfaces": ["can4"],
        "password": secret,
        "evaluation": {"verdict": "FAIL", "findings": ["host<script>", f"password={secret}"]},
        "commands": [{"argv": ["tool", f"token={secret}"], "stdin_secret": secret}],
        "stages": [],
    }
    paths = ReportBundleWriter().write(
        tmp_path,
        result,
        raw_artifacts={"dmesg/raw.log": f"password={secret}\nCAN error"},
        secrets=(secret,),
    )
    assert set(Path(path).name for key, path in paths.items() if key != "raw_dir") == {
        "report.html",
        "result.json",
        "manifest.json",
    }
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    combined = html + (tmp_path / "result.json").read_text() + (tmp_path / "raw/dmesg/raw.log").read_text()
    assert secret not in combined
    assert "host&lt;script&gt;" in html
    assert "<script src=" not in html and "http://" not in html and "https://" not in html
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["sha256"]
    assert manifest["artifacts"][0]["path"] == "raw/dmesg/raw.log"


def test_report_rejects_raw_artifact_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="路径无效"):
        ReportBundleWriter().write(tmp_path, {}, raw_artifacts={"../escape": "bad"})


def test_recursive_redaction_drops_secret_fields() -> None:
    value = redact({"password": "x", "nested": ["token=abc", {"stdin_secret": "abc"}]}, ("abc",))
    assert "password" not in value
    assert value["nested"] == ["token=***REDACTED***", {}]
