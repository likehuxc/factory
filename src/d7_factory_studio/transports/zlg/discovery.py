from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PE_MACHINE_AMD64 = 0x8664
CONTROL_CAN_FD_NAME = "ControlCANFD.dll"
REQUIRED_EXPORTS = (
    "ZCAN_OpenDevice",
    "ZCAN_CloseDevice",
    "ZCAN_InitCAN",
    "ZCAN_StartCAN",
    "ZCAN_ResetCAN",
    "ZCAN_Transmit",
    "ZCAN_Receive",
    "ZCAN_TransmitFD",
    "ZCAN_ReceiveFD",
)


class ZlgDllDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DllValidation:
    path: Path
    machine: int
    file_version: str = ""


def read_pe_machine(path: str | Path) -> int:
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ZlgDllDiscoveryError(f"not a PE DLL: {candidate}")
            stream.seek(0x3C)
            pe_offset_raw = stream.read(4)
            if len(pe_offset_raw) != 4:
                raise ZlgDllDiscoveryError(f"truncated PE header: {candidate}")
            stream.seek(int.from_bytes(pe_offset_raw, "little"))
            if stream.read(4) != b"PE\0\0":
                raise ZlgDllDiscoveryError(f"invalid PE signature: {candidate}")
            machine_raw = stream.read(2)
    except OSError as exc:
        raise ZlgDllDiscoveryError(f"cannot read DLL {candidate}: {exc}") from exc
    if len(machine_raw) != 2:
        raise ZlgDllDiscoveryError(f"truncated COFF header: {candidate}")
    return int.from_bytes(machine_raw, "little")


def _default_loader(path: str) -> Any:
    if sys.platform != "win32":
        raise ZlgDllDiscoveryError("ControlCANFD.dll is supported on Windows only")
    directory_handle = None
    try:
        if hasattr(os, "add_dll_directory"):
            directory_handle = os.add_dll_directory(str(Path(path).parent))
        return ctypes.WinDLL(path)
    finally:
        if directory_handle is not None:
            directory_handle.close()


def validate_controlcanfd(
    path: str | Path,
    *,
    loader: Callable[[str], Any] | None = None,
) -> DllValidation:
    candidate = Path(path).expanduser().resolve()
    if candidate.name.casefold() != CONTROL_CAN_FD_NAME.casefold():
        raise ZlgDllDiscoveryError(f"expected {CONTROL_CAN_FD_NAME}: {candidate}")
    if not candidate.is_file():
        raise ZlgDllDiscoveryError(f"DLL does not exist: {candidate}")
    machine = read_pe_machine(candidate)
    if machine != PE_MACHINE_AMD64:
        raise ZlgDllDiscoveryError(f"DLL is not x64 (machine=0x{machine:04X}): {candidate}")
    try:
        dll = (loader or _default_loader)(str(candidate))
    except (OSError, ZlgDllDiscoveryError) as exc:
        raise ZlgDllDiscoveryError(f"cannot load x64 DLL {candidate}: {exc}") from exc
    missing = [name for name in REQUIRED_EXPORTS if not hasattr(dll, name)]
    if missing:
        raise ZlgDllDiscoveryError(f"DLL is missing required exports: {', '.join(missing)}")
    return DllValidation(candidate, machine)


def iter_controlcanfd_candidates(
    *,
    explicit_path: str | Path | None = None,
    search_roots: Iterable[str | Path] | None = None,
) -> Iterator[Path]:
    seen: set[Path] = set()

    def emit(path: Path) -> Iterator[Path]:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved

    if explicit_path:
        yield from emit(Path(explicit_path))

    for local in (
        Path(sys.executable).resolve().parent / CONTROL_CAN_FD_NAME,
        Path.cwd() / CONTROL_CAN_FD_NAME,
    ):
        yield from emit(local)

    if search_roots is None:
        roots: list[Path] = [Path.home() / "Desktop" / "绿色软件"]
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if value := os.environ.get(variable):
                roots.append(Path(value))
    else:
        roots = [Path(root) for root in search_roots]

    preferred_patterns = (
        "**/二次开发库*/x64/ControlCANFD.dll",
        "**/ZCanPro/bin/x64/ControlCANFD.dll",
        "**/x64/ControlCANFD.dll",
        "**/ControlCANFD.dll",
    )
    for pattern in preferred_patterns:
        for root in roots:
            if root.exists():
                for path in sorted(root.glob(pattern)):
                    yield from emit(path)


def discover_controlcanfd(
    *,
    explicit_path: str | Path | None = None,
    search_roots: Iterable[str | Path] | None = None,
    loader: Callable[[str], Any] | None = None,
) -> Path:
    failures: list[str] = []
    for path in iter_controlcanfd_candidates(explicit_path=explicit_path, search_roots=search_roots):
        try:
            return validate_controlcanfd(path, loader=loader).path
        except ZlgDllDiscoveryError as exc:
            failures.append(str(exc))
    detail = "; ".join(failures[-5:]) if failures else "no candidates found"
    raise ZlgDllDiscoveryError(f"no usable x64 {CONTROL_CAN_FD_NAME}: {detail}")
