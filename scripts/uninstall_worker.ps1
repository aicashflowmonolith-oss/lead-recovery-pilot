param(
    [string]$TaskName = "LIFE OS Worker",
    [string]$RunValueName = "LIFE OS Worker"
)

$ErrorActionPreference = "Stop"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Remove-ItemProperty -Path $runKey -Name $RunValueName -ErrorAction SilentlyContinue

$db = Join-Path $env:USERPROFILE ".life-os\life.db"
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($null -ne $python -and (Test-Path $db)) {
    try {
        $status = (& $python -m life_os --db $db worker-status --json | ConvertFrom-Json)
        if ($null -ne $status.heartbeat.pid) {
            Stop-Process -Id ([int]$status.heartbeat.pid) -ErrorAction SilentlyContinue
        }
    } catch { }
}
Write-Output "LIFE OS worker activation removed. Local LIFE OS data was preserved."
