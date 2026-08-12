from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from d7_factory_studio.core.ports import CancellationToken, RemoteCommandRequest, RemoteFileInfo
from d7_factory_studio.features.diagnostics.transport import (
    ParamikoRemoteSession,
    SshConnection,
    _wrapped_command,
)


class FakeChannel:
    def __init__(
        self, stdout: list[bytes], stderr: list[bytes] | None = None, exit_after_reads: bool = True
    ) -> None:
        self.out = list(stdout)
        self.err = list(stderr or [])
        self.exit_after_reads = exit_after_reads
        self.closed = False

    def recv_ready(self):
        return bool(self.out)

    def recv(self, _size):
        return self.out.pop(0)

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv_stderr(self, _size):
        return self.err.pop(0)

    def exit_status_ready(self):
        return self.exit_after_reads and not self.out and not self.err

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class FakeWriter:
    def __init__(self) -> None:
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self): ...


class FakeSftp:
    def __init__(self) -> None:
        self.payloads = {
            "/sdcard/pudu/log/a.log": b"hello",
            "/sdcard/pudu/log/nested/b.log": b"world!",
        }
        self.attrs = {
            path: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=len(data), st_mtime=100)
            for path, data in self.payloads.items()
        }
        self.closed = False

    def listdir_attr(self, path):
        if path == "/sdcard/pudu/log":
            return [
                SimpleNamespace(filename="a.log", **vars(self.attrs["/sdcard/pudu/log/a.log"])),
                SimpleNamespace(filename="nested", st_mode=stat.S_IFDIR | 0o755, st_size=0, st_mtime=90),
                SimpleNamespace(filename="escape", st_mode=stat.S_IFLNK | 0o777, st_size=0, st_mtime=90),
            ]
        if path == "/sdcard/pudu/log/nested":
            return [SimpleNamespace(filename="b.log", **vars(self.attrs["/sdcard/pudu/log/nested/b.log"]))]
        raise FileNotFoundError(path)

    def lstat(self, path):
        return self.attrs[path]

    def open(self, path, mode):
        assert mode == "rb"
        return io.BytesIO(self.payloads[path])

    def close(self):
        self.closed = True

    def remove(self, _path):
        raise AssertionError("远端删除绝不能被调用")


class FakeClient:
    def __init__(self, *, running: bool = False) -> None:
        self.commands: list[str] = []
        self.running = running
        self.sftp = FakeSftp()

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("kill -TERM"):
            channel = FakeChannel([])
        else:
            channel = FakeChannel([b"__D7_PGID__:4321\nhello\n"], [b"warning\n"], not self.running)
        return FakeWriter(), SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)

    def open_sftp(self):
        return self.sftp

    def close(self): ...


def connected_session(client: FakeClient) -> ParamikoRemoteSession:
    session = ParamikoRemoteSession(SshConnection("example", "user"))
    session._client = client
    return session


def test_remote_wrapper_uses_setsid_and_process_group_marker() -> None:
    command = _wrapped_command(("sh", "-lc", "echo ok"))
    assert command.startswith("setsid --wait sh -c")
    assert "__D7_PGID__" in command
    assert "kill -TERM -- -$$" in command


@pytest.mark.skipif(os.name != "posix" or shutil.which("setsid") is None, reason="requires setsid")
def test_remote_wrapper_waits_for_child_process() -> None:
    command = _wrapped_command(("sh", "-c", "sleep 0.2; echo done"))
    started = time.monotonic()
    result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
    assert time.monotonic() - started >= 0.18
    assert result.returncode == 0
    assert "done" in result.stdout


def test_execute_streams_stdout_and_stderr_and_hides_marker() -> None:
    client = FakeClient()
    session = connected_session(client)
    streamed: list[tuple[str, bool]] = []
    result = session.execute(
        RemoteCommandRequest(("echo", "ok"), stream_output=True),
        CancellationToken(),
        lambda text, is_stderr: streamed.append((text, is_stderr)),
    )
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == "warning\n"
    assert any(not is_stderr for _, is_stderr in streamed)
    assert any(is_stderr for _, is_stderr in streamed)


def test_cancel_kills_remote_process_group() -> None:
    client = FakeClient(running=True)
    session = connected_session(client)
    token = CancellationToken()

    def cancel_after_output(_text: str, _is_stderr: bool) -> None:
        token.cancel()

    result = session.execute(
        RemoteCommandRequest(("sleep", "30"), stream_output=True), token, cancel_after_output
    )
    assert result.cancelled and result.returncode == -1
    assert any(command.startswith("kill -TERM -- -4321") for command in client.commands)


def test_sftp_lists_without_following_symlink_and_downloads_atomically(tmp_path: Path) -> None:
    session = connected_session(FakeClient())
    entries = session.list_files("/sdcard/pudu/log")
    assert [item.relative_path for item in entries] == ["a.log", "escape", "nested", "nested/b.log"]
    assert next(item for item in entries if item.name == "escape").is_symlink
    files = [item for item in entries if not item.is_directory and not item.is_symlink]
    records = session.download_files(files, tmp_path, CancellationToken())
    assert [item["status"] for item in records] == ["downloaded", "downloaded"]
    assert (tmp_path / "a.log").read_bytes() == b"hello"
    assert (tmp_path / "nested" / "b.log").read_bytes() == b"world!"
    assert records[0]["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert not list(tmp_path.rglob("*.part"))


def test_download_rejects_traversal_before_opening_file(tmp_path: Path) -> None:
    session = connected_session(FakeClient())
    entry = RemoteFileInfo("/sdcard/pudu/log/a.log", "../a.log", "a.log", 5, 100)
    record = session.download_files((entry,), tmp_path, CancellationToken())[0]
    assert record["status"] == "failed"
    assert "不安全" in record["error"]
