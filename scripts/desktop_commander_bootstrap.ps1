param(
    [string]$HomeDir = (Join-Path $env:USERPROFILE ".life-os"),
    [string]$GuardianPath = (Join-Path $env:USERPROFILE ".life-os\runtime\desktop-commander\desktop_commander_guardian.ps1"),
    [int]$StaleSeconds = 240,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$mutex = New-Object System.Threading.Mutex($false, "Local\LifeOSDesktopCommanderBootstrap")
$ownsMutex = $false
try { $ownsMutex = $mutex.WaitOne(0, $false) }
catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
if (-not $ownsMutex) { exit 0 }

$runtimeDir = Join-Path $HomeDir "runtime"
$logDir = Join-Path $HomeDir "logs"
$statePath = Join-Path $runtimeDir "desktop-commander-guardian.json"
$logPath = Join-Path $logDir "desktop-commander-bootstrap.log"
New-Item -ItemType Directory -Force -Path $runtimeDir,$logDir | Out-Null

function Write-BootstrapLog([string]$Message) {
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message"
}

function Guardian-Processes {
    $escaped = [regex]::Escape($GuardianPath)
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^(?:powershell|pwsh)\.exe$' -and $_.CommandLine -match $escaped
    })
}

function State-AgeSeconds {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { return $null }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -eq $state.timestamp_epoch) { return $null }
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        return [double]$now - [double]$state.timestamp_epoch
    }
    catch { return $null }
}

function Start-Guardian {
    if (-not (Test-Path -LiteralPath $GuardianPath -PathType Leaf)) {
        Write-BootstrapLog "guardian script unavailable: $GuardianPath"
        return
    }
    $args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$GuardianPath`" -HomeDir `"$HomeDir`""
    Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Hidden | Out-Null
    Write-BootstrapLog "guardian start requested"
}

function Ensure-Guardian {
    $processes = @(Guardian-Processes)
    $age = State-AgeSeconds
    $stale = ($null -eq $age -or $age -gt $StaleSeconds)

    if ($processes.Count -eq 0) {
        Start-Guardian
        return
    }

    if ($stale) {
        foreach ($process in $processes) {
            Write-BootstrapLog "recycling stale guardian pid=$($process.ProcessId) state_age=$age"
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
        Start-Guardian
    }
}

try {
    do {
        try { Ensure-Guardian }
        catch { Write-BootstrapLog "ensure failed: $($_.Exception.GetType().Name)" }
        if (-not $Once) { Start-Sleep -Seconds 30 }
    } while (-not $Once)
}
finally {
    if ($ownsMutex) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
