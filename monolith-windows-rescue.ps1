param(
    [switch]$Install,
    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"
$runtimeRoot = Join-Path $env:USERPROFILE ".life-os\runtime"
$watchdogPath = Join-Path $runtimeRoot "independent_rescue_watchdog.ps1"
$logDir = Join-Path $runtimeRoot "logs"
$logPath = Join-Path $logDir "independent_rescue_watchdog.log"
$taskName = "MONOLITH Independent Rescue Watchdog"
$taskLogonName = "MONOLITH Independent Rescue Watchdog Logon"
$runValueName = "MONOLITHIndependentRescueWatchdog"
$mutexName = "Local\MONOLITHIndependentRescueWatchdog"
$staleSeconds = 180
$loopSeconds = 30

function Write-RescueLog {
    param([string]$Message)
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $line = "{0:o} {1}" -f (Get-Date), $Message
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    } catch {}
}

function Test-StopGate {
    $candidates = @(
        (Join-Path $runtimeRoot "SYSTEM_STOP.lock"),
        (Join-Path $runtimeRoot "EMERGENCY_STOP.lock"),
        (Join-Path $runtimeRoot "SAFE_MODE.lock"),
        (Join-Path (Split-Path $runtimeRoot -Parent) "SYSTEM_STOP.lock")
    )
    return [bool]($candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1)
}

function Get-WorkerProcesses {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $cmd = [string]$_.CommandLine
            if (-not $cmd) { return $false }
            if ($cmd -like "*independent_rescue_watchdog.ps1*") { return $false }
            return (
                $cmd -like "*\.life-os\*" -and (
                    $cmd -match "(?i)recovery_launcher\.ps1" -or
                    $cmd -match "(?i)start_worker_now\.ps1" -or
                    $cmd -match "(?i)windows[_-]?control" -or
                    $cmd -match "(?i)\blife[_-]?os\b.*\bworker\b" -or
                    $cmd -match "(?i)\bworker\b.*\blife[_-]?os\b"
                )
            )
        })
    } catch {
        Write-RescueLog "process_probe_failed type=$($_.Exception.GetType().Name)"
        return @()
    }
}

function Get-BridgeStatusFile {
    $candidates = @(
        (Join-Path $runtimeRoot "control_bridge.last_status"),
        (Join-Path $runtimeRoot "state\control_bridge.last_status"),
        (Join-Path (Split-Path $runtimeRoot -Parent) "control_bridge.last_status")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Test-WorkerHealthy {
    $processes = @(Get-WorkerProcesses)
    if ($processes.Count -eq 0) { return $false }

    $statusFile = Get-BridgeStatusFile
    if ($statusFile) {
        try {
            $age = ((Get-Date) - (Get-Item -LiteralPath $statusFile).LastWriteTime).TotalSeconds
            if ($age -gt $staleSeconds) {
                Write-RescueLog ("bridge_status_stale age_seconds={0:N0}" -f $age)
                return $false
            }
        } catch {
            Write-RescueLog "bridge_status_probe_failed type=$($_.Exception.GetType().Name)"
        }
    }
    return $true
}

function Get-RecoveryLauncher {
    $startNow = Join-Path $runtimeRoot "start_worker_now.ps1"
    if (Test-Path -LiteralPath $startNow) { return $startNow }

    try {
        $candidate = Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter "recovery-*" -ErrorAction Stop |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { Join-Path $_.FullName "recovery_launcher.ps1" } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        return $candidate
    } catch {
        return $null
    }
}

function Stop-StaleWorkerProcesses {
    foreach ($proc in @(Get-WorkerProcesses)) {
        try {
            Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction Stop
            Write-RescueLog "stale_worker_stopped pid=$($proc.ProcessId)"
        } catch {
            Write-RescueLog "stale_worker_stop_failed pid=$($proc.ProcessId) type=$($_.Exception.GetType().Name)"
        }
    }
}

function Start-WorkerRecovery {
    if (Test-StopGate) {
        Write-RescueLog "repair_skipped stop_gate_present"
        return $false
    }

    $statusFile = Get-BridgeStatusFile
    if ($statusFile) {
        try {
            $age = ((Get-Date) - (Get-Item -LiteralPath $statusFile).LastWriteTime).TotalSeconds
            if ($age -gt $staleSeconds) { Stop-StaleWorkerProcesses }
        } catch {}
    }

    try {
        & schtasks.exe /Query /TN "LIFE OS Worker" 1>$null 2>$null
        if ($LASTEXITCODE -eq 0) {
            & schtasks.exe /Run /TN "LIFE OS Worker" 1>$null 2>$null
            Write-RescueLog "scheduled_worker_triggered"
            Start-Sleep -Seconds 8
            if (Test-WorkerHealthy) { return $true }
        }
    } catch {
        Write-RescueLog "scheduled_worker_trigger_failed type=$($_.Exception.GetType().Name)"
    }

    $launcher = Get-RecoveryLauncher
    if (-not $launcher) {
        Write-RescueLog "repair_failed launcher_missing"
        return $false
    }

    try {
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-NoProfile",
            "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $launcher)
        ) -WindowStyle Hidden
        Write-RescueLog "launcher_started"
        Start-Sleep -Seconds 10
        return (Test-WorkerHealthy)
    } catch {
        Write-RescueLog "launcher_start_failed type=$($_.Exception.GetType().Name)"
        return $false
    }
}

function Install-Watchdog {
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null

    $self = $PSCommandPath
    if (-not $self) { throw "installer must run from a file" }
    if ([IO.Path]::GetFullPath($self) -ne [IO.Path]::GetFullPath($watchdogPath)) {
        Copy-Item -LiteralPath $self -Destination $watchdogPath -Force
    }

    $taskCommand = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $watchdogPath + '"'
    & schtasks.exe /Create /F /TN $taskName /SC MINUTE /MO 1 /TR $taskCommand /RL LIMITED 1>$null
    if ($LASTEXITCODE -ne 0) { throw "minute watchdog task registration failed" }

    & schtasks.exe /Create /F /TN $taskLogonName /SC ONLOGON /TR $taskCommand /RL LIMITED 1>$null
    if ($LASTEXITCODE -ne 0) {
        Write-RescueLog "logon_task_registration_failed fallback=hkcu_run"
    }

    New-Item -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Force | Out-Null
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $runValueName -Value $taskCommand

    Write-RescueLog "watchdog_installed task_minute=1 hkcu_run=1"
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $watchdogPath),
        "-RunOnce"
    ) -WindowStyle Hidden
}

if ($Install) {
    Install-Watchdog
    exit 0
}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) { exit 0 }

try {
    $failures = 0
    do {
        if (Test-StopGate) {
            Write-RescueLog "watchdog_paused stop_gate_present"
            $failures = 0
        } elseif (Test-WorkerHealthy) {
            $failures = 0
        } else {
            Write-RescueLog "worker_unhealthy repair_start"
            if (Start-WorkerRecovery) {
                Write-RescueLog "repair_verified"
                $failures = 0
            } else {
                $failures++
                Write-RescueLog "repair_unverified failures=$failures"
            }
        }

        if ($RunOnce) { break }
        $sleep = [Math]::Min(300, $loopSeconds * [Math]::Max(1, [Math]::Pow(2, [Math]::Min($failures, 3))))
        Start-Sleep -Seconds ([int]$sleep)
    } while ($true)
} finally {
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
