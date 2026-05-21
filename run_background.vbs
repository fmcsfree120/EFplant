Set WShell = CreateObject("WScript.Shell")
Dim scriptDir
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
WShell.Run "cmd /c """ & scriptDir & "Start_Auto_Update.bat""", 0, False
