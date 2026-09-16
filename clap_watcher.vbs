' clap_watcher.vbs — starts the clap listener silently (no console window).
'
' Windows will run this on login when a shortcut is placed in
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
' (see install_startup.ps1 in this folder).
'
' You can also double-click this file to start the watcher manually.

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw   = scriptDir & "\.venv\Scripts\pythonw.exe"
script    = scriptDir & "\clap_watcher.py"

' Fall back to system python if the venv is missing (should not happen if
' launch.bat has been run at least once).
If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

' Quote paths that contain spaces and start hidden (window style 0).
cmd = """" & pythonw & """ """ & script & """"
shell.CurrentDirectory = scriptDir
shell.Run cmd, 0, False
