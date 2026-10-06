param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$db = Join-Path $homeDir "life.db"
$pidFile = Join-Path $homeDir "app.pid"
$url = "http://127.0.0.1:$Port/"
$health = "http://127.0.0.1:$Port/healthz"

New-Item -ItemType Directory -Force -Path $homeDir | Out-Null

function Test-LifeOsApp {
    try {
        $response = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

if (Test-LifeOsApp) {
    Start-Process $url
    Write-Output "LIFE OS app already running at $url"
    exit 0
}

Set-Location $RepoRoot
$q = [char]34
$quotedDb = "$q$db$q"
$args = @(
    "-m", "life_os",
    "--db", $quotedDb,
    "app",
    "--port", "$Port",
    "--no-open"
)
$process = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $process.Id -Encoding ascii

$ready = $false
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-LifeOsApp) {
        $ready = $true
        break
    }
    if ($process.HasExited) {
        break
    }
}

if (-not $ready) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    throw "LIFE OS app did not become healthy on $url"
}

Start-Process $url
Write-Output "LIFE OS app started at $url"
Write-Output "PID=$($process.Id)"
Write-Output "Database=$db"
