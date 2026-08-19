[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
    throw "This script must run on Windows because PyInstaller cannot cross-compile Windows executables."
}

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required to build the embedded web UI."
}

if (-not $SkipInstall) {
    npm --prefix frontend ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
    uv sync --locked --extra packaging
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed." }
}

npm --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }

uv run --extra packaging pyinstaller --clean --noconfirm packaging/work-harness.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$Executable = Join-Path $RepositoryRoot "dist/work-harness.exe"
if (-not (Test-Path $Executable -PathType Leaf)) {
    throw "Expected executable was not created: $Executable"
}

$SmokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("work-harness-smoke-" + [guid]::NewGuid())
try {
    $env:WORK_HARNESS_DATA_ROOT = $SmokeData
    & $Executable check-config
    if ($LASTEXITCODE -ne 0) { throw "Executable configuration smoke test failed." }
}
finally {
    Remove-Item Env:WORK_HARNESS_DATA_ROOT -ErrorAction SilentlyContinue
    Remove-Item $SmokeData -Recurse -Force -ErrorAction SilentlyContinue
}

$Hash = (Get-FileHash -Algorithm SHA256 $Executable).Hash.ToLowerInvariant()
$HashFile = "$Executable.sha256"
Set-Content -Path $HashFile -Value "$Hash *work-harness.exe" -Encoding ascii

Write-Host "Built $Executable"
Write-Host "SHA256 $Hash"
