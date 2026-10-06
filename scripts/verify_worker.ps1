param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$TaskName = "LIFE OS Worker",
    [string]$RunValueName = "LIFE OS Worker",
    [int]$MaxHeartbeatAgeSeconds = 90,
    [int]$MaxNativeBridgeAgeSeconds = 240,
    [int]$MaxDesktopCommanderStateAgeSeconds = 240
)

$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$db = Join-Path $env:USERPROFILE ".life-os\life.db"
$homeDir = Join-Path $env:USERPROFILE ".life-os"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$dcGuardianTaskName = "LIFE OS Desktop Commander Guardian"
$dcBootstrapTaskName = "LIFE OS Desktop Commander Bootstrap"
$dcRunValueName = "LIFE OS Desktop Commander Bootstrap"
$dcStatePath = Join-Path $homeDir "runtime\desktop-commander-guardian.json"
Set-Location $RepoRoot

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$runValue = (Get-ItemProperty -Path $runKey -Name $RunValueName -ErrorAction SilentlyContinue).$RunValueName
if ($null -eq $task -and [string]::IsNullOrWhiteSpace([string]$runValue)) {
    throw "no persistent LIFE OS activation is configured"
}

$statusJson = & $python -m life_os --db $db worker-status --json
if ($LASTEXITCODE -ne 0) { throw "worker status command failed" }
$status = $statusJson | ConvertFrom-Json
if ($null -eq $status.heartbeat) { throw "missing worker heartbeat" }
$heartbeat = [double]$status.heartbeat.timestamp_epoch
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$age = $now - $heartbeat
if ($age -gt $MaxHeartbeatAgeSeconds) {
    throw "worker heartbeat stale: $([math]::Round($age,1)) seconds"
}
if ($status.queue.running -gt 1) {
    throw "unexpected concurrent running-job count: $($status.queue.running)"
}
if ($null -ne $status.audit -and $status.audit.healthy -ne $true) {
    throw "LIFE OS database self-audit is not healthy"
}

# Native MONOLITH control is a first-class peer control path. Read only the
# bounded bridge health receipts from SQLite; never read or print the credential.
$bridgeRowsJson = & $python -c "import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); rows=dict(c.execute('SELECT key,value FROM worker_state WHERE key IN (?,?)',('control_bridge.last_status','control_bridge.last_exchange'))); print(json.dumps(rows,sort_keys=True))" $db
if ($LASTEXITCODE -ne 0) { throw "native control bridge status probe failed" }
$bridgeRows = $bridgeRowsJson | ConvertFrom-Json
$bridgeHealthy = $false
$bridgeAge = $null
$bridgeStatus = $null
try {
    $bridgeStatusRaw = $bridgeRows.'control_bridge.last_status'
    if (-not [string]::IsNullOrWhiteSpace([string]$bridgeStatusRaw)) {
        $bridgeStatus = $bridgeStatusRaw | ConvertFrom-Json
    }
    $bridgeExchangeRaw = $bridgeRows.'control_bridge.last_exchange'
    if (-not [string]::IsNullOrWhiteSpace([string]$bridgeExchangeRaw)) {
        $bridgeExchange = [DateTimeOffset]::Parse([string]$bridgeExchangeRaw)
        $bridgeAge = ([DateTimeOffset]::UtcNow - $bridgeExchange.ToUniversalTime()).TotalSeconds
    }
    $bridgeHealthy = (
        $null -ne $bridgeStatus -and
        $bridgeStatus.enabled -eq $true -and
        $bridgeStatus.reachable -eq $true -and
        $null -ne $bridgeAge -and
        $bridgeAge -le $MaxNativeBridgeAgeSeconds
    )
}
catch {
    $bridgeHealthy = $false
}

# Desktop Commander remains a useful peer/debug transport, but after the native
# bridge exists it is no longer a mandatory control-plane dependency.
$dcGuardianTask = Get-ScheduledTask -TaskName $dcGuardianTaskName -ErrorAction SilentlyContinue
$dcBootstrapTask = Get-ScheduledTask -TaskName $dcBootstrapTaskName -ErrorAction SilentlyContinue
$dcRunValue = (Get-ItemProperty -Path $runKey -Name $dcRunValueName -ErrorAction SilentlyContinue).$dcRunValueName
$dcHealthy = $false
$dcAge = $null
$dcState = $null
if ($null -ne $dcGuardianTask -and ($null -ne $dcBootstrapTask -or -not [string]::IsNullOrWhiteSpace([string]$dcRunValue)) -and (Test-Path -LiteralPath $dcStatePath -PathType Leaf)) {
    try {
        $dcState = Get-Content -LiteralPath $dcStatePath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($dcState.status -eq "online" -and $null -ne $dcState.timestamp_epoch) {
            $dcAge = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) - [double]$dcState.timestamp_epoch
            $dcHealthy = $dcAge -le $MaxDesktopCommanderStateAgeSeconds
        }
    }
    catch {
        $dcHealthy = $false
    }
}

$controlPathHealthy = $bridgeHealthy -or $dcHealthy
if (-not $controlPathHealthy) {
    throw "no healthy persistent control path: native MONOLITH bridge and Desktop Commander are both unavailable or stale"
}

$activation = if ($null -ne $task) { "scheduled-task" } else { "hkcu-run" }
$bridgeLabel = if ($bridgeHealthy) { "online" } else { "not-proven" }
$dcLabel = if ($dcHealthy) { "online" } else { "optional-unavailable" }
Write-Output "VERIFIED"
Write-Output "Activation=$activation"
if ($null -ne $task) { Write-Output "TaskState=$($task.State)" }
Write-Output "HeartbeatAgeSeconds=$([math]::Round($age,1))"
Write-Output "Queue=$($status.queue | ConvertTo-Json -Compress)"
Write-Output "NativeControlBridge=$bridgeLabel"
if ($null -ne $bridgeAge) { Write-Output "NativeControlBridgeAgeSeconds=$([math]::Round($bridgeAge,1))" }
Write-Output "DesktopCommander=$dcLabel"
if ($null -ne $dcGuardianTask) { Write-Output "DesktopCommanderGuardianTask=$($dcGuardianTask.State)" }
if ($null -ne $dcBootstrapTask) { Write-Output "DesktopCommanderBootstrapTask=$($dcBootstrapTask.State)" }
if ($null -ne $dcAge) { Write-Output "DesktopCommanderStateAgeSeconds=$([math]::Round($dcAge,1))" }
