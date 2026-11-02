Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
If fs.FileExists(folder & "\HHAssistant.exe") Then
  shell.Run """" & folder & "\HHAssistant.exe""", 0, False
  WScript.Quit
End If
python = "python.exe"
runtimePath = folder & "\python-path.txt"
If fs.FileExists(runtimePath) Then
  Set reader = fs.OpenTextFile(runtimePath, 1)
  python = Trim(reader.ReadAll)
  reader.Close
End If
shell.Run """" & python & """ """ & folder & "\launch.py""", 0, False
