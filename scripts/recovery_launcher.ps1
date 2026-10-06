param(
    [string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$HomeDir,
    [switch]$CheckOnly,
    [switch]$Once
)
$ErrorActionPreference = "Stop"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$git = (Get-Command git.exe -ErrorAction Stop).Source
$db = Join-Path $HomeDir "life.db"
$logDir = Join-Path $HomeDir "logs"

function Read-Git([string]$Root, [string[]]$GitArgs) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $git
    $info.WorkingDirectory = $Root
    # Windows paths cannot contain quotes. GitArgs are fixed local read commands.
    $info.Arguments = '-c "safe.directory=' + $Root + '" ' + ($GitArgs -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(20000)) {
            $process.Kill() # Only this launcher's own bounded read-only Git child.
            $null = $process.WaitForExit(5000)
            throw "bootstrap_git_timeout"
        }
        if ($process.ExitCode -ne 0) { throw "bootstrap_git_rejected" }
        return $stdout.GetAwaiter().GetResult().Trim()
    } finally {
        $process.Dispose()
    }
}

function Resolve-Bootstrap([string]$Preferred) {
    $data = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
    if ($data.authority -ne 'monolith' -or $data.scope -ne 'reversible_zero_cost_local_recovery' -or
        -not ($data.authorization_ref -is [string]) -or -not $data.authorization_ref -or
        -not ($data.approval_id -is [int] -or $data.approval_id -is [long]) -or
        $data.approval_id -le 0) { throw "bootstrap_manifest_invalid" }
    $roots = @{}
    foreach ($label in @('active', 'rollback')) {
        $entry = $data.$label
        if (-not ($entry.repo -is [string]) -or -not $entry.repo -or
            -not ($entry.commit -is [string]) -or $entry.commit -cnotmatch '^[0-9a-f]{40}$') {
            throw "bootstrap_manifest_invalid"
        }
        try {
            $root = [System.IO.Path]::GetFullPath($entry.repo)
            if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "bootstrap_missing" }
            if ((Read-Git $root @('rev-parse', 'HEAD')) -cne $entry.commit) { throw "bootstrap_head_changed" }
            if (Read-Git $root @('status', '--porcelain')) { throw "bootstrap_dirty" }
            $module = Join-Path $root 'life_os\recovery_controller.py'
            $source = Get-Content -LiteralPath $module -Raw
            if ($source -notmatch '(?m)^BOOTSTRAP_VERSION\s*=\s*(\d+)' -or [int]$Matches[1] -lt 3) {
                throw "bootstrap_protocol_unqualified"
            }
            $roots[$label] = $root
        } catch {
            # Preserve rejected trees and their evidence. Do not reset, fetch,
            # install, authenticate, or infer a new activation authority.
            Write-Verbose ('bootstrap {0} rejected: {1}' -f $label, $_.Exception.Message)
        }
    }
    foreach ($label in @($Preferred, 'active', 'rollback') | Select-Object -Unique) {
        if ($roots.ContainsKey($label)) {
            return [pscustomobject]@{Label=$label; Root=$roots[$label]}
        }
    }
    throw "bootstrap_no_qualified_runtime"
}

function Repair-DesktopCommanderRecovery([string]$Root) {
    $installer = Join-Path $Root 'scripts\install_desktop_commander_guardian.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value 'desktop commander recovery asset unavailable in qualified runtime'
        return
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = 'powershell.exe'
    $info.WorkingDirectory = $Root
    $info.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $installer + '" -RepoRoot "' + $Root + '"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(45000)) {
            $process.Kill()
            $null = $process.WaitForExit(5000)
            Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value 'desktop commander recovery reconcile timed out; controller launch continues'
            return
        }
        if ($process.ExitCode -eq 0) {
            Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value 'desktop commander recovery reconciled from qualified runtime'
        } else {
            Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value ('desktop commander recovery reconcile failed exit=' + $process.ExitCode + '; controller launch continues')
        }
        $null = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
    }
    catch {
        Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value ('desktop commander recovery reconcile error=' + $_.Exception.GetType().Name + '; controller launch continues')
    }
    finally {
        $process.Dispose()
    }
}

# Deploy an exact CI-qualified copy of this launcher outside either checkout.
# RepoRoot is accepted for compatibility; selection comes from the exact manifest.
$preferred = 'active'
while ($true) {
    try {
        $bootstrap = Resolve-Bootstrap $preferred
        if ($CheckOnly) { $bootstrap | ConvertTo-Json -Compress; exit 0 }
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Set-Location -LiteralPath $bootstrap.Root
        # Reconcile the independent Desktop Commander recovery channel from the
        # same exact qualified runtime before starting MONOLITH's controller.
        # Failure here is non-fatal because local recovery must remain available.
        Repair-DesktopCommanderRecovery $bootstrap.Root
        $controllerArgs = @('-m', 'life_os.recovery_controller', '--manifest', $Manifest, '--db', $db, '--home', $HomeDir)
        if ($Once) { $controllerArgs += '--once' }
        # The controller checks the canonical exact approval before any work.
        & $python @controllerArgs *>> (Join-Path $logDir 'recovery-launcher.log')
        $controllerExit = $LASTEXITCODE
        if ($Once) { exit $controllerExit }
        if ($controllerExit -ne 0 -and $bootstrap.Label -eq 'active') { $preferred = 'rollback' }
    } catch {
        if ($CheckOnly -or $Once) { Write-Error 'No qualified authorized bootstrap could start.'; exit 1 }
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Add-Content -LiteralPath (Join-Path $logDir 'recovery-launcher.log') -Value 'bootstrap retry: local qualification or launch failed'
    }
    # Retry restored activation/configuration without using a provider. Missing
    # authority stays gated; never clear pause or manufacture an approval here.
    Start-Sleep -Seconds 30
}
