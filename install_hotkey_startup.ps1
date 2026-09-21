# install_hotkey_startup.ps1
#
# Adds a shortcut to Windows Startup that launches Jarvis in push-to-talk
# mode on every login. After the reboot (or after running this script)
# your configured hotkey (default Ctrl+Alt+J) will trigger Jarvis from
# any application, without needing a terminal open.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_hotkey_startup.ps1
#
# To remove the auto-start:
#   powershell -ExecutionPolicy Bypass -File install_hotkey_startup.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs       = Join-Path $scriptDir "launch_hotkey.vbs"
$startup   = [Environment]::GetFolderPath("Startup")
$linkPath  = Join-Path $startup "Jarvis Hotkey.lnk"

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
    Write-Error "launch_hotkey.vbs not found at $vbs"
    exit 1
}

$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($linkPath)
$sc.TargetPath       = "wscript.exe"
$sc.Arguments        = "`"$vbs`""
$sc.WorkingDirectory = $scriptDir
$sc.IconLocation     = "$env:SystemRoot\System32\SHELL32.dll,44"
$sc.Description      = "Jarvis push-to-talk (Ctrl+Alt+J from any app)"
$sc.WindowStyle      = 7   # minimized (VBS still hides entirely)
$sc.Save()

Write-Host "Installed startup shortcut: $linkPath"
Write-Host "Jarvis will now arm the hotkey automatically on every Windows login."
Write-Host ""
Write-Host "Starting it now so you don't have to log out and back in..."

# Kill any previous silent Jarvis instance first (stale lock, old PID).
Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        $cmd -match 'jarvis\.py'
    } catch { $false }
} | ForEach-Object {
    Write-Host "  stopping existing Jarvis PID $($_.Id)"
    Stop-Process -Id $_.Id -Force
}
$lock = Join-Path $scriptDir "jarvis.lock"
if (Test-Path $lock) { Remove-Item $lock -Force }

Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WorkingDirectory $scriptDir
Start-Sleep -Seconds 2
Write-Host "Running in the background. Press Ctrl+Alt+J from any app to talk."
