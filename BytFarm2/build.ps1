Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py -3.11"
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    throw "Python launcher ('py') or 'python' was not found in PATH."
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Args
    )
    $pythonCmd = Get-PythonCommand
    $command = "$pythonCmd $Args"
    Write-Host ">> $command" -ForegroundColor Cyan
    Invoke-Expression $command
}

Write-Host "=== BytFarm build start ===" -ForegroundColor Green

# Always run from project root.
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
Invoke-Python "-m pip install -r requirements.txt"
Invoke-Python "-m pip install pyinstaller"

Write-Host "Cleaning prior build artifacts..." -ForegroundColor Yellow
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "BytFarm2.spec.bak") { Remove-Item -Force "BytFarm2.spec.bak" }

$winRingDll = Join-Path $projectRoot "vendor\WinRing0x64.dll"
$winRingSys = Join-Path $projectRoot "vendor\WinRing0x64.sys"
$hasWinRing = (Test-Path $winRingDll) -and (Test-Path $winRingSys)

if ($hasWinRing) {
    Write-Host "WinRing0 detected. Building with BytFarm2.spec..." -ForegroundColor Yellow
    Invoke-Python "-m PyInstaller --noconfirm --clean BytFarm2.spec"
}
else {
    Write-Warning "WinRing0 files not found. Building a monitor/external-tool capable exe without WinRing0 binaries."
    Write-Warning "Expected files: vendor\WinRing0x64.dll and vendor\WinRing0x64.sys"
    Invoke-Python "-m PyInstaller --noconfirm --clean --onefile --windowed --name BytFarm2 --icon assets/icons/bytfarm.ico --version-file version_info.txt --uac-admin --paths src --hidden-import wmi --hidden-import win32api --hidden-import win32con --hidden-import pythoncom --hidden-import pywintypes --hidden-import pystray._win32 --hidden-import PIL --hidden-import PIL.Image --hidden-import tomllib --hidden-import tomli_w --hidden-import watchdog --hidden-import watchdog.observers --hidden-import watchdog.events --hidden-import cpuinfo --hidden-import psutil._pswindows --add-data `"assets/icons/*.ico;assets/icons`" --add-data `"assets/icons/*.png;assets/icons`" main.py"
}

$exePath = Join-Path $projectRoot "dist\BytFarm2.exe"
if (Test-Path $exePath) {
    Write-Host "Build successful: $exePath" -ForegroundColor Green
}
else {
    throw "Build finished but dist\BytFarm2.exe was not found."
}

Write-Host "=== BytFarm build complete ===" -ForegroundColor Green
