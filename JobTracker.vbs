' Launches Job Tracker WITHOUT a visible console window (pure app feel).
' The app window still opens. To stop the server, click "Quit" in the app.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run """" & dir & "\start.bat""", 0, False
