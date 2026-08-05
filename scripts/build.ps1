[CmdletBinding()]
param(
    [string]$Python = "py -3.14",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot

$pythonParts = $Python -split " ", 2
$pythonExe = $pythonParts[0]
$pythonPrefix = @()
if ($pythonParts.Count -gt 1) {
    $pythonPrefix = @($pythonParts[1])
}

$probe = @'
import struct, sys
if not ((3, 12) <= sys.version_info[:2] < (3, 15)):
    raise SystemExit(f'Python 3.12-3.14 required, found {sys.version.split()[0]}')
if struct.calcsize('P') * 8 != 64:
    raise SystemExit('64-bit Python required')
print(sys.executable)
'@
& $pythonExe @pythonPrefix -c $probe
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $pythonExe @pythonPrefix -m pip install -e ".[dev]"
if (-not $SkipTests) {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $pythonExe @pythonPrefix -m ruff check src tests
    & $pythonExe @pythonPrefix -m pytest -q
}
& $pythonExe @pythonPrefix -m PyInstaller --noconfirm --clean packaging\d7_factory_studio.spec

$exePath = Join-Path $projectRoot "dist\D7-Factory-Studio.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "PyInstaller did not create $exePath"
}
$hash = Get-FileHash -LiteralPath $exePath -Algorithm SHA256
Write-Host "Built: $exePath"
Write-Host "SHA-256: $($hash.Hash)"
