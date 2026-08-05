from __future__ import annotations

import json
from pathlib import Path

import pytest

from d7_factory_studio.core.ports import CancellationToken, RemoteCommandResult, RemoteFileInfo, RemoteSession
from d7_factory_studio.features.device_logs import DeviceLogService, LogTimeFilter


class LogSession(RemoteSession):
    def __init__(self) -> None:
        self.entries = [
            RemoteFileInfo("/sdcard/pudu/log/old.log", "old.log", "old.log", 3, 100),
            RemoteFileInfo("/sdcard/pudu/log/new.log", "new.log", "new.log", 4, 200),
            RemoteFileInfo("/sdcard/pudu/log/link", "link", "link", 0, 200, is_symlink=True),
        ]
        self.download_calls = 0

    def connect(self): ...
    def close(self): ...

    def execute(self, request, token, on_output=None):
        return RemoteCommandResult(0, "", "", 0)

    def list_files(self, remote_root):
        return list(self.entries)

    def download_files(self, entries, destination, token):
        self.download_calls += 1
        return [
            {
                "remote_path": entry.remote_path,
                "relative_path": entry.relative_path,
                "status": "downloaded" if entry.name == "new.log" else "failed",
                "sha256": "a" * 64 if entry.name == "new.log" else None,
                "error": None if entry.name == "new.log" else "simulated",
            }
            for entry in entries
        ]


def test_browse_filters_time_inclusively_and_hides_symlinks() -> None:
    service = DeviceLogService(LogSession())
    assert [item.name for item in service.browse(LogTimeFilter(200, 200))] == ["new.log"]
    with pytest.raises(ValueError, match="晚于"):
        LogTimeFilter(201, 200)


def test_download_writes_partial_success_manifest(tmp_path: Path) -> None:
    session = LogSession()
    service = DeviceLogService(session)
    selected = tuple(item for item in service.browse() if not item.is_symlink)
    result = service.download_selected(selected, tmp_path, CancellationToken())
    assert result["downloaded_count"] == 1
    assert result["failed_count"] == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["remote_root"] == "/sdcard/pudu/log"
    assert {item["status"] for item in manifest["files"]} == {"downloaded", "failed"}


def test_changed_selection_is_not_downloaded(tmp_path: Path) -> None:
    session = LogSession()
    service = DeviceLogService(session)
    selected = session.entries[1]
    session.entries[1] = RemoteFileInfo(selected.remote_path, selected.relative_path, selected.name, 99, 201)
    result = service.download_selected((selected,), tmp_path, CancellationToken())
    assert result["failed_count"] == 1
    assert session.download_calls == 0


def test_service_rejects_entry_outside_fixed_root(tmp_path: Path) -> None:
    service = DeviceLogService(LogSession())
    outside = RemoteFileInfo("/sdcard/pudu/other.log", "../other.log", "other.log", 1, 1)
    with pytest.raises(ValueError, match="越界"):
        service.download_selected((outside,), tmp_path, CancellationToken())
