param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$pidFile = Join-Path $homeDir "app.pid"

if (-not (Test-Path $pidFile)) {
    Write-Output "No LIFE OS app PID file found."
    exit 0
}

$pidValue = [int](Get-Content $pidFile -Raw)
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue

if ($null -eq $processInfo) {
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Write-Output "LIFE OS app was not running."
    exit 0
}

$commandLine = [string]$processInfo.CommandLine
if ($commandLine -notmatch "life_os" -or $commandLine -notmatch "\sapp(\s|$)") {
    throw "PID $pidValue does not appear to be the LIFE OS app; refusing to stop it."
}

Stop-Process -Id $pidValue -ErrorAction Stop
Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Output "LIFE OS app stopped."
