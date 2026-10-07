param(
    [switch]$Install,
    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$runtimeRoot = Join-Path $homeDir "runtime"
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


function Get-NativeControlProcesses {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $cmd = [string]$_.CommandLine
            if (-not $cmd) { return $false }
            return (
                $cmd -match "(?i)native[_-]?control[_-]?launcher\.ps1" -or
                $cmd -match "(?i)\bwindows[_-]?control\b"
            )
        })
    } catch {
        Write-RescueLog "native_control_probe_failed type=$($_.Exception.GetType().Name)"
        return @()
    }
}

function Test-NativeControlHealthy {
    return (@(Get-NativeControlProcesses).Count -gt 0)
}

function Get-LifeOSRepoRoot {
    try {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        $candidate = (& $python -c "import pathlib,life_os; print(pathlib.Path(life_os.__file__).resolve().parent.parent)" 2>$null | Select-Object -First 1)
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return [string]$candidate
        }
    } catch {}

    $candidates = @(
        (Join-Path $env:USERPROFILE "life-os"),
        (Join-Path $env:USERPROFILE "Documents\life-os"),
        (Join-Path $env:USERPROFILE "Desktop\life-os"),
        (Join-Path $env:USERPROFILE "source\life-os"),
        (Join-Path $env:USERPROFILE "repos\life-os")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate "pyproject.toml") -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Start-NativeControlRecovery {
    if (Test-StopGate) {
        Write-RescueLog "native_control_repair_skipped stop_gate_present"
        return $false
    }
    if (Test-NativeControlHealthy) { return $true }

    try {
        & schtasks.exe /Query /TN "LIFE OS Native Windows Control" 1>$null 2>$null
        if ($LASTEXITCODE -eq 0) {
            & schtasks.exe /Run /TN "LIFE OS Native Windows Control" 1>$null 2>$null
            Write-RescueLog "native_control_task_triggered"
            Start-Sleep -Seconds 4
            if (Test-NativeControlHealthy) { return $true }
        }
    } catch {
        Write-RescueLog "native_control_task_trigger_failed type=$($_.Exception.GetType().Name)"
    }

    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    try {
        $runCommand = Get-ItemPropertyValue -Path $runKey -Name "LIFE OS Native Windows Control" -ErrorAction Stop
        if ($runCommand) {
            Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/c", [string]$runCommand) -WindowStyle Hidden
            Write-RescueLog "native_control_hkcu_triggered"
            Start-Sleep -Seconds 4
            if (Test-NativeControlHealthy) { return $true }
        }
    } catch {
        Write-RescueLog "native_control_hkcu_trigger_failed type=$($_.Exception.GetType().Name)"
    }

    $repoRoot = Get-LifeOSRepoRoot
    try {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        $db = Join-Path $homeDir "life.db"
        if ($repoRoot -and (Test-Path -LiteralPath $db -PathType Leaf)) {
            Start-Process -FilePath $python -ArgumentList @(
                "-m", "life_os",
                "--db", ('"{0}"' -f $db),
                "windows-control",
                "--home", ('"{0}"' -f $homeDir),
                "--repo-root", ('"{0}"' -f $repoRoot)
            ) -WorkingDirectory $repoRoot -WindowStyle Hidden
            Write-RescueLog "native_control_python_started"
            Start-Sleep -Seconds 5
            if (Test-NativeControlHealthy) { return $true }
        }
    } catch {
        Write-RescueLog "native_control_python_start_failed type=$($_.Exception.GetType().Name)"
    }

    Write-RescueLog "native_control_repair_unverified"
    return $false
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
    $minuteTaskInstalled = $false
    try {
        & schtasks.exe /Create /F /TN $taskName /SC MINUTE /MO 1 /TR $taskCommand /RL LIMITED 1>$null 2>$null
        $minuteTaskInstalled = ($LASTEXITCODE -eq 0)
    } catch {}
    if (-not $minuteTaskInstalled) {
        Write-RescueLog "minute_task_registration_failed fallback=hkcu_run"
    }

    try {
        & schtasks.exe /Create /F /TN $taskLogonName /SC ONLOGON /TR $taskCommand /RL LIMITED 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-RescueLog "logon_task_registration_failed fallback=hkcu_run"
        }
    } catch {
        Write-RescueLog "logon_task_registration_failed fallback=hkcu_run type=$($_.Exception.GetType().Name)"
    }

    New-Item -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Force | Out-Null
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $runValueName -Value $taskCommand

    Write-RescueLog "watchdog_installed task_minute=$([int]$minuteTaskInstalled) hkcu_run=1"
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
        } else {
            $nativeHealthy = Test-NativeControlHealthy
            if (-not $nativeHealthy) {
                Write-RescueLog "native_control_unhealthy repair_start"
                $nativeHealthy = Start-NativeControlRecovery
            }

            $workerHealthy = Test-WorkerHealthy
            if (-not $workerHealthy) {
                Write-RescueLog "worker_unhealthy repair_start"
                $workerHealthy = Start-WorkerRecovery
            }

            if ($nativeHealthy -and $workerHealthy) {
                Write-RescueLog "repair_verified worker=1 native_control=1"
                $failures = 0
            } else {
                $failures++
                Write-RescueLog "repair_unverified worker=$([int]$workerHealthy) native_control=$([int]$nativeHealthy) failures=$failures"
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
