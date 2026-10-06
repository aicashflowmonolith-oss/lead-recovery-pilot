param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$TaskName = "LIFE OS Worker",
    [string]$RunValueName = "LIFE OS Worker"
)

$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$db = Join-Path $homeDir "life.db"
$logDir = Join-Path $homeDir "logs"
$backupDir = Join-Path $homeDir "backups"
$launcher = Join-Path $RepoRoot "scripts\worker_launcher.ps1"
$nativeControlInstaller = Join-Path $RepoRoot "scripts\install_native_control_agent.ps1"
$desktopCommanderInstaller = Join-Path $RepoRoot "scripts\install_desktop_commander_guardian.ps1"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

New-Item -ItemType Directory -Force -Path $homeDir,$logDir,$backupDir | Out-Null
Set-Location $RepoRoot

& $python -m pip install --user -e .
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

& $python -m life_os --db $db worker --once --home $homeDir --backups $backupDir --log (Join-Path $logDir "worker.log")
if ($LASTEXITCODE -ne 0) { throw "one-shot worker verification failed with exit code $LASTEXITCODE" }
$launcherArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -RepoRoot `"$RepoRoot`""
$activation = $null
$scheduledTaskError = $null

try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $launcherArgs -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances "IgnoreNew"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Persistent LIFE OS autonomous worker" -Force -ErrorAction Stop | Out-Null
    $registeredTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($null -eq $registeredTask) { throw "scheduled task registration could not be verified" }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Remove-ItemProperty -Path $runKey -Name $RunValueName -ErrorAction SilentlyContinue
    $activation = "scheduled-task"
}
catch {
    $scheduledTaskError = $_.Exception.Message
}
if ($null -eq $activation) {
    New-Item -Path $runKey -Force | Out-Null
    $runCommand = "powershell.exe $launcherArgs"
    New-ItemProperty -Path $runKey -Name $RunValueName -Value $runCommand -PropertyType String -Force | Out-Null
    $savedRunCommand = Get-ItemPropertyValue -Path $runKey -Name $RunValueName -ErrorAction Stop
    if ($savedRunCommand -ne $runCommand) { throw "HKCU worker activation could not be verified" }

    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }

    Start-Process -FilePath "powershell.exe" -ArgumentList $launcherArgs -WindowStyle Hidden
    $activation = "hkcu-run"
}

if (-not (Test-Path -LiteralPath $nativeControlInstaller -PathType Leaf)) {
    throw "native Windows control installer is missing"
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $nativeControlInstaller -RepoRoot $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "native Windows control install failed with exit code $LASTEXITCODE" }

$desktopCommanderRecovery = "unavailable"
if (Test-Path -LiteralPath $desktopCommanderInstaller -PathType Leaf) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $desktopCommanderInstaller -RepoRoot $RepoRoot
    if ($LASTEXITCODE -eq 0) { $desktopCommanderRecovery = "installed-optional-fallback" }
    else { $desktopCommanderRecovery = "optional-fallback-install-failed" }
}

Start-Sleep -Seconds 4
$statusJson = & $python -m life_os --db $db worker-status --json
if ($LASTEXITCODE -ne 0) { throw "worker status probe failed" }
$status = $statusJson | ConvertFrom-Json
if ($null -eq $status.heartbeat) { throw "worker heartbeat was not established" }
$taskConfigured = $null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
$runConfigured = $null -ne (Get-ItemProperty -Path $runKey -Name $RunValueName -ErrorAction SilentlyContinue)
if (-not $taskConfigured -and -not $runConfigured) { throw "persistent worker activation was not established" }
$heartbeat = [double]$status.heartbeat.timestamp_epoch
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$age = $now - $heartbeat
if ($age -gt 90) { throw "worker heartbeat stale after install: $([math]::Round($age,1)) seconds" }

Write-Output "LIFE OS worker installed and activated."
Write-Output "Activation=$activation"
Write-Output "HeartbeatWorker=$($status.heartbeat.worker_id)"
Write-Output "HeartbeatAgeSeconds=$([math]::Round($age,1))"
Write-Output "Database=$db"
Write-Output "NativeWindowsControl=installed-independent"
Write-Output "DesktopCommanderRecovery=$desktopCommanderRecovery"
if ($null -ne $scheduledTaskError -and $activation -eq "hkcu-run") {
    Write-Output "ScheduledTaskFallbackReason=$scheduledTaskError"
}
