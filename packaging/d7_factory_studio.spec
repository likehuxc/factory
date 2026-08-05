# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).resolve().parent.parent
package_root = project_root / "src" / "d7_factory_studio"
datas = [
    (str(package_root / "config" / "evt1.yaml"), "d7_factory_studio/config"),
    (str(package_root / "config" / "evt2.yaml"), "d7_factory_studio/config"),
    (str(project_root / "THIRD_PARTY_NOTICES.md"), "."),
]

agent_binary = project_root / "artifacts" / "agent" / "aarch64" / "d7-factory-agent"
if agent_binary.is_file():
    datas.append((str(agent_binary), "d7_factory_studio/agent/aarch64"))

hiddenimports = collect_submodules("keyring.backends")

a = Analysis(
    [str(package_root / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="D7-Factory-Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
)
