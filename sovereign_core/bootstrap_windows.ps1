$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$InstallDir = Join-Path $env:LOCALAPPDATA 'SovereignCore'
$StateDir = Join-Path $InstallDir 'state'
$BaseUrl = 'https://raw.githubusercontent.com/aicashflowmonolith-oss/lead-recovery-pilot/sovereign-core-v0/sovereign_core'
$Files = @('monolith.py', 'render_runtime.py', 'local_runtime.py')

New-Item -ItemType Directory -Force -Path $InstallDir, $StateDir | Out-Null

$PythonwCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($PythonwCommand) {
    $Pythonw = $PythonwCommand.Source
} else {
    $Pythonw = (Get-Command python.exe -ErrorAction Stop).Source
}

foreach ($File in $Files) {
    $Destination = Join-Path $InstallDir $File
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$File" -OutFile $Destination
}

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $StateDir /inheritance:r /grant:r "$($Identity):(OI)(CI)F" | Out-Null

$ScriptPath = Join-Path $InstallDir 'local_runtime.py'
$RunCommand = '"{0}" "{1}"' -f $Pythonw, $ScriptPath
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-Item -Path $RunKey -Force | Out-Null
New-ItemProperty -Path $RunKey -Name 'SovereignCore' -PropertyType String -Value $RunCommand -Force | Out-Null

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*SovereignCore*local_runtime.py*' } |
    ForEach-Object {
        if ($_.ProcessId -ne $PID) {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

Start-Process -FilePath $Pythonw -ArgumentList ('"{0}"' -f $ScriptPath) -WindowStyle Hidden

$Deadline = (Get-Date).AddSeconds(20)
$Healthy = $false
do {
    Start-Sleep -Milliseconds 500
    try {
        $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2
        if ($Health.ok -eq $true -and $Health.worker -eq $true -and $Health.control_auth -eq $true) {
            $Healthy = $true
            break
        }
    } catch {}
} while ((Get-Date) -lt $Deadline)

if (-not $Healthy) {
    throw 'Sovereign Core failed its local health verification.'
}

$TokenFile = Join-Path $StateDir 'control.token'
if (Test-Path $TokenFile) {
    & icacls $TokenFile /inheritance:r /grant:r "$($Identity):F" | Out-Null
}

[pscustomobject]@{
    Installed = $true
    PersistentState = Join-Path $StateDir 'monolith.db'
    Startup = 'HKCU Run'
    Endpoint = 'http://127.0.0.1:8765'
    Health = 'verified'
} | ConvertTo-Json -Compress
