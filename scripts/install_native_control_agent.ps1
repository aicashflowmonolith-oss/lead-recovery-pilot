param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$TaskName = "LIFE OS Native Windows Control",
    [string]$RunValueName = "LIFE OS Native Windows Control"
)

$ErrorActionPreference = "Stop"
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$deployDir = Join-Path $homeDir "runtime\native-control"
$source = Join-Path $RepoRoot "scripts\native_control_launcher.ps1"
$target = Join-Path $deployDir "native_control_launcher.ps1"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "native control launcher missing"
}
New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Force

$args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$target`" -RepoRoot `"$RepoRoot`" -HomeDir `"$homeDir`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances "IgnoreNew"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$taskError = $null

try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $args -WorkingDirectory $deployDir
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Independent provider-neutral MONOLITH Windows control launcher" -Force -ErrorAction Stop | Out-Null
}
catch { $taskError = $_.Exception.Message }

# Always retain a non-admin per-user fallback. The launcher's named mutex and
# the agent's file lock prevent duplicate control loops.
New-Item -Path $runKey -Force | Out-Null
$runCommand = "powershell.exe $args"
New-ItemProperty -Path $runKey -Name $RunValueName -Value $runCommand -PropertyType String -Force | Out-Null
if ((Get-ItemPropertyValue -Path $runKey -Name $RunValueName -ErrorAction Stop) -ne $runCommand) {
    throw "native control HKCU fallback could not be verified"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) { Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 2

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "native control launcher was not deployed"
}
$escaped = [regex]::Escape($target)
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(?:powershell|pwsh)\.exe$' -and $_.CommandLine -match $escaped
})
if ($running.Count -eq 0) {
    throw "native control launcher process was not established"
}

Write-Output "Native Windows control installed."
Write-Output "ScheduledTask=$([bool]($null -ne $task))"
Write-Output "RunFallback=$RunValueName"
Write-Output "Launcher=$target"
if ($taskError) { Write-Output "ScheduledTaskFallbackReason=$taskError" }
