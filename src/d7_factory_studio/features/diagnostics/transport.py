from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from d7_factory_studio.core.ports import (
    CancellationToken,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteFileInfo,
    RemoteSession,
)


@dataclass(frozen=True, slots=True)
class SshConnection:
    host: str
    username: str
    password: str | None = None
    port: int = 22
    connect_timeout_s: float = 15.0
    known_hosts: Path | None = None
    allow_unknown_host: bool = False


def _shell_join(argv: tuple[str, ...]) -> str:
    import shlex

    return shlex.join(argv)


def _wrapped_command(argv: tuple[str, ...]) -> str:
    import shlex

    supervisor = "echo __D7_PGID__:$$; trap 'trap - TERM INT; kill -TERM -- -$$ 2>/dev/null' TERM INT; \"$@\""
    return (
        "setsid sh -c " + shlex.quote(supervisor) + " d7-remote " + " ".join(shlex.quote(arg) for arg in argv)
    )


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"不安全的远端相对路径: {value}")
    return path


def _safe_local_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).rstrip(". ")
    if cleaned.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned or "_"


class ParamikoRemoteSession(RemoteSession):
    """Paramiko transport with streamed output and remote process-group cleanup."""

    def __init__(self, connection: SshConnection, client_factory: Callable[[], Any] | None = None) -> None:
        self.connection = connection
        self._client_factory = client_factory
        self._client: Any | None = None

    def connect(self) -> None:
        import paramiko

        client = self._client_factory() if self._client_factory else paramiko.SSHClient()
        client.load_system_host_keys()
        if self.connection.known_hosts:
            client.load_host_keys(str(self.connection.known_hosts))
        policy = paramiko.AutoAddPolicy() if self.connection.allow_unknown_host else paramiko.RejectPolicy()
        client.set_missing_host_key_policy(policy)
        client.connect(
            hostname=self.connection.host,
            port=self.connection.port,
            username=self.connection.username,
            password=self.connection.password,
            look_for_keys=self.connection.password is None,
            allow_agent=self.connection.password is None,
            timeout=self.connection.connect_timeout_s,
            banner_timeout=self.connection.connect_timeout_s,
            auth_timeout=self.connection.connect_timeout_s,
        )
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("SSH 会话尚未连接")
        return self._client

    def execute(
        self,
        request: RemoteCommandRequest,
        token: CancellationToken,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> RemoteCommandResult:
        if not request.argv:
            raise ValueError("远端命令 argv 不能为空")
        client = self._require_client()
        started = time.monotonic()
        stdin, stdout, _stderr = client.exec_command(
            _wrapped_command(request.argv), timeout=request.timeout_s + 5
        )
        channel = stdout.channel
        if request.stdin_secret is not None:
            stdin.write(request.stdin_secret + "\n")
            stdin.flush()
        out_parts: list[bytes] = []
        err_parts: list[bytes] = []
        pgid: int | None = None
        timed_out = cancelled = False

        while True:
            while channel.recv_ready():
                chunk = channel.recv(65536)
                out_parts.append(chunk)
                if pgid is None:
                    match = re.search(rb"__D7_PGID__:(\d+)", b"".join(out_parts[:4]))
                    if match:
                        pgid = int(match.group(1))
                if on_output and request.stream_output:
                    streamed = re.sub(
                        r"^__D7_PGID__:\d+\r?\n?", "", chunk.decode("utf-8", errors="replace"), count=1
                    )
                    if streamed:
                        on_output(streamed, False)
            while channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                err_parts.append(chunk)
                if on_output and request.stream_output:
                    on_output(chunk.decode("utf-8", errors="replace"), True)
            if channel.exit_status_ready():
                break
            elapsed = time.monotonic() - started
            cancelled = token.is_cancelled
            timed_out = elapsed > request.timeout_s
            if cancelled or timed_out:
                if pgid is not None:
                    self._terminate_process_group(pgid)
                channel.close()
                break
            time.sleep(0.02)

        while not channel.closed and channel.recv_ready():
            out_parts.append(channel.recv(65536))
        while not channel.closed and channel.recv_stderr_ready():
            err_parts.append(channel.recv_stderr(65536))
        returncode = -1 if cancelled else 124 if timed_out else channel.recv_exit_status()
        out = b"".join(out_parts).decode("utf-8", errors="replace")
        out = re.sub(r"^__D7_PGID__:\d+\r?\n?", "", out, count=1)
        return RemoteCommandResult(
            returncode=returncode,
            stdout=out,
            stderr=b"".join(err_parts).decode("utf-8", errors="replace"),
            duration_s=round(time.monotonic() - started, 3),
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _terminate_process_group(self, pgid: int) -> None:
        command = f"kill -TERM -- -{pgid} 2>/dev/null; sleep 0.4; kill -KILL -- -{pgid} 2>/dev/null || true"
        try:
            _stdin, stdout, _stderr = self._require_client().exec_command(command, timeout=3)
            stdout.channel.recv_exit_status()
        except Exception:
            pass

    def list_files(self, remote_root: str) -> list[RemoteFileInfo]:
        root = posixpath.normpath(remote_root)
        if not root.startswith("/"):
            raise ValueError("远端根目录必须是绝对路径")
        sftp = self._require_client().open_sftp()
        result: list[RemoteFileInfo] = []
        pending = [root]
        try:
            while pending:
                current = pending.pop()
                for item in sftp.listdir_attr(current):
                    remote_path = posixpath.normpath(posixpath.join(current, item.filename))
                    if posixpath.commonpath((root, remote_path)) != root:
                        raise ValueError(f"远端路径越界: {remote_path}")
                    relative = posixpath.relpath(remote_path, root)
                    mode = int(item.st_mode or 0)
                    is_symlink = stat.S_ISLNK(mode)
                    is_directory = stat.S_ISDIR(mode)
                    result.append(
                        RemoteFileInfo(
                            remote_path=remote_path,
                            relative_path=relative,
                            name=item.filename,
                            size_bytes=int(item.st_size or 0),
                            modified_epoch=int(item.st_mtime or 0),
                            is_directory=is_directory,
                            is_symlink=is_symlink,
                        )
                    )
                    if is_directory and not is_symlink:
                        pending.append(remote_path)
        finally:
            sftp.close()
        return sorted(result, key=lambda item: (item.relative_path.casefold(), item.relative_path))

    def download_files(
        self,
        entries: Iterable[RemoteFileInfo],
        destination: Path,
        token: CancellationToken,
    ) -> list[dict[str, object]]:
        root = destination.resolve()
        root.mkdir(parents=True, exist_ok=True)
        sftp = self._require_client().open_sftp()
        records: list[dict[str, object]] = []
        used_paths: set[Path] = set()
        try:
            for entry in entries:
                record: dict[str, object] = {
                    "remote_path": entry.remote_path,
                    "relative_path": entry.relative_path,
                    "size_bytes": entry.size_bytes,
                    "modified_epoch": entry.modified_epoch,
                    "status": "failed",
                }
                part_path: Path | None = None
                try:
                    token.raise_if_cancelled()
                    if entry.is_directory or entry.is_symlink:
                        raise ValueError("目录或符号链接不可下载")
                    relative = _safe_relative_path(entry.relative_path)
                    local_path = root.joinpath(*(_safe_local_name(part) for part in relative.parts)).resolve()
                    if os.path.commonpath((str(root), str(local_path))) != str(root):
                        raise ValueError("本地下载路径越界")
                    if local_path in used_paths or local_path.exists():
                        suffix = hashlib.sha256(entry.relative_path.encode()).hexdigest()[:8]
                        local_path = local_path.with_name(f"{local_path.stem}_{suffix}{local_path.suffix}")
                    used_paths.add(local_path)
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    part_path = local_path.with_name(local_path.name + ".part")
                    before = sftp.lstat(entry.remote_path)
                    if stat.S_ISLNK(int(before.st_mode or 0)):
                        raise ValueError("远端文件已变为符号链接")
                    if (
                        int(before.st_size or 0) != entry.size_bytes
                        or int(before.st_mtime or 0) != entry.modified_epoch
                    ):
                        raise RuntimeError("远端文件在选择后发生变化")
                    digest = hashlib.sha256()
                    received = 0
                    with sftp.open(entry.remote_path, "rb") as source, part_path.open("wb") as target:
                        while chunk := source.read(1024 * 1024):
                            token.raise_if_cancelled()
                            target.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    after = sftp.lstat(entry.remote_path)
                    if int(after.st_size or 0) != int(before.st_size or 0) or int(after.st_mtime or 0) != int(
                        before.st_mtime or 0
                    ):
                        raise RuntimeError("远端文件在下载过程中发生变化")
                    if received != entry.size_bytes:
                        raise RuntimeError(f"下载字节数不一致: {received}/{entry.size_bytes}")
                    os.replace(part_path, local_path)
                    record.update(
                        status="downloaded",
                        local_path=str(local_path),
                        downloaded_bytes=received,
                        sha256=digest.hexdigest(),
                        remote_size_after=int(after.st_size or 0),
                        remote_modified_after=int(after.st_mtime or 0),
                    )
                except Exception as exc:
                    if part_path is not None:
                        part_path.unlink(missing_ok=True)
                    record["error"] = str(exc)
                records.append(record)
                if token.is_cancelled:
                    break
        finally:
            sftp.close()
        return records
