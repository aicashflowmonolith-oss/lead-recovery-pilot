param(
    [string]$HomeDir = (Join-Path $env:USERPROFILE ".life-os")
)

$ErrorActionPreference = "Stop"
$mutex = New-Object System.Threading.Mutex($false, "Local\LifeOSDesktopCommanderGuardian")
$ownsMutex = $false
try {
    $ownsMutex = $mutex.WaitOne(0, $false)
}
catch [System.Threading.AbandonedMutexException] {
    # If the prior guardian died while holding the singleton, Windows transfers
    # ownership to this waiter. Continue recovery instead of exiting.
    $ownsMutex = $true
}
if (-not $ownsMutex) { exit 0 }

$logDir = Join-Path $HomeDir "logs"
$runtimeDir = Join-Path $HomeDir "runtime"
$guardianLog = Join-Path $logDir "desktop-commander-guardian.log"
$stdoutLog = Join-Path $logDir "desktop-commander-remote.out.log"
$stderrLog = Join-Path $logDir "desktop-commander-remote.err.log"
$statePath = Join-Path $runtimeDir "desktop-commander-guardian.json"
$migrationMarker = Join-Path $runtimeDir "desktop-commander-guardian-migrated-v1"
New-Item -ItemType Directory -Force -Path $logDir,$runtimeDir | Out-Null

function Write-GuardianLog([string]$Message) {
    Add-Content -LiteralPath $guardianLog -Value "$(Get-Date -Format o) $Message"
}

function Write-State([string]$Status, [int]$ProcessId = 0, [string]$Reason = "", [string]$Entry = "") {
    $value = [ordered]@{
        status = $Status
        pid = $ProcessId
        reason = $Reason
        entry = $Entry
        timestamp_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    } | ConvertTo-Json -Compress
    $tmp = "$statePath.tmp"
    Set-Content -LiteralPath $tmp -Value $value -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $statePath -Force
}

function Resolve-Entry {
    $direct = Join-Path $env:APPDATA "npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
    if (Test-Path -LiteralPath $direct -PathType Leaf) { return $direct }
    $cache = Join-Path $env:LOCALAPPDATA "npm-cache\_npx"
    if (-not (Test-Path -LiteralPath $cache -PathType Container)) { return $null }
    $matches = @(Get-ChildItem -LiteralPath $cache -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $candidate = Join-Path $_.FullName "node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { Get-Item -LiteralPath $candidate }
    } | Sort-Object LastWriteTimeUtc -Descending)
    if ($matches.Count -eq 0) { return $null }
    return $matches[0].FullName
}

function Descendant-Pids([int]$RootPid) {
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $queue = New-Object 'System.Collections.Generic.Queue[int]'
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parent" -ErrorAction SilentlyContinue)) {
            $childId = [int]$child.ProcessId
            if ($seen.Add($childId)) { $queue.Enqueue($childId) }
        }
    }
    return @($seen)
}

function Stop-OwnedTree([int]$RootPid) {
    $children = @(Descendant-Pids $RootPid | Sort-Object -Descending)
    foreach ($childId in $children) { Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

function Has-EstablishedConnection([int]$RootPid) {
    $ids = @($RootPid) + @(Descendant-Pids $RootPid)
    foreach ($processId in $ids) {
        if (Get-NetTCPConnection -OwningProcess $processId -State Established -ErrorAction SilentlyContinue | Select-Object -First 1) {
            return $true
        }
    }
    return $false
}

function Last-ChannelState {
    $lines = @()
    foreach ($path in @($stdoutLog,$stderrLog)) {
        if (Test-Path -LiteralPath $path) {
            $lines += @(Get-Content -LiteralPath $path -Tail 120 -ErrorAction SilentlyContinue)
        }
    }
    $online = -1
    $offline = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match 'Device marked as online') { $online = $i }
        if ($lines[$i] -match 'Device marked as offline|Remote session expired|IncreaseConnectionPool') { $offline = $i }
    }
    if ($offline -gt $online) { return "offline" }
    if ($online -ge 0) { return "online" }
    return "unknown"
}

function Migrate-LegacySupervisor {
    if (Test-Path -LiteralPath $migrationMarker) { return }
    Write-GuardianLog "starting one-time legacy Desktop Commander ownership migration"
    $legacyRunName = "LIFE OS Desktop Commander Supervisor"
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $runValues = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
    $legacyRun = if ($null -ne $runValues) { $runValues.$legacyRunName } else { $null }
    if ($legacyRun -and $legacyRun -match 'desktop_commander_supervisor\.ps1') {
        Remove-ItemProperty -Path $runKey -Name $legacyRunName -ErrorAction SilentlyContinue
        Write-GuardianLog "removed legacy Desktop Commander Run entry"
    }

    $legacySupervisorPath = Join-Path $runtimeDir "desktop_commander_supervisor.ps1"
    $escapedLegacySupervisor = [regex]::Escape($legacySupervisorPath)
    $legacySupervisors = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^(?:powershell|pwsh)\.exe$' -and
        $_.CommandLine -match $escapedLegacySupervisor
    })
    foreach ($legacySupervisor in $legacySupervisors) {
        Write-GuardianLog "retiring legacy Desktop Commander supervisor pid=$($legacySupervisor.ProcessId)"
        Stop-OwnedTree ([int]$legacySupervisor.ProcessId)
    }

    $targets = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match '@wonderwhy-er[\\/]desktop-commander[\\/]dist[\\/]index\.js' -and
        $_.CommandLine -match '(?:^|\s)remote(?:\s|$)'
    })
    foreach ($target in $targets) {
        Write-GuardianLog "retiring legacy Desktop Commander remote pid=$($target.ProcessId)"
        Stop-OwnedTree ([int]$target.ProcessId)
    }
    Set-Content -LiteralPath $migrationMarker -Value (Get-Date -Format o) -Encoding ASCII
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) { $nodeCommand = Get-Command node -ErrorAction SilentlyContinue }
if ($null -eq $nodeCommand) {
    Write-State "blocked" 0 "node executable unavailable"
    Write-GuardianLog "blocked: node executable unavailable"
    exit 2
}
$node = $nodeCommand.Source

Migrate-LegacySupervisor

$process = $null
$startedAt = $null
$offlineSince = $null
$noConnectionSince = $null
$backoff = 2

while ($true) {
    if ($null -eq $process -or $process.HasExited) {
        $entry = Resolve-Entry
        if (-not $entry) {
            Write-State "blocked" 0 "Desktop Commander package unavailable"
            Write-GuardianLog "package unavailable; retrying"
            Start-Sleep -Seconds 30
            continue
        }
        Remove-Item -LiteralPath $stdoutLog,$stderrLog -Force -ErrorAction SilentlyContinue
        try {
            $quotedEntry = '"' + $entry + '" remote'
            $process = Start-Process -FilePath $node -ArgumentList $quotedEntry -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
            $startedAt = Get-Date
            $offlineSince = $null
            $noConnectionSince = $null
            Write-State "starting" $process.Id "" $entry
            Write-GuardianLog "started pid=$($process.Id) entry=$entry"
            $backoff = 2
        }
        catch {
            Write-State "recovering" 0 $_.Exception.GetType().Name $entry
            Write-GuardianLog "start failed: $($_.Exception.GetType().Name); retry in ${backoff}s"
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min(60, $backoff * 2)
            continue
        }
    }

    Start-Sleep -Seconds 5
    $process.Refresh()
    if ($process.HasExited) {
        Write-State "recovering" 0 "process exited"
        Write-GuardianLog "pid=$($process.Id) exited code=$($process.ExitCode); restarting"
        $process = $null
        continue
    }

    $state = Last-ChannelState
    $connected = Has-EstablishedConnection $process.Id
    $now = Get-Date
    if ($state -eq "online") {
        $offlineSince = $null
        Write-State "online" $process.Id
    }
    elseif ($state -eq "offline") {
        if ($null -eq $offlineSince) { $offlineSince = $now }
    }

    if ($connected) { $noConnectionSince = $null }
    elseif ($null -eq $noConnectionSince) { $noConnectionSince = $now }

    $startupStuck = (($now - $startedAt).TotalSeconds -ge 120 -and $state -ne "online")
    $channelStuck = ($null -ne $offlineSince -and ($now - $offlineSince).TotalSeconds -ge 30)
    $transportGone = ($null -ne $noConnectionSince -and ($now - $noConnectionSince).TotalSeconds -ge 90)

    if ($startupStuck -or $channelStuck -or $transportGone) {
        $reason = if ($channelStuck) { "remote channel offline" } elseif ($transportGone) { "remote transport disconnected" } else { "startup did not reach online" }
        Write-State "recovering" $process.Id $reason
        Write-GuardianLog "recycling pid=$($process.Id): $reason"
        Stop-OwnedTree $process.Id
        try { $process.WaitForExit(5000) | Out-Null } catch {}
        $process = $null
        Start-Sleep -Seconds 2
    }
}
