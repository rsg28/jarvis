' launch_hotkey.vbs - silent launcher for Jarvis in push-to-talk mode.
'
' Runs `jarvis.py --hotkey --no-greet` via pythonw.exe so there is no
' console window and no boot greeting on Windows login. Jarvis stays
' resident in the background and only listens when you press the
' configured hotkey (default: Ctrl+Alt+J).
'
' Intended to be triggered by install_hotkey_startup.ps1 on every login.

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw   = scriptDir & "\.venv\Scripts\pythonw.exe"
script    = scriptDir & "\jarvis.py"

If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

' --no-greet keeps startup completely silent. If you want the "Hello,
' Raul." greeting on every login, remove --no-greet below.
cmd = """" & pythonw & """ """ & script & """ --hotkey --no-greet"
shell.CurrentDirectory = scriptDir
shell.Run cmd, 0, False
