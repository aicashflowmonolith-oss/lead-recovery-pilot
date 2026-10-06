param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$ErrorActionPreference = "Stop"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "LIFE OS.lnk"
$launcher = Join-Path $RepoRoot "scripts\start_app.ps1"
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$q = [char]34

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File $q$launcher$q -RepoRoot $q$RepoRoot$q"
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.Description = "Open the local LIFE OS dashboard"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,21"
$shortcut.Save()

Write-Output "Created $shortcutPath"
