from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import queue
import shlex
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.core.ports import CanTransport

MAX_JSONL_BYTES = 1024 * 1024


class AgentProtocolError(RuntimeError):
    pass


class AgentOperationError(RuntimeError):
    def __init__(self, message: str, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code


class UnknownHostKeyError(RuntimeError):
    def __init__(self, host: str, fingerprint: str) -> None:
        super().__init__(f"{host} 的 SSH 主机指纹尚未确认: {fingerprint}")
        self.host = host
        self.fingerprint = fingerprint


def sha256_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class JsonlDecoder:
    def __init__(self, max_line_bytes: int = MAX_JSONL_BYTES) -> None:
        self._buffer = bytearray()
        self.max_line_bytes = max_line_bytes

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(chunk)
        if len(self._buffer) > self.max_line_bytes and b"\n" not in self._buffer:
            raise AgentProtocolError("agent JSONL 行超过大小限制")
        messages: list[dict[str, Any]] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline]).rstrip(b"\r")
            del self._buffer[: newline + 1]
            if not line:
                continue
            if len(line) > self.max_line_bytes:
                raise AgentProtocolError("agent JSONL 行超过大小限制")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentProtocolError(f"agent 返回无效 JSONL: {exc}") from exc
            if not isinstance(message, dict) or message.get("v") != 1:
                raise AgentProtocolError("agent 消息缺少受支持的协议版本 v=1")
            messages.append(message)
        return messages


class OrinAgentClient:
    """Interactive SSH JSONL client for the session-scoped D7 agent."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.on_event = on_event
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._transport: paramiko.Transport | None = None
        self._channel: paramiko.Channel | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._fatal_error: BaseException | None = None

    @property
    def is_connected(self) -> bool:
        return bool(self._transport and self._transport.is_active())

    @property
    def is_running(self) -> bool:
        return bool(self._channel and not self._channel.closed and not self._channel.exit_status_ready())

    def connect(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        expected_fingerprint: str,
        known_hosts_path: Path | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.close()
        sock = socket.create_connection((host, port), timeout=timeout_s)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=timeout_s)
            key = transport.get_remote_server_key()
            actual = sha256_fingerprint(key)
            if not expected_fingerprint:
                raise UnknownHostKeyError(host, actual)
            if actual != expected_fingerprint.strip():
                raise AgentProtocolError(
                    f"SSH 主机指纹不匹配：期望 {expected_fingerprint.strip()}，实际 {actual}"
                )
            transport.auth_password(username=username, password=password, fallback=False)
            if not transport.is_authenticated():
                raise AgentProtocolError("SSH 身份验证失败")
            if known_hosts_path is not None:
                known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
                host_keys = paramiko.HostKeys()
                if known_hosts_path.is_file():
                    host_keys.load(str(known_hosts_path))
                host_label = host if port == 22 else f"[{host}]:{port}"
                host_keys.add(host_label, key.get_name(), key)
                host_keys.save(str(known_hosts_path))
        except Exception:
            transport.close()
            raise
        self._transport = transport
        self._fatal_error = None
        self._stop.clear()

    def execute(self, command: str, timeout_s: float = 30.0) -> tuple[int, str, str]:
        transport = self._require_transport()
        channel = transport.open_session(timeout=timeout_s)
        channel.exec_command(command)
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + timeout_s
        while not channel.exit_status_ready():
            if channel.recv_ready():
                stdout.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(65536))
            if time.monotonic() >= deadline:
                channel.close()
                raise TimeoutError(f"远程命令超时: {command}")
            time.sleep(0.01)
        while channel.recv_ready():
            stdout.extend(channel.recv(65536))
        while channel.recv_stderr_ready():
            stderr.extend(channel.recv_stderr(65536))
        status = channel.recv_exit_status()
        channel.close()
        return status, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    def deploy_file(self, local_path: Path, remote_path: str, executable: bool = False) -> str:
        transport = self._require_transport()
        local_path = local_path.resolve(strict=True)
        remote = PurePosixPath(remote_path)
        if not remote.is_absolute() or ".." in remote.parts:
            raise ValueError("agent 远端路径必须是无上级跳转的绝对路径")
        parent = str(remote.parent)
        status, _stdout, stderr = self.execute(f"mkdir -p -- {shlex.quote(parent)}")
        if status != 0:
            raise AgentProtocolError(f"创建 agent 目录失败: {stderr.strip()}")
        temporary = str(remote) + ".part"
        local_hash = hashlib.sha256()
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            with local_path.open("rb") as source:

                class HashingReader:
                    def read(self, size: int = -1) -> bytes:
                        chunk = source.read(size)
                        local_hash.update(chunk)
                        return chunk

                sftp.putfo(HashingReader(), temporary, file_size=local_path.stat().st_size, confirm=True)
            if executable:
                sftp.chmod(temporary, 0o755)
            try:
                sftp.posix_rename(temporary, str(remote))
            except OSError:
                # Replacement is scoped to our own session-agent path, never the device-log root.
                with contextlib.suppress(OSError):
                    sftp.remove(str(remote))
                sftp.rename(temporary, str(remote))
        finally:
            sftp.close()
        status, stdout, stderr = self.execute(f"sha256sum -- {shlex.quote(str(remote))}")
        remote_hash = stdout.strip().split(maxsplit=1)[0] if status == 0 else ""
        if remote_hash.lower() != local_hash.hexdigest().lower():
            raise AgentProtocolError(f"agent 上传后 SHA-256 校验失败: {stderr.strip()}")
        return local_hash.hexdigest()

    def start(
        self,
        remote_binary: str,
        remote_config: str,
        remote_library_path: str | None = None,
    ) -> dict[str, Any]:
        transport = self._require_transport()
        if self.is_running:
            raise RuntimeError("agent 已在运行")
        channel = transport.open_session(timeout=10.0)
        environment = (
            f"env LD_LIBRARY_PATH={shlex.quote(remote_library_path)} "
            if remote_library_path
            else ""
        )
        command = (
            f"exec {environment}{shlex.quote(remote_binary)} --config {shlex.quote(remote_config)}"
        )
        channel.exec_command(command)
        self._channel = channel
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, name="d7-agent-reader", daemon=True)
        self._reader.start()
        hello = self.request("hello", timeout_s=10.0)
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="d7-agent-heartbeat", daemon=True
        )
        self._heartbeat.start()
        return hello

    def request(
        self,
        operation: str,
        *,
        target: dict[str, Any] | None = None,
        args: dict[str, Any] | None = None,
        timeout_s: float = 15.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        channel = self._require_channel()
        if self._fatal_error:
            raise AgentProtocolError(str(self._fatal_error))
        identifier = request_id or uuid.uuid4().hex
        payload: dict[str, Any] = {"v": 1, "id": identifier, "op": operation}
        if target is not None:
            payload["target"] = target
        if args is not None:
            payload["args"] = args
        response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if identifier in self._pending:
                raise ValueError(f"重复 agent 请求 ID: {identifier}")
            self._pending[identifier] = response_queue
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(encoded) > MAX_JSONL_BYTES:
                raise ValueError("agent 请求超过大小限制")
            with self._send_lock:
                channel.sendall(encoded)
            try:
                result = response_queue.get(timeout=timeout_s)
            except queue.Empty as exc:
                self.cancel(identifier)
                raise TimeoutError(f"agent 请求超时: {operation}") from exc
            if isinstance(result, BaseException):
                raise result
            if not result.get("ok"):
                error = result.get("error") or {}
                raise AgentOperationError(
                    str(error.get("message", "agent 操作失败")), str(error.get("code", "operation_failed"))
                )
            data = result.get("data", {})
            return data if isinstance(data, dict) else {"value": data}
        finally:
            with self._pending_lock:
                self._pending.pop(identifier, None)

    def add_event_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with contextlib.suppress(ValueError):
            self._event_listeners.remove(listener)

    def cancel(self, request_id: str) -> None:
        if not self.is_running:
            return
        with contextlib.suppress(AgentProtocolError, AgentOperationError, TimeoutError):
            self.request("cancel", args={"target_id": request_id}, timeout_s=3.0)

    def close(self) -> None:
        channel = self._channel
        if channel is not None and not channel.closed:
            with contextlib.suppress(AgentProtocolError, AgentOperationError, TimeoutError, OSError):
                self.request("shutdown", timeout_s=2.0)
        self._stop.set()
        if channel is not None and not channel.closed:
            channel.close()
        self._channel = None
        transport = self._transport
        if transport is not None:
            transport.close()
        self._transport = None
        self._fail_pending(AgentProtocolError("agent 会话已关闭"))

    def _reader_loop(self) -> None:
        decoder = JsonlDecoder()
        channel = self._channel
        assert channel is not None
        try:
            while not self._stop.is_set():
                if channel.recv_ready():
                    for message in decoder.feed(channel.recv(65536)):
                        self._dispatch(message)
                elif channel.exit_status_ready() or channel.closed:
                    raise AgentProtocolError("agent 意外退出")
                else:
                    time.sleep(0.01)
        except BaseException as exc:
            self._fatal_error = exc
            self._fail_pending(exc)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if message.get("type") == "result":
            identifier = str(message.get("id", ""))
            with self._pending_lock:
                target_queue = self._pending.get(identifier)
            if target_queue is not None:
                with contextlib.suppress(queue.Full):
                    target_queue.put_nowait(message)
            return
        if self.on_event is not None:
            self.on_event(message)
        for listener in tuple(self._event_listeners):
            listener(message)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(0.5):
            if not self.is_running:
                return
            try:
                self.request("heartbeat", timeout_s=1.0)
            except (AgentProtocolError, AgentOperationError, TimeoutError, OSError) as exc:
                self._fatal_error = exc
                self._fail_pending(exc)
                return

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            with contextlib.suppress(queue.Full):
                response_queue.put_nowait(error)

    def _require_transport(self) -> paramiko.Transport:
        if not self._transport or not self._transport.is_active():
            raise AgentProtocolError("SSH 尚未连接")
        return self._transport

    def _require_channel(self) -> paramiko.Channel:
        if not self.is_running or self._channel is None:
            raise AgentProtocolError("agent 尚未运行")
        return self._channel


class AgentCanTransport(CanTransport):
    """Synchronous CAN transport adapter over d7-factory-agent events."""

    def __init__(self, client: OrinAgentClient, bus: str, queue_size: int = 4096) -> None:
        self.client = client
        self.bus = bus
        self.queue_size = queue_size
        self._frames: queue.Queue[CanFrame] = queue.Queue(maxsize=queue_size)
        self._open = False
        self._mode = CanMode.FD

    @property
    def is_open(self) -> bool:
        return self._open and self.client.is_running

    def open(self, channel: int = 0, mode: CanMode = CanMode.FD) -> None:
        del channel
        if self.is_open:
            return
        self._mode = CanMode(mode)
        self.client.add_event_listener(self._on_event)
        try:
            self.client.request("can.subscribe")
        except Exception:
            self.client.remove_event_listener(self._on_event)
            raise
        self._open = True

    def close(self) -> None:
        if self._open and self.client.is_running:
            with contextlib.suppress(AgentProtocolError, AgentOperationError, TimeoutError):
                self.client.request("can.unsubscribe", timeout_s=3.0)
        self.client.remove_event_listener(self._on_event)
        self._open = False
        while not self._frames.empty():
            with contextlib.suppress(queue.Empty):
                self._frames.get_nowait()

    def send(self, frame: CanFrame) -> None:
        if not self.is_open:
            raise AgentProtocolError("远程 CAN 尚未打开")
        if frame.is_fd and self._mode is CanMode.CLASSIC:
            raise ValueError("Classic CAN 接口不能发送 CAN FD 帧")
        self.client.request(
            "can.send",
            args={
                "unsafe": True,
                "bus": self.bus,
                "id": frame.arbitration_id,
                "is_fd": frame.is_fd,
                "data": list(frame.data),
            },
        )

    def receive(self, timeout_ms: int = 50) -> list[CanFrame]:
        if not self.is_open:
            raise AgentProtocolError("远程 CAN 尚未打开")
        frames: list[CanFrame] = []
        timeout_s = max(0, timeout_ms) / 1000
        try:
            frames.append(self._frames.get(timeout=timeout_s))
        except queue.Empty:
            return []
        while True:
            try:
                frames.append(self._frames.get_nowait())
            except queue.Empty:
                return frames

    def _on_event(self, message: dict[str, Any]) -> None:
        if message.get("type") != "can.frame" or message.get("bus") != self.bus:
            return
        try:
            frame = CanFrame(
                arbitration_id=int(message["id"]),
                data=bytes(int(value) for value in message.get("data", [])),
                is_fd=bool(message.get("is_fd", True)),
                bitrate_switch=bool(message.get("is_fd", True)),
                timestamp=float(message.get("mono_ms", 0)) / 1000,
            )
        except (KeyError, TypeError, ValueError):
            return
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._frames.get_nowait()
            with contextlib.suppress(queue.Full):
                self._frames.put_nowait(frame)
