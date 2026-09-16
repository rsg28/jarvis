# install_startup.ps1
#
# Adds a shortcut to Windows Startup that launches clap_watcher.vbs
# silently on every login. Two claps will then wake Jarvis at any time,
# even after a reboot.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1
#
# To remove the auto-start:
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs       = Join-Path $scriptDir "clap_watcher.vbs"
$startup   = [Environment]::GetFolderPath("Startup")
$linkPath  = Join-Path $startup "Jarvis Clap Watcher.lnk"

if ($Uninstall) {
    if (Test-Path $linkPath) {
        Remove-Item $linkPath -Force
        Write-Host "Removed: $linkPath"
    }
    else {
        Write-Host "Nothing to remove - no startup shortcut found."
    }
    exit 0
}

if (-not (Test-Path $vbs)) {
    Write-Error "clap_watcher.vbs not found at $vbs"
    exit 1
}

$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($linkPath)
$sc.TargetPath       = "wscript.exe"
$sc.Arguments        = "`"$vbs`""
$sc.WorkingDirectory = $scriptDir
$sc.IconLocation     = "$env:SystemRoot\System32\SHELL32.dll,71"
$sc.Description      = "Jarvis clap watcher - two claps launch Jarvis"
$sc.WindowStyle      = 7   # minimized (VBS still hides entirely)
$sc.Save()

Write-Host "Installed startup shortcut: $linkPath"
Write-Host "The clap watcher will now start automatically on every Windows login."
Write-Host ""
Write-Host "Starting it now so you dont have to log out and back in..."
Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WorkingDirectory $scriptDir
Write-Host "Running in the background. Two quick claps to wake Jarvis."
