' Launch the webhook daemon with NO console window at all.
'
' Why this exists: the scheduled task ran powershell.exe directly with -WindowStyle Hidden,
' which does not work. PowerShell allocates a console FIRST and hides it a moment later, so
' a window flashed on screen every five minutes, all day. The usual alternatives both have
' costs: running the task as S4U ("whether user is logged on or not") hides it properly but
' needs elevation to register, and this project deliberately stays non-elevated.
'
' WScript.Shell.Run with intWindowStyle = 0 never creates the console in the first place,
' which is why this two-line script is here instead of a flag.
'
' Usage (from the scheduled task):
'   wscript.exe "<repo>\scripts\run_hidden.vbs" "<repo>\scripts\webhook_daemon.ps1"

Option Explicit

Dim shell, fso, scriptPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If

scriptPath = WScript.Arguments(0)
If Not fso.FileExists(scriptPath) Then
    WScript.Quit 3
End If

cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & _
      scriptPath & """"

' 0 = hidden window, True = wait for it to finish so the task's exit code is the
' daemon's own rather than the launcher's.
WScript.Quit shell.Run(cmd, 0, True)
