<#
.SYNOPSIS
    Build DexaOCRWorker as a standalone Windows executable using PyInstaller.

.DESCRIPTION
    1. Installs / upgrades PyInstaller in the current Python environment.
    2. Runs PyInstaller with worker.spec.
    3. Copies the environment template and service scripts to the output.

.PARAMETER Clean
    Remove build\ and dist\ before building.

.PARAMETER NoPip
    Skip the PyInstaller pip install step (use when offline).

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean
    .\build.ps1 -Clean -NoPip
#>
param(
    [switch]$Clean,
    [switch]$NoPip
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot

Set-Location $ProjectRoot

# ── Optional clean ────────────────────────────────────────────────────────────
if ($Clean) {
    Write-Host "Cleaning previous build artefacts..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "$ProjectRoot\build" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "$ProjectRoot\dist"  -ErrorAction SilentlyContinue
    Write-Host "Done." -ForegroundColor Green
}

# ── Ensure PyInstaller is installed ──────────────────────────────────────────
if (-not $NoPip) {
    Write-Host "`nInstalling / upgrading PyInstaller..." -ForegroundColor Cyan
    pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }
}

# ── Build ─────────────────────────────────────────────────────────────────────
Write-Host "`nRunning PyInstaller..." -ForegroundColor Cyan
$distDir = "$ProjectRoot\dist\DexaOCRWorker"
$distEnv = Join-Path $distDir ".env"
$stagingRoot = Join-Path $ProjectRoot ("build\dist-staging-{0}" -f [Guid]::NewGuid())
$stagingDir = Join-Path $stagingRoot "DexaOCRWorker"
$envBackup = $null
if (Test-Path -LiteralPath $distEnv -PathType Leaf) {
    $envBackup = Join-Path ([IO.Path]::GetTempPath()) ("DexaOCR-{0}.env" -f [Guid]::NewGuid())
    Copy-Item -LiteralPath $distEnv -Destination $envBackup
}

try {
    # Build outside the live deployment directory. This preserves logs, work
    # files and configuration, and avoids OneDrive/file-lock deletion errors.
    pyinstaller worker.spec --noconfirm --distpath $stagingRoot
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

    [IO.Directory]::CreateDirectory($distDir) | Out-Null
    Get-ChildItem -LiteralPath $stagingDir -Force | Copy-Item `
        -Destination $distDir -Recurse -Force
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    if ($envBackup -and (Test-Path -LiteralPath $envBackup -PathType Leaf)) {
        [IO.Directory]::CreateDirectory($distDir) | Out-Null
        Copy-Item -LiteralPath $envBackup -Destination $distEnv -Force
        Remove-Item -LiteralPath $envBackup -Force
    }
}

# ── Post-build: copy env template ─────────────────────────────────────────────
if (Test-Path "$ProjectRoot\.env.example") {
    Copy-Item "$ProjectRoot\.env.example" "$distDir\.env.example" -Force
    Write-Host "Copied .env.example to $distDir" -ForegroundColor DarkGray
}

Copy-Item "$ProjectRoot\deploy\install_service.ps1" "$distDir\install_service.ps1" -Force
Copy-Item "$ProjectRoot\deploy\uninstall_service.ps1" "$distDir\uninstall_service.ps1" -Force
Write-Host "Copied Windows Service scripts to $distDir" -ForegroundColor DarkGray

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Build complete!" -ForegroundColor Green
Write-Host " Output folder : $distDir" -ForegroundColor Green
Write-Host ""
Write-Host " Before deploying:" -ForegroundColor Yellow
Write-Host "   1. Copy dist\DexaOCRWorker\ to the production server." -ForegroundColor Yellow
Write-Host "   2. Place a configured .env file next to DexaOCRWorker.exe." -ForegroundColor Yellow
Write-Host "   3. Run install_service.ps1 as Administrator." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
