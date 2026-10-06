param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [int]$MaxWorkerHeartbeatAgeSeconds = 180,
    [int]$WorkerHealthCheckSeconds = 30
)

$ErrorActionPreference = "Stop"
$mutex = New-Object System.Threading.Mutex($false, "Local\LifeOSRuntimeSupervisor")
$ownsMutex = $false
try {
    $ownsMutex = $mutex.WaitOne(0, $false)
}
catch [System.Threading.AbandonedMutexException] {
    $ownsMutex = $true
}
if (-not $ownsMutex) { exit 0 }

$python = (Get-Command python.exe -ErrorAction Stop).Source
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$db = Join-Path $homeDir "life.db"
$backupDir = Join-Path $homeDir "backups"
$logDir = Join-Path $homeDir "logs"
$workerLog = Join-Path $logDir "worker.log"
$launcherLog = Join-Path $logDir "launcher.log"
$guardian = Join-Path $RepoRoot "scripts\desktop_commander_guardian.ps1"
$nativeControlLog = Join-Path $logDir "windows-control.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $RepoRoot

$workerArgs = "-m life_os --db `"$db`" worker --home `"$homeDir`" --backups `"$backupDir`" --log `"$workerLog`""
$appArgs = "-m life_os --db `"$db`" app --port 8766 --no-open"
$guardianArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$guardian`" -HomeDir `"$homeDir`""
$nativeControlArgs = "-m life_os --db `"$db`" windows-control --home `"$homeDir`" --repo-root `"$RepoRoot`""

$worker = $null
$app = $null
$dcGuardian = $null
$nativeControl = $null
$workerBackoffUntil = Get-Date
$appBackoffUntil = Get-Date
$guardianBackoffUntil = Get-Date
$nativeControlBackoffUntil = Get-Date
$workerStartedAt = [datetime]::MinValue
$lastWorkerHealthCheck = [datetime]::MinValue

function Log([string]$Message) {
    Add-Content -LiteralPath $launcherLog -Value "$(Get-Date -Format o) $Message"
}

function Alive($Process) {
    if ($null -eq $Process) { return $false }
    try { $Process.Refresh(); return -not $Process.HasExited } catch { return $false }
}

function Start-Child([string]$Name, [string]$FilePath, [string]$Arguments) {
    Log "starting $Name"
    return Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
}

function Get-WorkerHeartbeatAgeSeconds {
    if (-not (Test-Path -LiteralPath $db -PathType Leaf)) { return $null }
    $probe = @'
import json
import sqlite3
import sys
import time

try:
    connection = sqlite3.connect(sys.argv[1], timeout=2.0)
    row = connection.execute(
        "SELECT value FROM worker_state WHERE key='worker.heartbeat'"
    ).fetchone()
    connection.close()
    if row is None:
        raise SystemExit(2)
    value = json.loads(row[0])
    stamp = value.get("timestamp_epoch")
    if not isinstance(stamp, (int, float)):
        raise SystemExit(3)
    print(max(0.0, time.time() - float(stamp)))
except Exception:
    raise SystemExit(4)
'@
    try {
        $raw = & $python -c $probe $db 2>$null
        if ($LASTEXITCODE -ne 0 -or $null -eq $raw) { return $null }
        return [double]::Parse(($raw | Select-Object -Last 1), [System.Globalization.CultureInfo]::InvariantCulture)
    }
    catch {
        return $null
    }
}

function Recycle-Worker([string]$Reason) {
    if ($null -ne $worker) {
        try {
            Log "recycling LIFE OS worker pid=$($worker.Id): $Reason"
            Stop-Process -Id $worker.Id -Force -ErrorAction Stop
            try { $worker.WaitForExit(5000) | Out-Null } catch {}
        }
        catch {
            Log "worker recycle failed pid=$($worker.Id): $($_.Exception.GetType().Name)"
        }
    }
    $script:worker = $null
    $script:workerBackoffUntil = (Get-Date).AddSeconds(2)
    $script:workerStartedAt = [datetime]::MinValue
}

while ($true) {
    $now = Get-Date

    if (-not (Alive $worker) -and $now -ge $workerBackoffUntil) {
        try {
            $worker = Start-Child "LIFE OS worker" $python $workerArgs
            $workerStartedAt = $now
            $lastWorkerHealthCheck = $now
            $workerBackoffUntil = $now.AddSeconds(5)
        }
        catch {
            Log "worker start failed: $($_.Exception.GetType().Name)"
            $workerBackoffUntil = $now.AddSeconds(15)
        }
    }

    if (
        (Alive $worker) -and
        $workerStartedAt -ne [datetime]::MinValue -and
        ($now - $workerStartedAt).TotalSeconds -ge $MaxWorkerHeartbeatAgeSeconds -and
        ($now - $lastWorkerHealthCheck).TotalSeconds -ge $WorkerHealthCheckSeconds
    ) {
        $lastWorkerHealthCheck = $now
        $heartbeatAge = Get-WorkerHeartbeatAgeSeconds
        if ($null -ne $heartbeatAge -and $heartbeatAge -gt $MaxWorkerHeartbeatAgeSeconds) {
            Recycle-Worker "heartbeat stale age=$([math]::Round($heartbeatAge, 1))s limit=$MaxWorkerHeartbeatAgeSeconds s"
        }
    }

    if (-not (Alive $app) -and $now -ge $appBackoffUntil) {
        try {
            $app = Start-Child "LIFE OS Control Room" $python $appArgs
            $appBackoffUntil = $now.AddSeconds(5)
        }
        catch {
            Log "Control Room start failed: $($_.Exception.GetType().Name)"
            $appBackoffUntil = $now.AddSeconds(15)
        }
    }

    if (-not (Alive $nativeControl) -and $now -ge $nativeControlBackoffUntil) {
        try {
            $nativeControl = Start-Child "MONOLITH Windows Control Agent" $python $nativeControlArgs
            $nativeControlBackoffUntil = $now.AddSeconds(5)
        }
        catch {
            Log "native Windows control start failed: $($_.Exception.GetType().Name)"
            $nativeControlBackoffUntil = $now.AddSeconds(15)
        }
    }

    if ((Test-Path -LiteralPath $guardian -PathType Leaf) -and -not (Alive $dcGuardian) -and $now -ge $guardianBackoffUntil) {
        try {
            $dcGuardian = Start-Child "Desktop Commander guardian (optional fallback)" "powershell.exe" $guardianArgs
            $guardianBackoffUntil = $now.AddSeconds(5)
        }
        catch {
            Log "Desktop Commander guardian start failed: $($_.Exception.GetType().Name)"
            $guardianBackoffUntil = $now.AddSeconds(15)
        }
    }

    Start-Sleep -Seconds 5
}
