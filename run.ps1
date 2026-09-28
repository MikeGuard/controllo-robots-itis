<#
.SYNOPSIS
    Quickstart script to launch the UR Robotics Lab Platform on Windows (PowerShell).
#>
[CmdletBinding()]
param(
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# Activate virtualenv if available
$venvPython = $null
if (Test-Path "$ScriptDir\.venv\Scripts\python.exe") {
    $venvPython = "$ScriptDir\.venv\Scripts\python.exe"
    & "$ScriptDir\.venv\Scripts\Activate.ps1"
} elseif (Test-Path "$ScriptDir\..\.venv\Scripts\python.exe") {
    $venvPython = "$ScriptDir\..\.venv\Scripts\python.exe"
    & "$ScriptDir\..\.venv\Scripts\Activate.ps1"
} else {
    $venvPython = "python"
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "    Universal Robots Lab Platform - Starting Up" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "Instructor Dashboard: http://localhost:8000/admin" -ForegroundColor Green
Write-Host ""
Write-Host "Share one of these URLs with your students on the LAN:" -ForegroundColor Yellow

try {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -notmatch '^127\.' } |
        ForEach-Object { Write-Host " -> http://$($_.IPAddress):8000" -ForegroundColor White }
} catch {
    Write-Host " -> http://localhost:8000"
}
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$reloadFlag = @()
if ($Reload -or $env:DEV_MODE -eq "1") {
    $reloadFlag = @("--reload")
}

& $venvPython -m uvicorn app:app --host 0.0.0.0 --port 8000 @reloadFlag
