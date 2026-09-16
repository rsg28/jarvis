' launch_ui.vbs - silent launcher for Jarvis with the floating orb HUD.
'
' No terminal window - Jarvis runs via pythonw.exe and communicates only
' through the HUD orb. Voice output still speaks via edge-tts + pygame.

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw   = scriptDir & "\.venv\Scripts\pythonw.exe"
script    = scriptDir & "\jarvis.py"

If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

cmd = """" & pythonw & """ """ & script & """ --ui"
shell.CurrentDirectory = scriptDir
shell.Run cmd, 0, False
