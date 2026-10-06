param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$GuardianTaskName = "LIFE OS Desktop Commander Guardian",
    [string]$BootstrapTaskName = "LIFE OS Desktop Commander Bootstrap",
    [string]$RunValueName = "LIFE OS Desktop Commander Bootstrap"
)

$ErrorActionPreference = "Stop"
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$deployDir = Join-Path $homeDir "runtime\desktop-commander"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$guardianSource = Join-Path $RepoRoot "scripts\desktop_commander_guardian.ps1"
$bootstrapSource = Join-Path $RepoRoot "scripts\desktop_commander_bootstrap.ps1"
$guardianTarget = Join-Path $deployDir "desktop_commander_guardian.ps1"
$bootstrapTarget = Join-Path $deployDir "desktop_commander_bootstrap.ps1"

foreach ($path in @($guardianSource,$bootstrapSource)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "required Desktop Commander recovery asset missing: $path" }
}
New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
Copy-Item -LiteralPath $guardianSource -Destination $guardianTarget -Force
Copy-Item -LiteralPath $bootstrapSource -Destination $bootstrapTarget -Force

# Retire only older guardian copies. The deployed target owns the stable mutex.
$escapedTarget = [regex]::Escape($guardianTarget)
$legacyGuardians = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(?:powershell|pwsh)\.exe$' -and
    $_.CommandLine -match 'desktop_commander_guardian\.ps1' -and
    $_.CommandLine -notmatch $escapedTarget
})
foreach ($legacyGuardian in $legacyGuardians) {
    Stop-Process -Id ([int]$legacyGuardian.ProcessId) -Force -ErrorAction SilentlyContinue
}
if ($legacyGuardians.Count -gt 0) { Start-Sleep -Seconds 2 }

$guardianArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$guardianTarget`" -HomeDir `"$homeDir`""
$bootstrapArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$bootstrapTarget`" -HomeDir `"$homeDir`" -GuardianPath `"$guardianTarget`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances "IgnoreNew"
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn

# Task Scheduler is preferred but not authoritative. Some user sessions cannot
# register tasks; that must never prevent the HKCU fallback or immediate repair.
$guardianTaskError = $null
$bootstrapTaskError = $null
try {
    $guardianAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $guardianArgs -WorkingDirectory $deployDir
    Register-ScheduledTask -TaskName $GuardianTaskName -Action $guardianAction -Trigger $logonTrigger -Settings $settings `
        -Description "Always-on Desktop Commander remote-channel guardian for LIFE OS" -Force -ErrorAction Stop | Out-Null
}
catch { $guardianTaskError = $_.Exception.Message }

try {
    $bootstrapAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $bootstrapArgs -WorkingDirectory $deployDir
    Register-ScheduledTask -TaskName $BootstrapTaskName -Action $bootstrapAction -Trigger $logonTrigger -Settings $settings `
        -Description "Independent watchdog that restores the Desktop Commander guardian if it disappears or hangs" -Force -ErrorAction Stop | Out-Null
}
catch { $bootstrapTaskError = $_.Exception.Message }

# Always establish the per-user logon fallback, even when Task Scheduler is
# unavailable or access is denied. This path requires no elevated permission.
New-Item -Path $runKey -Force | Out-Null
$runCommand = "powershell.exe $bootstrapArgs"
New-ItemProperty -Path $runKey -Name $RunValueName -Value $runCommand -PropertyType String -Force | Out-Null
if ((Get-ItemPropertyValue -Path $runKey -Name $RunValueName -ErrorAction Stop) -ne $runCommand) {
    throw "Desktop Commander HKCU recovery fallback could not be verified"
}

# Start whichever scheduled paths exist, then invoke the bootstrap directly.
# Its named mutex makes this safe when a scheduled/Run instance is already live.
$guardianTask = Get-ScheduledTask -TaskName $GuardianTaskName -ErrorAction SilentlyContinue
$bootstrapTask = Get-ScheduledTask -TaskName $BootstrapTaskName -ErrorAction SilentlyContinue
if ($null -ne $guardianTask) { Start-ScheduledTask -TaskName $GuardianTaskName -ErrorAction SilentlyContinue }
if ($null -ne $bootstrapTask) { Start-ScheduledTask -TaskName $BootstrapTaskName -ErrorAction SilentlyContinue }
Start-Process -FilePath "powershell.exe" -ArgumentList $bootstrapArgs -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 3

if (-not (Test-Path -LiteralPath $guardianTarget -PathType Leaf) -or
    -not (Test-Path -LiteralPath $bootstrapTarget -PathType Leaf)) {
    throw "Desktop Commander stable recovery assets are missing after install"
}

Write-Output "Desktop Commander recovery installed."
Write-Output "GuardianTask=$([bool]($null -ne $guardianTask))"
Write-Output "BootstrapTask=$([bool]($null -ne $bootstrapTask))"
Write-Output "RunFallback=$RunValueName"
Write-Output "DeployedGuardian=$guardianTarget"
if ($guardianTaskError) { Write-Output "GuardianTaskFallbackReason=$guardianTaskError" }
if ($bootstrapTaskError) { Write-Output "BootstrapTaskFallbackReason=$bootstrapTaskError" }
