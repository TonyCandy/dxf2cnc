Set WshShell = CreateObject("WScript.Shell")
Dim curPath
curPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run """" & curPath & "\cnc_gui.py""", 0

