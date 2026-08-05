from __future__ import annotations

import json
import posixpath
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from d7_factory_studio.core.ports import CancellationToken, RemoteFileInfo, RemoteSession

DEVICE_LOG_ROOT = "/sdcard/pudu/log"


@dataclass(frozen=True, slots=True)
class LogTimeFilter:
    modified_from: int | None = None
    modified_to: int | None = None

    def __post_init__(self) -> None:
        if (
            self.modified_from is not None
            and self.modified_to is not None
            and self.modified_from > self.modified_to
        ):
            raise ValueError("日志起始时间不能晚于结束时间")

    def matches(self, entry: RemoteFileInfo) -> bool:
        return (self.modified_from is None or entry.modified_epoch >= self.modified_from) and (
            self.modified_to is None or entry.modified_epoch <= self.modified_to
        )


def _validate_entry(entry: RemoteFileInfo, remote_root: str) -> None:
    root = posixpath.normpath(remote_root)
    path = posixpath.normpath(entry.remote_path)
    if posixpath.commonpath((root, path)) != root or path == root:
        raise ValueError(f"日志路径越界: {entry.remote_path}")
    relative = PurePosixPath(entry.relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"日志相对路径无效: {entry.relative_path}")
    expected = posixpath.normpath(posixpath.join(root, entry.relative_path))
    if expected != path:
        raise ValueError(f"日志路径与相对路径不一致: {entry.remote_path}")


class DeviceLogService:
    def __init__(self, session: RemoteSession, remote_root: str = DEVICE_LOG_ROOT) -> None:
        normalized = posixpath.normpath(remote_root)
        if not normalized.startswith("/"):
            raise ValueError("设备日志根目录必须是绝对路径")
        self.session = session
        self.remote_root = normalized

    def browse(self, time_filter: LogTimeFilter | None = None) -> tuple[RemoteFileInfo, ...]:
        entries = self.session.list_files(self.remote_root)
        selected: list[RemoteFileInfo] = []
        for entry in entries:
            _validate_entry(entry, self.remote_root)
            if entry.is_symlink:
                continue
            if time_filter is None or entry.is_directory or time_filter.matches(entry):
                selected.append(entry)
        return tuple(selected)

    def download_selected(
        self,
        selected: tuple[RemoteFileInfo, ...],
        destination: Path,
        token: CancellationToken,
    ) -> dict[str, object]:
        unique: dict[str, RemoteFileInfo] = {}
        for entry in selected:
            _validate_entry(entry, self.remote_root)
            if entry.is_symlink or entry.is_directory:
                raise ValueError(f"不可下载目录或符号链接: {entry.remote_path}")
            unique[entry.remote_path] = entry

        current = {entry.remote_path: entry for entry in self.browse() if not entry.is_directory}
        ready: list[RemoteFileInfo] = []
        records: list[dict[str, object]] = []
        for entry in unique.values():
            fresh = current.get(entry.remote_path)
            if (
                fresh is None
                or fresh.size_bytes != entry.size_bytes
                or fresh.modified_epoch != entry.modified_epoch
            ):
                records.append(
                    {
                        "remote_path": entry.remote_path,
                        "relative_path": entry.relative_path,
                        "status": "failed",
                        "error": "远端文件在选择后发生变化或已不可见",
                    }
                )
            else:
                ready.append(entry)
        if ready and not token.is_cancelled:
            records.extend(self.session.download_files(ready, destination, token))

        manifest = {
            "schema_version": 1,
            "kind": "device_log_download",
            "remote_root": self.remote_root,
            "created_at": datetime.now(UTC).isoformat(),
            "requested_count": len(unique),
            "downloaded_count": sum(item.get("status") == "downloaded" for item in records),
            "failed_count": sum(item.get("status") != "downloaded" for item in records),
            "cancelled": token.is_cancelled,
            "files": records,
        }
        destination.mkdir(parents=True, exist_ok=True)
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**manifest, "manifest_path": str(manifest_path)}

    @staticmethod
    def display_row(entry: RemoteFileInfo) -> dict[str, object]:
        return {
            **asdict(entry),
            "modified_iso": datetime.fromtimestamp(entry.modified_epoch, UTC).astimezone().isoformat(),
        }
