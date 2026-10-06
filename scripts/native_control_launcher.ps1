param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [string]$HomeDir = (Join-Path $env:USERPROFILE ".life-os")
)

$ErrorActionPreference = "Stop"
$mutex = New-Object System.Threading.Mutex($false, "Local\LifeOSNativeWindowsControlLauncher")
$ownsMutex = $false
try { $ownsMutex = $mutex.WaitOne(0, $false) }
catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
if (-not $ownsMutex) { exit 0 }

$python = (Get-Command python.exe -ErrorAction Stop).Source
$db = Join-Path $HomeDir "life.db"
$logDir = Join-Path $HomeDir "logs"
$launcherLog = Join-Path $logDir "native-control-launcher.log"
$stdoutLog = Join-Path $logDir "native-control.out.log"
$stderrLog = Join-Path $logDir "native-control.err.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Log([string]$Message) {
    Add-Content -LiteralPath $launcherLog -Value "$(Get-Date -Format o) $Message"
}

$backoff = 2
try {
    while ($true) {
        $args = "-m life_os --db `"$db`" windows-control --home `"$HomeDir`" --repo-root `"$RepoRoot`""
        try {
            $child = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
            Log "started native control pid=$($child.Id)"
            $backoff = 2
            while (-not $child.HasExited) {
                Start-Sleep -Seconds 5
                $child.Refresh()
            }
            Log "native control exited code=$($child.ExitCode)"
        }
        catch {
            Log "native control launch failed: $($_.Exception.GetType().Name)"
        }
        Start-Sleep -Seconds $backoff
        $backoff = [Math]::Min(60, $backoff * 2)
    }
}
finally {
    if ($ownsMutex) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
